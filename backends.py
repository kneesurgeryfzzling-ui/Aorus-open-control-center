"""Backends — AORUS Open Control Center v13 (AORUS 15G YC).

Toutes les commandes matérielles passent par un service PowerShell persistant (gbwmi.ps1)
qui parle directement au firmware via WMI (root\\WMI : GB_WMIACPI_Set / GB_WMIACPI_Get / RW_GMWMI).
Recettes décodées depuis DeepFan.dll, Aorus15Keyboard.dll et SmartManager.dll d'origine.
"""
from __future__ import annotations

import colorsys
import fusionhid
import kbfx
import ctypes
import json
import math
import random
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import psutil

IS_WIN = sys.platform == "win32"
FROZEN = getattr(sys, "frozen", False)
RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
OUT_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "AorusOpenCC"
CFG_FILE = APP_DIR / "config.json"
NO_WINDOW = 0x08000000 if IS_WIN else 0
GUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
SET, GET = "GB_WMIACPI_Set", "GB_WMIACPI_Get"


# ----------------------------------------------------------------------------- utilitaires
def run(args, timeout=20):
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    args = sys.argv[1:] if FROZEN else [os.path.abspath(sys.argv[0])] + sys.argv[1:]
    exe = sys.executable
    if not FROZEN:   # pythonw : pas de fenêtre de console, l'appli vit dans la zone de notification
        w = exe.replace("python.exe", "pythonw.exe")
        exe = w if os.path.exists(w) else exe
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe,
                                        " ".join(f'"{a}"' for a in args), None, 1)
    sys.exit(0)


def set_autostart(enable: bool):
    if enable:
        tr = f'"{sys.executable}" --no-window' if FROZEN else \
            f'"{sys.executable.replace("python.exe", "pythonw.exe")}" "{os.path.abspath(sys.argv[0])}" --no-window'
        return run(["schtasks", "/create", "/tn", "AorusOpenCC", "/tr", tr, "/sc", "onlogon",
                    "/rl", "highest", "/f"])
    return run(["schtasks", "/delete", "/tn", "AorusOpenCC", "/f"])


def autostart_enabled() -> bool:
    return run(["schtasks", "/query", "/tn", "AorusOpenCC"])[0] == 0


def hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


class Log:
    def __init__(self):
        self.q = deque(maxlen=500)
        self.n = 0

    def add(self, kind, msg):
        self.n += 1
        self.q.append({"i": self.n, "t": time.strftime("%H:%M:%S"), "k": kind, "m": str(msg)})

    def tail(self, since=0):
        return [e for e in self.q if e["i"] > since]


LOG = Log()


# ----------------------------------------------------------------------------- configuration
DEFAULT_CFG = {
    "last_preset": "", "apply_on_start": True, "background": True, "scheme_guid": "", "custom_presets": {},
    "auto": {"enabled": False, "ac_preset": "", "dc_preset": "battery", "rules": []},
    "fan": {"mode": "normal", "pct": 60, "set": False},
    "fan_curve": {"points": [[40, 30], [55, 40], [65, 55], [75, 75], [85, 100]]},
    "guard": {"enabled": True, "temp": 93},
    "keyboard": {"zones": ["#ff6a00", "#ff6a00", "#ff6a00"], "level": 2, "effect": "static",
                 "speed": 5, "flip": False, "hwcolor": 8, "gain": [14, 100, 36], "keys": {}, "set": False},
    "battery": {"limit_on": False, "limit": 80},
}


def load_cfg() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        user = json.loads(CFG_FILE.read_text("utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict) and k != "custom_presets":
                cfg[k].update(v)
            else:
                cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(cfg: dict):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CFG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    except OSError:
        pass


# ----------------------------------------------------------------------------- service WMI
class WmiService:
    """Processus PowerShell persistant. Une requête JSON par ligne, une réponse par ligne."""

    def __init__(self, name="ctl"):
        self.name, self.proc, self.ready = name, None, threading.Event()
        self.lock, self.resp, self.seq = threading.Lock(), queue.Queue(), 0
        self.fake = os.environ.get("AORUS_FAKE_SERVICE")  # tests : script python simulant le firmware
        self.ok_start = True

    def _spawn(self):
        if self.fake:
            cmd = [sys.executable, self.fake]
        elif IS_WIN:
            cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(RES_DIR / "gbwmi.ps1")]
        else:
            self.ok_start = False
            return False
        self.ready.clear()
        self.resp = queue.Queue()
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                         encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        except OSError as e:
            LOG.add("err", f"[{self.name}] démarrage PowerShell impossible : {e}")
            self.ok_start = False
            return False
        threading.Thread(target=self._reader, args=(self.proc, self.resp), daemon=True).start()
        if not self.ready.wait(15):
            LOG.add("err", f"[{self.name}] PowerShell ne répond pas")
            self._kill()
            return False
        return True

    def _reader(self, proc, q):
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("ready"):
                self.ready.set()
            else:
                q.put(d)

    def _kill(self):
        try:
            if self.proc:
                self.proc.kill()
        except OSError:
            pass
        self.proc = None

    def request(self, op, timeout=12, **kw):
        """Retourne (ok, data_ou_erreur)."""
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                if not self._spawn():
                    return False, "Service WMI indisponible (Windows + PowerShell requis)"
            self.seq += 1
            msg = dict(kw, id=self.seq, op=op)
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError) as e:
                self._kill()
                return False, f"écriture impossible : {e}"
            end = time.time() + timeout
            while time.time() < end:
                try:
                    d = self.resp.get(timeout=max(0.05, end - time.time()))
                except queue.Empty:
                    break
                if d.get("id") == self.seq:
                    return (True, d.get("data")) if d.get("ok") else (False, d.get("err", "erreur inconnue"))
            self._kill()
            return False, "délai dépassé (le service WMI ne répond pas)"

    def stop(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.stdin.write(json.dumps({"id": 0, "op": "quit"}) + "\n")
                self.proc.stdin.flush()
        except (OSError, ValueError):
            pass
        self._kill()


def call(method, data=None, cls=SET, optional=False, **extra):
    a = dict(extra)
    if data is not None:
        a["Data"] = data
    return {"cls": cls, "method": method, "args": a, "optional": optional}


# ----------------------------------------------------------------------------- ventilateurs
RAW_MAX = 229  # DeepFan.dll : duty = 229 * pourcentage / 100 (octet), pourcentage 30..100


def fan_raw(pct):
    pct = max(30, min(100, int(pct)))
    return int(RAW_MAX * pct / 100)


FAN_MODES = {
    "normal": dict(label="Normal", desc="Courbe d'origine du firmware, équilibre bruit / température."),
    "quiet": dict(label="Silencieux", desc="Courbe discrète, cible thermique GPU abaissée."),
    "gaming": dict(label="Gaming", desc="Courbe plus agressive pour tenir les charges soutenues."),
    "turbo": dict(label="Turbo", desc="Ventilateurs fixes à 100 %. Bruyant : à garder pour les pics."),
    "fixed": dict(label="Manuel", desc="Vitesse fixe réglable de 30 à 100 %."),
    "cap": dict(label="Auto plafonné", desc="Courbe automatique avec une vitesse maximale limitée."),
    "curve": dict(label="Ma courbe", desc="Ta propre courbe température → vitesse, pilotée par l'appli."),
}


def curve_pct(points, temp):
    """Interpolation linéaire de la courbe [[°C, %], …], arrondie au pas de 5 %, bornée 30–100."""
    pts = sorted((float(a), float(b)) for a, b in points)
    if temp is None or not pts:
        return 70
    if temp <= pts[0][0]:
        v = pts[0][1]
    elif temp >= pts[-1][0]:
        v = pts[-1][1]
    else:
        v = pts[-1][1]
        for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
            if t0 <= temp <= t1:
                v = p0 + (p1 - p0) * (temp - t0) / max(0.001, t1 - t0)
                break
    return int(max(30, min(100, round(v / 5) * 5)))


def clean_curve(points):
    pts = []
    for a, b in points or []:
        pts.append([int(max(20, min(100, float(a)))), int(max(30, min(100, float(b))))])
    pts.sort()
    if not 2 <= len(pts) <= 8:
        raise ValueError("la courbe doit avoir entre 2 et 8 points")
    for (t0, _), (t1, _) in zip(pts, pts[1:]):
        if t1 - t0 < 2:
            raise ValueError("deux points sont trop proches en température")
    return pts


class FanController:
    def __init__(self, svc, cfg, temp_fn=lambda: None):
        self.svc, self.cfg, self.temp_fn = svc, cfg, temp_fn
        self.mode, self.pct = cfg["fan"]["mode"], cfg["fan"]["pct"]
        if self.mode not in FAN_MODES:
            self.mode = "normal"
        self.last_ok, self.last_msg = None, "Aucune commande envoyée."
        self.override = None          # "guard" quand la garde thermique a pris la main
        self.curve_pct, self.curve_t, self.curve_down = None, 0.0, 0
        self.lock = threading.Lock()

    def effective_pct(self):
        if self.override:
            return 100
        return {"turbo": 100, "fixed": self.pct, "cap": self.pct, "curve": self.curve_pct}.get(self.mode)

    @staticmethod
    def plan(mode, pct=100):
        if mode in ("normal", "quiet", "gaming"):
            return [call("SetFixedFanStatus", 0), call("SetStepFanStatus", 0),
                    call("SetAutoFanStatus", 1 if mode == "gaming" else 0),
                    call("SetNvThermalTarget", 1 if mode == "quiet" else 0, optional=True)]
        raw = fan_raw(100 if mode == "turbo" else pct)
        head = [call("SetAutoFanStatus", 0), call("SetFixedFanStatus", 0), call("SetStepFanStatus", 1),
                call("SetNvThermalTarget", 0, optional=True)]
        if mode == "cap":
            return head + [call("SetFanAdjustStatus", raw)]
        return head + [call("SetFixedFanStatus", 1), call("SetFixedFanSpeed", raw),
                       call("SetGPUFanDuty", raw, optional=True)]

    def _send(self, mode, pct):
        ok, res = self.svc.request("seq", calls=self.plan(mode, pct), delay=40, timeout=25)
        if ok:
            bad = [r for r in res if not r.get("ok")]
            hard = [r for r in bad if r["m"] not in ("SetNvThermalTarget", "SetGPUFanDuty")]
            ok = not hard
            msg = "OK" if ok else "; ".join(f"{r['m']}: {r.get('err')}" for r in hard)
            for r in bad:
                LOG.add("warn", f"ventilos · {r['m']} ignoré : {r.get('err')}")
        else:
            msg = str(res)
        return ok, msg

    def apply(self, mode, pct=None, persist=True, quiet=False):
        if mode not in FAN_MODES:
            return False, "mode inconnu"
        pct = self.pct if pct is None else int(pct)
        with self.lock:
            if mode == "curve":
                t = self.temp_fn()
                hw = curve_pct(self.cfg["fan_curve"]["points"], t)
                ok, msg = self._send("fixed", hw)
                if ok:
                    self.curve_pct, self.curve_t, self.curve_down = hw, time.time(), 0
                extra = f" ({'%.0f °C' % t if t is not None else 'capteurs en attente'} → {hw} %)"
            else:
                ok, msg = self._send(mode, pct)
                extra = f" {pct} %" if mode in ("fixed", "cap") else ""
            self.last_ok, self.last_msg = ok, msg
            if not quiet or not ok:
                LOG.add("ok" if ok else "err", f"ventilos · {FAN_MODES[mode]['label']}{extra} → {msg}")
            if ok and persist:
                self.override = None
                self.mode, self.pct = mode, pct
                self.cfg["fan"].update(mode=mode, pct=pct, set=True)
                save_cfg(self.cfg)
            return ok, msg

    def curve_tick(self, temp):
        """Appelé toutes les 2 s en mode « Ma courbe » : monte tout de suite, redescend après ~8 s."""
        target = curve_pct(self.cfg["fan_curve"]["points"], temp)
        now = time.time()
        cur = self.curve_pct
        send = False
        if cur is None:
            send = True
        elif target > cur:
            send, self.curve_down = now - self.curve_t >= 2, 0
        elif target < cur:
            self.curve_down += 1
            send = self.curve_down >= 4 and now - self.curve_t >= 6
        else:
            self.curve_down = 0
        if not send:
            return
        with self.lock:
            ok, msg = self._send("fixed", target)
            self.last_ok, self.last_msg = ok, msg
            self.curve_t = now
            if ok:
                self.curve_pct, self.curve_down = target, 0
                LOG.add("info", f"courbe · {'%.0f °C' % temp if temp is not None else 'temp. inconnue'} → {target} %")
            else:
                LOG.add("err", f"courbe · {msg}")

    def read_table(self):
        out = []
        for i in range(15):
            ok, d = self.svc.request("call", cls=GET, method="GetFanIndexValue", args={"Index": i})
            if not ok:
                return False, str(d)
            out.append((i, d.get("Temperture"), d.get("Value")))
        return True, out


# ----------------------------------------------------------------------------- clavier (3 zones)
HW_EFFECTS = {"hw2": 2, "hw3": 3, "hw4": 4, "hw6": 6}  # modes matériels vus dans l'UI d'origine
KB_EFFECTS_HID = (["static", "gradient", "rainbowfix", "paint", "mire"] + kbfx.NAMES + ["wave", "breathe", "aurora", "fire", "ocean", "police", "temp"]
                  + [f"hw{i}" for i in range(1, 14)])
KB_EFFECTS = ["static"] + kbfx.NAMES + ["breathe", "rainbow", "wave", "aurora", "fire", "ocean", "police", "temp", "hw2", "hw3", "hw4", "hw6"]
ZONE_AREAS = (3, 4, 5)  # gauche, centre, droite ; 6 = toutes ; 7 = ambiance


def kb_packet(area, mode, level, rgb, flip=False):
    """Paquet RW_GMWMI.BufferBytes (32 octets) — reproduit WmiTool.SetLedControl[NA]."""
    if flip and area in (3, 5):
        area = 8 - area
    b = [0] * 32
    b[0], b[2] = 0xFB, 1
    b[7] = area
    b[8] = (mode * 16 + level) & 0xFF
    b[9], b[10], b[11] = (max(0, min(255, int(c))) for c in rgb)
    out = [0] * 32
    out[0], out[1], out[2], out[3] = b[1], b[0], b[3], b[2]
    for i in range(7):
        j = 4 * i
        out[j + 4], out[j + 5], out[j + 6], out[j + 7] = b[j + 7], b[j + 6], b[j + 5], b[j + 4]
    return out


class KeyboardController:
    def __init__(self, svc, cfg, temp_fn, sys_fn=None):
        self.svc, self.cfg, self.temp_fn, self.sys_fn = svc, cfg, temp_fn, sys_fn
        self.stop_ev, self.thr = threading.Event(), None
        self.preview = list(cfg["keyboard"]["zones"])
        self.err_once = False
        self.last_err = ""
        self.hid, self.hid_seen, self.hid_tried, self.backend = None, [], False, "wmi"
        self.keyframe = {}
        self.fx = None

    def flash(self):
        """Éclair envoyé par l'interface (effets Orage et Pluie)."""
        if self.fx is not None:
            self.fx.ext = time.time()
            return True
        return False

    @property
    def k(self):
        return self.cfg["keyboard"]

    def _put(self, area, mode, level, rgb):
        ok, e = self.svc.request("kbput", bytes=kb_packet(area, mode, level, rgb, self.k["flip"]), timeout=6)
        if not ok:
            self.last_err = str(e)
        if not ok and not self.err_once:
            self.err_once = True
            LOG.add("err", f"clavier · écriture RW_GMWMI refusée : {e}")
        elif ok:
            self.err_once = False
        return ok

    def _zones(self, mode, level, cols, gap=0.06):
        """Comme le logiciel d'origine : une écriture par zone (3 = gauche, 4 = centre, 5 = droite), 60 ms d'écart."""
        res = []
        for a, c in zip(ZONE_AREAS, cols):
            res.append(self._put(a, mode, level, c))
            time.sleep(gap)
        return all(res)

    def test(self):
        """Rouge / vert / bleu par zone, avec relecture, pour vérifier que le clavier répond."""
        self._stop()
        if self.ensure():
            ok = True
            for cols in (((255, 0, 0),) * 3, ((0, 255, 0),) * 3, ((0, 0, 255),) * 3, ((255, 0, 0), (0, 255, 0), (0, 0, 255))):
                ok &= self._hid_run(lambda c=cols: self.hid.custom(self._mat(lambda n, x, y: c[fusionhid.zone_of_x(x)]), 50))
                time.sleep(0.8)
            LOG.add("ok" if ok else "err", f"clavier · test RVB (USB) → {'OK' if ok else self.last_err}")
            self.apply()
            return ok
        steps = [((255, 0, 0),) * 3, ((0, 255, 0),) * 3, ((0, 0, 255),) * 3,
                 ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
        oks = []
        for cols in steps:
            oks.append(self._zones(1, 2, cols))
            time.sleep(0.7)
        ok, rb = self.svc.request("kbget", timeout=6)
        LOG.add("ok" if all(oks) else "err", f"clavier · test RVB → {'écritures OK' if all(oks) else 'écriture refusée'} · relecture {rb if ok else 'impossible'}")
        self.apply()
        return all(oks)

    def _stop(self):
        self.fx = None
        self.stop_ev.set()
        if self.thr and self.thr.is_alive() and self.thr is not threading.current_thread():
            self.thr.join(timeout=2)

    def apply(self):
        self._stop()
        self.keyframe = {}
        if self.ensure():
            return self._apply_hid()
        k = self.k
        zones, level, eff = [hex_to_rgb(z) for z in k["zones"]], int(k["level"]), k["effect"]
        if level == 0:
            self.preview = ["#000000"] * 3
            ok = self._put(6, 1, 0, (0, 0, 0))
            LOG.add("ok" if ok else "err", "clavier · éteint")
            return ok
        if eff == "static" or eff in HW_EFFECTS:
            mode = 1 if eff == "static" else HW_EFFECTS[eff]
            self.preview = list(k["zones"])
            ok = self._zones(mode, level, zones)
            ok2, rb = self.svc.request("kbget", timeout=6)
            if ok and ok2:
                LOG.add("info", f"clavier · relecture tampon : {list(rb)[:16] if isinstance(rb, list) else rb}")
            LOG.add("ok" if ok else "err", f"clavier · {eff} niveau {level} {k['zones']}")
            return ok
        self.stop_ev = threading.Event()
        if eff in kbfx.NAMES:
            self.thr = threading.Thread(target=self._anim_fx, args=(self.stop_ev, eff, zones, 0, False), daemon=True)
            self.thr.start()
            LOG.add("ok", f"clavier · effet {eff} (3 zones, WMI)")
            return True
        self.thr = threading.Thread(target=self._anim, args=(self.stop_ev, eff, zones, level), daemon=True)
        self.thr.start()
        LOG.add("ok", f"clavier · animation {eff}")
        return True

    @staticmethod
    def _hsv(h, s=1.0, v=1.0):
        r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
        return (r * 255, g * 255, b * 255)

    def _cols(self, eff, t, zones):
        if eff == "breathe":
            f = 0.08 + 0.92 * (math.sin(t * 1.7) + 1) / 2
            return [tuple(c * f for c in z) for z in zones]
        if eff == "rainbow":
            return [self._hsv(t * 0.08)] * 3
        if eff == "wave":
            return [self._hsv(t * 0.07 + i * 0.16) for i in range(3)]
        if eff == "aurora":
            return [self._hsv(0.38 + 0.2 * math.sin(t * 0.5 + i * 1.3), 0.85, 0.55 + 0.45 * (math.sin(t * 0.9 + i) + 1) / 2) for i in range(3)]
        if eff == "fire":
            return [self._hsv(0.02 + 0.06 * random.random(), 1.0, 0.45 + 0.55 * random.random()) for _ in range(3)]
        if eff == "ocean":
            return [self._hsv(0.52 + 0.1 * math.sin(t * 0.6 + i * 1.1), 0.9, 0.5 + 0.5 * (math.sin(t * 0.8 + i * 1.7) + 1) / 2) for i in range(3)]
        if eff == "police":
            on = int(t * 3) % 2
            return [(255, 0, 0) if (on and i < 1) or (not on and i > 1) else (0, 0, 255) if (on and i > 1) or (not on and i < 1) else (0, 0, 0) for i in range(3)]
        frac = max(0.0, min(1.0, ((self.temp_fn() or 50) - 45) / 50))  # temp : bleu -> rouge
        return [self._hsv((1 - frac) * 0.62)] * 3

    def _anim(self, stop, eff, zones, level):
        t0 = time.time()
        speed = max(1, min(10, int(self.k["speed"]))) / 5.0
        while not stop.is_set():
            cols = self._cols(eff, (time.time() - t0) * speed, zones)
            self.preview = [rgb_to_hex(c) for c in cols]
            same = cols[0] == cols[1] == cols[2]
            if same:
                self._put(6, 1, level, cols[0])
                stop.wait(0.12)
            else:
                for a, c in zip(ZONE_AREAS, cols):
                    self._put(a, 1, level, c)
                    stop.wait(0.05)
                stop.wait(0.05)

    # ------------------------------------------------------------------ clavier USB Fusion (15G YC)
    def ensure(self):
        """Détecte une fois le clavier Fusion USB ; le 15G YC n'utilise pas le WMI RW_GMWMI."""
        if self.hid_tried:
            return self.hid is not None
        self.hid_tried = True
        fake = os.environ.get("AORUS_FAKE_HID")
        if not (IS_WIN or fake):
            return False
        try:
            self.hid, self.hid_seen = fusionhid.FusionKb.find(
                lambda m: LOG.add("info", m), (lambda kind, d: LOG.add("info", f"HID fake {kind} {d}")) if fake else None)
        except Exception as e:  # noqa: BLE001
            LOG.add("err", f"clavier USB · détection impossible : {e}")
            self.hid = None
        if self.hid:
            self.backend = "hid"
            LOG.add("ok", f"clavier · Fusion USB détecté (pid 0x{self.hid.info.get('pid', 0):04X}) — mode HID actif")
        else:
            others = [d for d in self.hid_seen if d.get("pid") in (fusionhid.PID_IONE | fusionhid.PID_X9)]
            LOG.add("warn", "clavier · aucun clavier Fusion USB compatible trouvé"
                    + (f" (famille non gérée : {[hex(d['pid']) for d in others]})" if others else "") + " → essai du WMI")
        return self.hid is not None

    @staticmethod
    def _zone_of(col):
        return 0 if col <= 6 else 1 if col <= 12 else 2

    @staticmethod
    def _lerp3(zs, x):
        x = max(0.0, min(1.0, x)) * 2
        i = min(1, int(x))
        f = x - i
        return tuple(zs[i][c] * (1 - f) + zs[i + 1][c] * f for c in range(3))

    def _color_fn(self, eff, zones, cols3=None):
        W = fusionhid.WIDTH
        if eff == "mire":
            bars = [(255, 0, 0), (255, 165, 0), (255, 255, 0), (0, 255, 0), (0, 255, 255), (0, 0, 255), (128, 0, 255),
                    (255, 0, 255), (255, 255, 255)]
            return lambda n, x, y: bars[min(8, int(x / W * 9))]
        if eff == "rainbowfix":
            return lambda n, x, y: self._hsv(x / W)
        if eff == "static":
            return lambda n, x, y: zones[fusionhid.zone_of_x(x)]
        if eff == "paint":
            keys = {}
            for n, c in (self.k.get("keys") or {}).items():
                try:
                    keys[n] = hex_to_rgb(c)
                except (ValueError, TypeError):
                    pass
            return lambda n, x, y: keys.get(n, (0, 0, 0))
        z = cols3 or zones  # gradient lissé entre les 3 couleurs
        return lambda n, x, y: self._lerp3(z, x / W)

    def _mat(self, fn):
        return fusionhid.matrix_from_names(fn, self.k.get("gain", fusionhid.GAIN_GIGABYTE))

    def _set_keyframe(self, fn):
        self.keyframe = {n: rgb_to_hex(fn(n, x + w / 2, y + h / 2)) for n, _l, x, y, w, h in fusionhid.LAYOUT}

    def _hid_run(self, fn):
        try:
            fn()
            return True
        except Exception as e:  # noqa: BLE001
            self.last_err = f"USB HID : {e}"
            LOG.add("err", "clavier · " + self.last_err)
            return False

    def _apply_hid(self):
        k = self.k
        zones, level, eff = [hex_to_rgb(z) for z in k["zones"]], int(k["level"]), k["effect"]
        br = (0, 25, 50)[max(0, min(2, level))]
        if level == 0:
            self.preview = ["#000000"] * 3
            self.keyframe = {}
            ok = self._hid_run(lambda: self.hid.custom(fusionhid.build_matrix(lambda c, r: (0, 0, 0)), 0))
            LOG.add("ok" if ok else "err", "clavier · éteint (USB)")
            return ok
        if eff in ("static", "gradient", "rainbowfix", "mire", "paint"):
            self.preview = list(k["zones"]) if eff != "rainbowfix" else ["#ff0040", "#40ff40", "#4040ff"]
            fn = self._color_fn(eff, zones)
            self._set_keyframe(fn)
            ok = self._hid_run(lambda: self.hid.custom(self._mat(fn), br))
            LOG.add("ok" if ok else "err", f"clavier · {eff} (USB) niveau {level}")
            return ok
        if eff.startswith("hw"):
            n = int(eff[2:])
            sp = max(1, min(10, int(k["speed"])))
            ok = self._hid_run(lambda: self.hid.effect(n, sp, br, int(k.get("hwcolor", 8)), 1))
            LOG.add("ok" if ok else "err", f"clavier · effet matériel {n} (USB)")
            return ok
        self.stop_ev = threading.Event()
        tgt = self._anim_fx if eff in kbfx.NAMES else self._anim_hid
        self.thr = threading.Thread(target=tgt, args=(self.stop_ev, eff, zones, br, True) if eff in kbfx.NAMES
                                    else (self.stop_ev, eff, zones, br), daemon=True)
        self.thr.start()
        LOG.add("ok", f"clavier · animation {eff} (USB)")
        return True

    def _anim_hid(self, stop, eff, zones, br):
        t0 = time.time()
        speed = max(1, min(10, int(self.k["speed"]))) / 5.0
        while not stop.is_set():
            cols = self._cols(eff, (time.time() - t0) * speed, zones)
            self.preview = [rgb_to_hex(c) for c in cols]
            fn = self._color_fn("gradient", zones, cols)
            self._set_keyframe(fn)
            m = self._mat(fn)
            if not self._hid_run(lambda: self.hid.custom(m, br)):
                stop.wait(3)
            stop.wait(0.3)

    def _anim_fx(self, stop, eff, zones, br, hid):
        """Effets touche par touche (kbfx). En mode WMI (3 zones), on moyenne les couleurs par zone."""
        fx = self.fx = kbfx.Fx(eff, zones, self.k["speed"], ctx=self.sys_fn)
        poll = kbfx.KeyPoll() if eff in kbfx.REACTIVE else None
        t0 = last = time.time()
        fails = 0
        while not stop.is_set():
            now = time.time()
            t, dt, last = now - t0, min(.25, now - last), now
            fr = fx.frame(t, dt, poll.new() if poll else [])
            self.keyframe = {n: rgb_to_hex(c) for n, c in fr.items()}
            zc = [[0, 0, 0, 0] for _ in range(3)]
            for n, c in fr.items():
                z = zc[fusionhid.zone_of_x(fusionhid.POS[n][0])]
                z[0] += c[0]; z[1] += c[1]; z[2] += c[2]; z[3] += 1
            cols = [tuple(v / max(1, z[3]) for v in z[:3]) for z in zc]
            self.preview = [rgb_to_hex(c) for c in cols]
            if hid:
                m = self._mat(lambda n, x, y: fr.get(n, (0, 0, 0)))
                if self._hid_run(lambda: self.hid.custom(m, br, fast=True)):
                    fails = 0
                else:
                    fails += 1
                    self.hid.slow_down()
                    stop.wait(min(5, fails))
            else:
                lv = int(self.k["level"])
                for a, c in zip(ZONE_AREAS, cols):
                    self._put(a, 1, lv, c)
                    stop.wait(.04)
            stop.wait(.01)

    def shutdown(self):
        self._stop()


# ----------------------------------------------------------------------------- batterie / boosts
class BatteryController:
    def __init__(self, svc, cfg):
        self.svc, self.cfg = svc, cfg

    def set_limit(self, on, pct=80):
        pct = max(50, min(100, int(pct)))
        calls = [call("SetChargePolicy", 1), call("SetChargeStop", pct)] if on else [call("SetChargePolicy", 0)]
        ok, res = self.svc.request("seq", calls=calls, delay=60)
        bad = [r for r in res if not r.get("ok")] if ok else []
        ok = ok and not bad
        msg = "OK" if ok else (str(res) if not isinstance(res, list) else "; ".join(f"{r['m']}: {r.get('err')}" for r in bad))
        LOG.add("ok" if ok else "err", f"batterie · limite {'%d %%' % pct if on else 'désactivée'} → {msg}")
        if ok:
            self.cfg["battery"].update(limit_on=bool(on), limit=pct)
            save_cfg(self.cfg)
        return ok, msg

    def read(self):
        out = {}
        for key, m in (("policy", "GetChargePolicy"), ("stop", "GetChargeStop"),
                       ("dynamic", "GetDynamicBoostStatus")):
            ok, d = self.svc.request("call", cls=GET, method=m, timeout=6)
            out[key] = d.get("Data") if ok and isinstance(d, dict) else None
        return out

    def set_boost(self, kind, on):
        m = "SetDynamicBoostStatus" if kind == "dynamic" else "SetAIBoostStatus"
        ok, res = self.svc.request("call", cls=SET, method=m, args={"Data": 1 if on else 0})
        LOG.add("ok" if ok else "err", f"boost · {kind} {'on' if on else 'off'} → {'OK' if ok else res}")
        return ok, ("OK" if ok else str(res))


# ----------------------------------------------------------------------------- alimentation Windows
SUB_PROC = "54533251-82be-4824-96c1-47b60b740d00"
PROCMAX, PROCMIN = "bc5038f7-23e0-4960-96da-33abaf5935ec", "893dee8e-2bef-41e0-89c6-b55d0929964c"
BOOST, COOLPOL = "be337238-0d82-4146-a960-4f3749d470c7", "94d3a615-a899-4ac5-ae2b-e4d8f634367f"
BALANCED_SCHEME, SCHEME_NAME = "381b4222-f694-41f0-9685-ff5bb260df2e", "AORUS Open CC"
OVERLAYS = {"efficiency": "961cc777-2547-4f9d-8174-7d86181b8a7a", "balanced": "00000000-0000-0000-0000-000000000000",
            "performance": "3af9b8d9-7c97-431d-ad78-34a8bfea439f", "best": "ded574b5-45a0-4f42-8737-46345c09c238"}

PRESETS = {
    "silent": dict(label="Silencieux", icon="moon", color="#5b9dff", fan="quiet", fan_pct=60,
                   desc="CPU 70 %, turbo coupé, GPU bridé, ventilos discrets.",
                   cpu_max=70, cpu_min=5, boost=0, cooling=0, gpu_pct=40, overlay="efficiency"),
    "balanced": dict(label="Équilibré", icon="scale", color="#3ecf8e", fan="normal", fan_pct=60,
                     desc="Usage quotidien : turbo actif, GPU à 80 %.",
                     cpu_max=100, cpu_min=5, boost=1, cooling=1, gpu_pct=80, overlay="balanced"),
    "performance": dict(label="Performance", icon="bolt", color="#ffa21a", fan="gaming", fan_pct=75,
                        desc="Turbo agressif, GPU à fond, courbe Gaming.",
                        cpu_max=100, cpu_min=10, boost=2, cooling=1, gpu_pct=100, overlay="performance"),
    "turbo": dict(label="Turbo", icon="flame", color="#ff4d3d", fan="turbo", fan_pct=100,
                  desc="Tout au maximum, ventilos 100 %. Branché uniquement.",
                  cpu_max=100, cpu_min=50, boost=2, cooling=1, gpu_pct=100, overlay="best"),
    "battery": dict(label="Autonomie", icon="leaf", color="#a48bff", fan="quiet", fan_pct=40,
                    desc="CPU 60 %, GPU minimal, ventilos discrets.",
                    cpu_max=60, cpu_min=5, boost=0, cooling=0, gpu_pct=30, overlay="efficiency"),
}


def all_presets(cfg):
    merged = dict(PRESETS)
    for name, p in cfg.get("custom_presets", {}).items():
        merged["custom:" + name] = dict(label=name, icon="star", color="#ff6a00",
                                        desc=f"Perso · CPU {p['cpu_min']}–{p['cpu_max']} %, GPU {p['gpu_pct']} %.", **p)
    return merged


class PowerManager:
    def __init__(self, cfg):
        self.cfg, self.guid = cfg, cfg.get("scheme_guid") or ""

    def ensure_scheme(self):
        rc, out = run(["powercfg", "/list"])
        if rc != 0:
            return None
        for line in out.splitlines():
            m = GUID_RE.search(line)
            if m and f"({SCHEME_NAME})" in line:
                self.guid = self.cfg["scheme_guid"] = m.group(0)
                return self.guid
        rc, out = run(["powercfg", "/duplicatescheme", BALANCED_SCHEME])
        m = GUID_RE.search(out)
        if rc != 0 or not m:
            return None
        self.guid = self.cfg["scheme_guid"] = m.group(0)
        run(["powercfg", "/changename", self.guid, SCHEME_NAME, "Créé par AORUS Open Control Center"])
        return self.guid

    def _set(self, setting, value):
        return all(run(["powercfg", f, self.guid, SUB_PROC, setting, str(int(value))])[0] == 0
                   for f in ("/setacvalueindex", "/setdcvalueindex"))

    def apply(self, p):
        if not self.ensure_scheme():
            return [("Plan d'alimentation", False, "powercfg indisponible (Windows requis)")]
        cmin = min(p["cpu_min"], p["cpu_max"])
        run(["powercfg", "-attributes", SUB_PROC, BOOST, "-ATTRIB_HIDE"])
        res = [("CPU max", self._set(PROCMAX, p["cpu_max"]), f"{p['cpu_max']} %"),
               ("CPU min", self._set(PROCMIN, cmin), f"{cmin} %"),
               ("Turbo CPU", self._set(BOOST, p["boost"]), str(p["boost"])),
               ("Refroidissement Windows", self._set(COOLPOL, p["cooling"]), "")]
        rc, out = run(["powercfg", "/setactive", self.guid])
        res.append(("Plan actif", rc == 0, out))
        rc, out = run(["powercfg", "/overlaysetactive", OVERLAYS.get(p.get("overlay", "balanced"), OVERLAYS["balanced"])])
        res.append(("Mode Windows", rc == 0, out))
        return res


class Gpu:
    FIELDS = ("name,temperature.gpu,utilization.gpu,power.draw,power.limit,power.min_limit,"
              "power.max_limit,clocks.gr,memory.used,memory.total")
    KEYS = "name temp util power plimit pmin pmax clk vram_used vram_total".split()

    def __init__(self):
        c = [shutil.which("nvidia-smi"), r"C:\Windows\System32\nvidia-smi.exe",
             r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"]
        self.exe = next((x for x in c if x and os.path.exists(x)), None)

    @staticmethod
    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def read(self):
        if not self.exe:
            return None
        rc, out = run([self.exe, f"--query-gpu={self.FIELDS}", "--format=csv,noheader,nounits"], 6)
        if rc != 0 or not out:
            return None
        v = [x.strip() for x in out.splitlines()[0].split(",")]
        if len(v) < len(self.KEYS):
            return None
        return {k: (v[i] if k == "name" else self._num(v[i])) for i, k in enumerate(self.KEYS)}

    def set_power_pct(self, pct):
        d = self.read()
        if not d:
            return False, "GPU NVIDIA non détecté"
        lo, hi = d["pmin"], d["pmax"]
        if lo is None or hi is None:
            return False, "limite de puissance non modifiable sur ce GPU"
        w = round(lo + (hi - lo) * pct / 100)
        rc, out = run([self.exe, "-pl", str(w)], 10)
        return rc == 0, (f"{w} W" if rc == 0 else (out.splitlines() or [""])[0])


# ----------------------------------------------------------------------------- hub
PROTECTED = {"system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
             "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe", "memory compression",
             "secure system", "lsaiso.exe", "msedge.exe", "chrome.exe", "python.exe", "pythonw.exe",
             "aorus-open-cc.exe", "powershell.exe", "conhost.exe", "system idle process", "audiodg.exe",
             "msmpeng.exe", "sihost.exe", "ctfmon.exe", "runtimebroker.exe", "systemd", "init"}


def kill_by_name(name):
    if name.lower() in PROTECTED:
        return False, f"{name} est un processus système protégé"
    n = 0
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "") == name and p.pid != os.getpid():
            try:
                p.terminate()
                n += 1
            except psutil.Error:
                pass
    LOG.add("ok" if n else "warn", f"processus · {name} : {n} fermé(s)")
    return (n > 0), (f"{name} fermé ({n} processus)" if n else f"Impossible de fermer {name}")


class Hub:
    """Chef d'orchestre : état, commandes, threads de fond."""

    def __init__(self):
        self.cfg = load_cfg()
        self.ctl, self.mon = WmiService("ctl"), WmiService("mon")
        self.gpu, self.power = Gpu(), PowerManager(self.cfg)
        self.state = {"cpu": 0, "freq": None, "ram": 0, "ram_used": 0, "ram_total": 0, "batt": None,
                      "gpu": None, "cpu_temp": None, "gpu_temp": None, "fans": [], "sens_src": "—",
                      "net": {"up": 0, "down": 0}, "disk": None, "uptime": 0, "cores": psutil.cpu_count() or 1}
        self.history = deque(maxlen=400)   # ~10 min à 1,5 s
        self.procs, self._pcache, self._net, self._tick = [], {}, None, 0
        self.fan = FanController(self.ctl, self.cfg, self.max_temp)
        self.kb = KeyboardController(self.ctl, self.cfg, self.max_temp, self.sys_ctx)
        self.batt = BatteryController(self.ctl, self.cfg)
        self.active = self.cfg.get("last_preset")
        self.info, self.batt_info = None, {}
        self.running = True
        self.wmi_rpm, self.wmi_temp = [], []    # méthodes Get du firmware qui donnent tr/min et °C
        self.notify = lambda title, msg, warn=False: None      # branché sur l'icône de notification
        self.ui_active = lambda: True                           # l'interface est-elle ouverte ?
        self.peak = {"cpu": None, "gpu": None, "since": time.time()}
        self.stress = StressTest(self)
        self._low_batt = False

    def max_temp(self):
        v = [t for t in (self.state.get("cpu_temp"), self.state.get("gpu_temp"),
                         (self.state.get("gpu") or {}).get("temp")) if t is not None]
        return max(v) if v else None

    def sys_ctx(self):
        s = self.state
        return {"cpu": s.get("cpu"), "ram": s.get("ram"), "temp": self.max_temp(),
                "gpu": (s.get("gpu") or {}).get("util")}

    # -- démarrage / fond
    def start(self):
        threading.Thread(target=self._monitor, daemon=True).start()
        threading.Thread(target=self._boot, daemon=True).start()
        threading.Thread(target=self._maintain, daemon=True).start()
        threading.Thread(target=self._fan_loop, daemon=True).start()
        AutoSwitcher(lambda: self.cfg, self._auto_preset).start()

    def _auto_preset(self, key):
        ok, m = self.apply_preset(key, auto=True)
        p = all_presets(self.cfg).get(key) or {}
        if not self.ui_active():
            self.notify("Profil automatique", f"Preset « {p.get('label', key)} » appliqué")
        return ok, m

    def _boot(self):
        try:
            self.kb.ensure()
        except Exception as e:  # noqa: BLE001
            LOG.add('err', f'clavier · {e}')
        ok, info = self.ctl.request("info", timeout=30)
        self.info = info if ok else {"error": str(info)}
        if ok:
            cl = info.get("classes", {})
            LOG.add("ok" if cl.get("GB_WMIACPI_Set") is True else "err",
                    f"WMI · modèle {info.get('model')} · GB_WMIACPI_Set={cl.get('GB_WMIACPI_Set')} · "
                    f"RW_GMWMI={cl.get('RW_GMWMI')} · {len(info.get('methods', []))} méthodes")
        else:
            LOG.add("err", f"WMI · {info}")
        self.batt_info = self.batt.read() if ok else {}
        if ok:
            self._find_wmi_sensors(info.get("getmethods") or [])
        if self.cfg.get("apply_on_start") and ok:
            self.reapply()

    def reapply(self):
        if self.cfg["fan"].get("set"):
            self.fan.apply(self.cfg["fan"]["mode"], self.cfg["fan"]["pct"])
        if self.cfg["keyboard"].get("set"):
            self.kb.apply()
        if self.cfg["battery"]["limit_on"]:
            self.batt.set_limit(True, self.cfg["battery"]["limit"])

    def _maintain(self):
        """Réapplique ventilos/clavier après une veille (le firmware les remet à zéro)."""
        last = time.time()
        while self.running:
            time.sleep(5)
            now = time.time()
            if now - last > 20 and self.cfg.get("apply_on_start"):
                LOG.add("ok", "reprise après veille détectée → réapplication")
                time.sleep(3)
                self.reapply()
            last = now

    def _find_wmi_sensors(self, methods):
        """Repère les méthodes de lecture sans paramètre (ex. GetRpm1, GetCpuTemp) pour se passer de LibreHardwareMonitor."""
        free = [m.split("(")[0] for m in methods if m.endswith("()")]
        self.wmi_rpm = [m for m in free if re.search(r"rpm|fan\d*speed|speed\d", m, re.I)
                        and not re.search(r"duty|status|index|step|adjust|fixed|target", m, re.I)][:3]
        self.wmi_temp = [m for m in free if re.search(r"temp", m, re.I)
                         and not re.search(r"target|status|index|thermal|limit", m, re.I)][:3]
        if self.wmi_rpm or self.wmi_temp:
            LOG.add("info", f"capteurs du firmware : ventilos {self.wmi_rpm or '—'} · températures {self.wmi_temp or '—'}")

    def _wmi_read(self, m):
        ok, d = self.mon.request("call", cls=GET, method=m, timeout=4)
        if not ok:
            return None
        vals = [v for k, v in (d.items() if isinstance(d, dict) else []) if k.lower() != "returnvalue" and isinstance(v, (int, float))]
        return vals[0] if vals else None

    def _wmi_sensors(self):
        s = self.state
        if not s.get("fans"):
            fans = []
            for m in list(self.wmi_rpm):
                v = self._wmi_read(m)
                if v is None or not 0 <= v < 12000:
                    self.wmi_rpm.remove(m)
                    LOG.add("warn", f"capteurs du firmware · {m} illisible, ignoré")
                    continue
                n = re.sub(r"^get", "", m, flags=re.I)
                fans.append({"n": "Ventilateur " + (re.findall(r"\d+", n) or [str(len(fans) + 1)])[0] if re.search(r"\d", n) else n, "rpm": int(v)})
            if fans:
                s["fans"] = fans
        for m in list(self.wmi_temp):
            key = "gpu_temp" if "gpu" in m.lower() else "cpu_temp"
            if s.get(key) is not None and s.get("sens_src") != "Firmware Gigabyte":
                continue
            v = self._wmi_read(m)
            if v is None or not 1 < v < 130:
                self.wmi_temp.remove(m)
                continue
            s[key], s["sens_src"] = float(v), "Firmware Gigabyte"

    def _fan_loop(self):
        """Mode « Ma courbe » + garde thermique (ventilos à 100 % si ça chauffe trop)."""
        hot = cool = 0
        while self.running:
            time.sleep(2)
            try:
                t, f, g = self.max_temp(), self.fan, self.cfg.get("guard") or {}
                if f.override == "guard":
                    if not g.get("enabled"):
                        cool = 99
                    else:
                        cool = cool + 1 if t is not None and t <= g.get("temp", 93) - 8 else 0
                    if cool >= 10:
                        cool, f.override, f.curve_pct = 0, None, None
                        LOG.add("ok", "garde thermique · température redescendue → retour au mode "
                                + FAN_MODES[f.mode]["label"])
                        if f.mode != "curve":
                            f.apply(f.mode, f.pct, persist=False, quiet=True)
                    continue
                if g.get("enabled") and t is not None and f.mode != "turbo":
                    hot = hot + 1 if t >= g.get("temp", 93) else 0
                    if hot >= 2:
                        hot = 0
                        ok, _m = f.apply("turbo", persist=False, quiet=True)
                        if ok:
                            f.override = "guard"
                            LOG.add("warn", f"garde thermique · {t:.0f} °C ≥ {g.get('temp')} °C → ventilos à 100 %")
                            self.notify("Garde thermique", f"{t:.0f} °C : ventilateurs à 100 % jusqu'à ce que ça refroidisse", True)
                        continue
                if f.mode == "curve" and f.override is None:
                    f.curve_tick(t)
            except Exception as e:  # noqa: BLE001
                LOG.add("warn", f"ventilos · boucle : {e}")

    def _procs(self):
        n, alive, groups = self.state["cores"], {}, {}
        me = os.getpid()
        for p in psutil.process_iter(["name"]):
            if p.pid in (0, 4, me):
                continue
            q = self._pcache.get(p.pid, p)
            try:
                c, m = q.cpu_percent(None) / n, q.memory_info().rss
            except (psutil.Error, OSError):
                continue
            alive[p.pid] = q
            name = p.info.get("name") or "?"
            g = groups.setdefault(name, [0.0, 0, 0])
            g[0] += c
            g[1] += m
            g[2] += 1
        self._pcache = alive
        top = sorted(groups.items(), key=lambda kv: -kv[1][0])[:7]
        self.procs = [{"n": k, "cpu": round(v[0], 1), "mem": round(v[1] / 2**20), "k": v[2],
                       "safe": k.lower() not in PROTECTED} for k, v in top]

    def _monitor(self):
        psutil.cpu_percent(None)
        drive = (os.environ.get("SystemDrive", "C:") + "\\") if IS_WIN else "/"
        while self.running:
            try:
                f, vm = psutil.cpu_freq(), psutil.virtual_memory()
                s = self.state
                s.update(cpu=psutil.cpu_percent(None), freq=f.current if f else None, ram=vm.percent,
                         ram_used=vm.used / 2**30, ram_total=vm.total / 2**30,
                         batt=self._batt(), uptime=time.time() - psutil.boot_time())
                if self.ui_active() or self._tick % 2 == 0:   # fenêtre fermée : on interroge le GPU 2× moins souvent
                    s["gpu"] = self.gpu.read()
                io, now = psutil.net_io_counters(), time.time()
                if io and self._net:
                    dt = max(0.1, now - self._net[0])
                    s["net"] = {"up": max(0, io.bytes_sent - self._net[1]) / dt,
                                "down": max(0, io.bytes_recv - self._net[2]) / dt}
                if io:
                    self._net = (now, io.bytes_sent, io.bytes_recv)
                if self._tick % 20 == 0:
                    du = psutil.disk_usage(drive)
                    s["disk"] = {"used": du.used / 2**30, "total": du.total / 2**30, "pct": du.percent, "path": drive}
                ui = self.ui_active()
                if self._tick % 2 == 0 and (ui or self.stress.running):
                    self._procs()
                self._tick += 1
                ok, d = self.mon.request("sensors", timeout=10)
                if ok and isinstance(d, dict):
                    self._sensors(d)
                if self.wmi_rpm or self.wmi_temp:
                    self._wmi_sensors()
                g = s.get("gpu") or {}
                fans = [x["rpm"] for x in s.get("fans") or []]
                gt = s.get("gpu_temp") if s.get("gpu_temp") is not None else g.get("temp")
                for k2, v in (("cpu", s.get("cpu_temp")), ("gpu", gt)):
                    if v is not None and (self.peak[k2] is None or v > self.peak[k2]):
                        self.peak[k2] = v
                b = s.get("batt")
                if b and not b["plugged"] and b["pct"] <= 15 and not self._low_batt:
                    self._low_batt = True
                    self.notify("Batterie faible", f"{b['pct']:.0f} % restants : branche le chargeur ou passe en preset Autonomie", True)
                elif b and (b["plugged"] or b["pct"] > 20):
                    self._low_batt = False
                self.history.append([round(now), round(s["cpu"]), s.get("cpu_temp"),
                                     s.get("gpu_temp") if s.get("gpu_temp") is not None else g.get("temp"),
                                     g.get("util"), max(fans) if fans else None, self.fan.effective_pct()])
            except Exception as e:  # le monitoring ne doit jamais s'arrêter
                LOG.add("warn", f"monitor · {e}")
            time.sleep(1.5)

    def _batt(self):
        b = psutil.sensors_battery()
        if not b:
            return None
        return {"pct": b.percent, "plugged": b.power_plugged,
                "left": b.secsleft if b.secsleft and b.secsleft > 0 else None}

    def _sensors(self, d):
        s = self.state
        lhm = d.get("lhm") or []
        if isinstance(lhm, dict):
            lhm = [lhm]
        cpu, gpu, fans = [], None, []
        for x in lhm:
            ident = (x.get("id") or "").lower()
            if x["t"] == "Temperature" and "cpu" in ident:
                cpu.append((x["n"], x["v"]))
            elif x["t"] == "Temperature" and "gpu" in ident:
                gpu = x["v"]
            elif x["t"] == "Fan":
                fans.append({"n": x["n"], "rpm": int(x["v"])})
        if cpu:
            pkg = [v for n, v in cpu if "package" in n.lower()]
            s["cpu_temp"], s["sens_src"] = (pkg[0] if pkg else max(v for _, v in cpu)), "LibreHardwareMonitor"
        else:
            ac = d.get("acpi")
            if ac:
                ac = ac if isinstance(ac, list) else [ac]
                s["cpu_temp"], s["sens_src"] = max(ac), "ACPI"
        s["gpu_temp"], s["fans"] = gpu, fans

    # -- commandes
    def apply_preset(self, key, auto=False):
        p = all_presets(self.cfg).get(key)
        if not p:
            return False, "preset inconnu"
        res = self.power.apply(p)
        g_ok, g_msg = self.gpu.set_power_pct(p["gpu_pct"]) if self.gpu.exe else (False, "GPU NVIDIA non détecté")
        res.append(("Limite GPU", g_ok, g_msg))
        f_ok, f_msg = self.fan.apply(p.get("fan", "normal"), p.get("fan_pct", 60))
        res.append(("Ventilateurs", f_ok, f_msg))
        self.active = key
        if not key.startswith("custom:__"):
            self.cfg["last_preset"] = key
        save_cfg(self.cfg)
        failed = [f"{n} ({m})" if m else n for n, ok, m in res if not ok and n != "Mode Windows"]
        LOG.add("warn" if failed else "ok", f"preset · {p['label']}{' [auto]' if auto else ''}"
                + (f" · échecs : {', '.join(failed)}" if failed else ""))
        return not failed, ("OK" if not failed else "Échecs : " + ", ".join(failed))

    def snapshot(self, log_since=0):
        s = dict(self.state)
        k = self.cfg["keyboard"]
        return {
            "t": time.time(), "admin": is_admin(), "win": IS_WIN, "s": s,
            "info": self.info, "batt_info": self.batt_info,
            "fan": {"mode": self.fan.mode, "pct": self.fan.pct, "ok": self.fan.last_ok, "msg": self.fan.last_msg,
                    "modes": {k2: {"label": v["label"], "desc": v["desc"]} for k2, v in FAN_MODES.items()},
                    "curve": self.cfg["fan_curve"]["points"], "curve_pct": self.fan.curve_pct,
                    "override": self.fan.override, "eff": self.fan.effective_pct()},
            "guard": self.cfg.get("guard"), "procs": self.procs, "peak": self.peak, "stress": self.stress.snapshot(),
            "background": self.cfg.get("background", True), "hist": self.history[-1] if self.history else None,
            "kb": {"zones": k["zones"], "level": k["level"], "effect": k["effect"], "speed": k["speed"],
                   "flip": k["flip"], "preview": self.kb.preview,
                   "effects": KB_EFFECTS_HID if self.kb.backend == "hid" else KB_EFFECTS,
                   "backend": self.kb.backend, "hwcolor": k.get("hwcolor", 8), "gain": k.get("gain", fusionhid.GAIN_GIGABYTE),
                   "keys": k.get("keys") or {}},
            "battery": self.cfg["battery"],
            "presets": {key: {kk: vv for kk, vv in p.items() if kk in ("label", "icon", "color", "desc", "fan", "fan_pct",
                                                                   "cpu_max", "cpu_min", "gpu_pct", "boost", "overlay")}
                        for key, p in all_presets(self.cfg).items()},
            "active": self.active,
            "custom": self.cfg.get("custom_presets", {}),
            "auto": self.cfg["auto"], "apply_on_start": self.cfg.get("apply_on_start", True),
            "gpu_exe": self.gpu.exe, "cfg_file": str(CFG_FILE),
            "log": LOG.tail(log_since),
        }

    def shutdown(self):
        self.running = False
        self.stress.stop()
        if self.fan.mode == "curve" or self.fan.override:
            # la courbe est pilotée par l'appli : on rend la main au firmware avant de partir
            self.fan.apply("normal", persist=False, quiet=True)
            LOG.add("info", "ventilos rendus au firmware (Normal) à la fermeture")
        self.kb.shutdown()
        self.ctl.stop()
        self.mon.stop()


# ----------------------------------------------------------------------------- test de refroidissement
def stress_worker(seconds):
    """Boucle de calcul pure (lancée dans un processus séparé par cœur)."""
    end, x = time.time() + max(5, min(400, seconds)), 1.0001
    while time.time() < end:
        for _ in range(20000):
            x = (x * 1.0000001) % 1e9 + math.sqrt(x)


class StressTest:
    """Charge tous les cœurs, mesure la montée en température, puis la redescente."""
    COOL = 45

    def __init__(self, hub):
        self.hub, self.procs, self.running, self.lock = hub, [], False, threading.Lock()
        self.st = {"phase": "idle"}

    def snapshot(self):
        return dict(self.st)

    def start(self, dur):
        dur = int(max(20, min(300, dur)))
        with self.lock:
            if self.running:
                return False, "Un test est déjà en cours"
            base = [sys.executable] if FROZEN else [sys.executable, os.path.abspath(sys.argv[0])]
            n = psutil.cpu_count() or 4
            try:
                for _ in range(n):
                    self.procs.append(subprocess.Popen(base + ["--stress", str(dur + 3)], creationflags=NO_WINDOW | (0x4000 if IS_WIN else 0),
                                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            except OSError as e:
                self._kill()
                return False, f"Impossible de lancer le test : {e}"
            self.running = True
        threading.Thread(target=self._run, args=(dur,), daemon=True).start()
        LOG.add("info", f"test de refroidissement · {n} cœurs chargés pendant {dur} s")
        return True, f"Test lancé sur {n} cœurs pendant {dur} s"

    def _kill(self):
        for p in self.procs:
            try:
                p.kill()
            except OSError:
                pass
        self.procs = []

    def stop(self):
        if self.running:
            self._kill()
            self.st["phase"] = "stopped"
            self.running = False

    def _run(self, dur):
        h, t0 = self.hub, time.time()
        temp0 = h.max_temp()
        pts, freqs, rpms = [], [], []
        self.st = {"phase": "load", "dur": dur, "cool": self.COOL, "t": 0, "start": temp0, "peak": temp0, "pts": []}
        while self.running and time.time() - t0 < dur + self.COOL:
            el = time.time() - t0
            if el >= dur and self.procs:
                self._kill()
                self.st.update(phase="cool", end_load=h.max_temp())
            t = h.max_temp()
            if t is not None:
                pts.append([round(el, 1), round(t, 1)])
                if self.st.get("peak") is None or t > self.st["peak"]:
                    self.st["peak"], self.st["t_peak"] = t, round(el)
            if el < dur:
                f = psutil.cpu_freq()
                if f:
                    freqs.append(f.current)
                rpm = [x["rpm"] for x in h.state.get("fans") or []]
                if rpm:
                    rpms.append(max(rpm))
            self.st.update(t=round(el), pts=pts[-200:], cur=t)
            time.sleep(1)
        if not self.running:
            return
        self._kill()
        peak, end = self.st.get("peak"), h.max_temp()
        drop = (peak - end) if peak is not None and end is not None else None
        fmax = max(freqs) if freqs else None
        fend = sum(freqs[-10:]) / len(freqs[-10:]) if freqs else None
        thr = bool(fmax and fend and fend < fmax * .85)
        verdict = ("Températures inconnues : lance LibreHardwareMonitor pour un vrai résultat" if peak is None else
                   "Excellent : ton refroidissement suit sans effort" if peak < 82 else
                   "Correct : ça chauffe mais ça reste dans les clous" if peak < 92 else
                   "Chaud : essaie une courbe plus agressive, le preset Turbo ou un nettoyage des ventilateurs")
        self.st.update(phase="done", end=end, drop=drop, fmax=fmax, fend=fend, throttle=thr, rpm=max(rpms) if rpms else None,
                       verdict=verdict + (" (le processeur a ralenti pour se protéger)" if thr else ""))
        self.running = False
        LOG.add("ok", f"test de refroidissement · pic {peak if peak is None else round(peak)} °C · {verdict}")
        if not h.ui_active():
            h.notify("Test de refroidissement terminé", f"Pic à {peak:.0f} °C. {verdict}" if peak is not None else verdict)


# ----------------------------------------------------------------------------- profils automatiques
class AutoSwitcher(threading.Thread):
    def __init__(self, get_cfg, request):
        super().__init__(daemon=True)
        self.get_cfg, self.request, self._last = get_cfg, request, None

    @staticmethod
    def desired(cfg):
        a = cfg["auto"]
        names = {(p.info.get("name") or "").lower() for p in psutil.process_iter(["name"])}
        for r in a.get("rules", []):
            if r["exe"].lower() in names:
                return r["preset"]
        b = psutil.sensors_battery()
        if b is not None:
            return (a.get("ac_preset") if b.power_plugged else a.get("dc_preset")) or None
        return None

    def run(self):
        while True:
            time.sleep(3)
            try:
                cfg = self.get_cfg()
                if not cfg["auto"].get("enabled"):
                    self._last = None
                    continue
                d = self.desired(cfg)
                if d and d != self._last:
                    self._last = d
                    self.request(d)
                elif not d:
                    self._last = None
            except Exception:
                pass
