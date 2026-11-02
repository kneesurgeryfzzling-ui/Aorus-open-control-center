"""AORUS Open Control Center v13 — lanceur + serveur local (127.0.0.1, protégé par jeton).

Tourne en arrière-plan avec une icône près de l'horloge : fermer la fenêtre ne coupe pas la courbe des
ventilateurs, la garde thermique, les profils automatiques ni les effets du clavier.
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

if "--stress" in sys.argv:                     # processus de charge du test de refroidissement
    import math  # noqa: F401
    from backends import stress_worker
    stress_worker(float(sys.argv[sys.argv.index("--stress") + 1]))
    sys.exit(0)

import backends as B

TOKEN = secrets.token_urlsafe(16)
STATE = {"seen": False, "last": time.time(), "quit": threading.Event()}
HUB: "B.Hub"
TRAY = None
URL = ""
INSTANCE = B.APP_DIR / "instance.json"


def ok(msg="OK", **kw):
    return dict(ok=True, msg=msg, **kw)


def err(msg):
    return dict(ok=False, msg=str(msg))


def report():
    h = HUB
    lines = ["=== AORUS Open CC · rapport ===", time.strftime("%Y-%m-%d %H:%M:%S"),
             f"admin={B.is_admin()} frozen={B.FROZEN} nvidia-smi={h.gpu.exe}",
             f"info={json.dumps(h.info, ensure_ascii=False)}", f"config={json.dumps(h.cfg, ensure_ascii=False)}"]
    okk, t = h.fan.read_table()
    lines.append("courbe interne (index, temp, valeur) : " + (str(t) if okk else f"illisible ({t})"))
    okk, kb = h.ctl.request("kbget")
    lines.append("clavier RW_GMWMI.BufferBytes : " + (str(kb) if okk else f"illisible ({kb})"))
    try:
        import fusionhid
        lines.append(f"clavier : backend={h.kb.backend} · HID Gigabyte (VID 1044) : {fusionhid.scan()}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"clavier HID : scan impossible ({e})")
    lines.append("--- journal ---")
    lines += [f"{e['t']} [{e['k']}] {e['m']}" for e in B.LOG.tail(0)]
    text = "\n".join(lines)
    try:
        (B.OUT_DIR / "rapport_aorus.txt").write_text(text, "utf-8")
    except OSError:
        pass
    return text


def handle(action, d):
    h = HUB
    if action == "fan":
        if d["mode"] == "curve" and h.max_temp() is None:
            return err("Températures indisponibles : « Ma courbe » a besoin d'un capteur "
                       "(LibreHardwareMonitor lancé ou GPU NVIDIA détecté)")
        okk, m = h.fan.apply(d["mode"], d.get("pct"))
        return ok(m) if okk else err(m)
    if action == "fan_curve":
        try:
            h.cfg["fan_curve"]["points"] = B.clean_curve(d["points"])
        except (ValueError, TypeError, KeyError) as e:
            return err(f"Courbe refusée : {e}")
        B.save_cfg(h.cfg)
        h.fan.curve_t = 0
        return ok("Courbe enregistrée")
    if action == "guard":
        g = h.cfg.setdefault("guard", {"enabled": True, "temp": 93})
        if "enabled" in d:
            g["enabled"] = bool(d["enabled"])
        if "temp" in d:
            g["temp"] = int(max(75, min(100, int(d["temp"]))))
        B.save_cfg(h.cfg)
        return ok("Garde thermique " + (f"active à {g['temp']} °C" if g["enabled"] else "désactivée"))
    if action == "kill":
        okk, m = B.kill_by_name(str(d.get("name", "")))
        return ok(m) if okk else err(m)
    if action == "export_cfg":
        c = {k: v for k, v in h.cfg.items() if k != "scheme_guid"}
        return ok("Configuration exportée", cfg=c)
    if action == "import_cfg":
        src = d.get("cfg")
        if not isinstance(src, dict) or "keyboard" not in src or "fan" not in src:
            return err("Ce fichier n'est pas une configuration AORUS Open CC")
        for k, v in src.items():
            if k not in B.DEFAULT_CFG or k == "scheme_guid":
                continue
            if isinstance(v, dict) and isinstance(h.cfg.get(k), dict) and k != "custom_presets":
                h.cfg[k].update(v)
            else:
                h.cfg[k] = v
        B.save_cfg(h.cfg)
        threading.Thread(target=h.reapply, daemon=True).start()
        return ok("Configuration importée et réappliquée")
    if action == "preset":
        okk, m = h.apply_preset(d["key"])
        return ok(m) if okk else err(m)
    if action == "kb":
        k = h.cfg["keyboard"]
        for key in ("zones", "level", "effect", "speed", "flip", "hwcolor", "gain", "keys"):
            if key in d:
                k[key] = d[key]
        k["level"] = max(0, min(2, int(k["level"])))
        k["set"] = True
        B.save_cfg(h.cfg)
        okk = h.kb.apply()
        return ok() if okk else err("Clavier refusé : " + (h.kb.last_err or "voir le journal")[:400])
    if action == "kb_flash":
        return ok() if h.kb.flash() else err("aucun effet animé")
    if action == "stress":
        okk, m = h.stress.start(int(d.get("dur", 60))) if d.get("on", True) else (h.stress.stop() or True, "Test arrêté")
        return ok(m) if okk else err(m)
    if action == "background":
        h.cfg["background"] = bool(d.get("on"))
        B.save_cfg(h.cfg)
        return ok("L'application restera active en arrière-plan" if h.cfg["background"] else
                  "L'application se fermera avec sa fenêtre")
    if action == "kb_test":
        okk = h.kb.test()
        return ok("Test envoyé : rouge → vert → bleu → 3 zones") if okk else err("Clavier refusé : " + (h.kb.last_err or "voir le journal")[:400])
    if action == "battery":
        okk, m = h.batt.set_limit(d["on"], d.get("pct", 80))
        return ok(m) if okk else err(m)
    if action == "boost":
        okk, m = h.batt.set_boost(d["kind"], d["on"])
        return ok(m) if okk else err(m)
    if action == "custom_save":
        name, p = d["name"].strip(), d["params"]
        if not name:
            return err("Nom vide")
        h.cfg["custom_presets"][name] = {k: p[k] for k in
                                         ("cpu_max", "cpu_min", "boost", "cooling", "gpu_pct", "overlay", "fan", "fan_pct")}
        B.save_cfg(h.cfg)
        return ok("Enregistré")
    if action == "custom_apply":
        p = d["params"]
        h.cfg["custom_presets"]["__tmp"] = {k: p[k] for k in
                                            ("cpu_max", "cpu_min", "boost", "cooling", "gpu_pct", "overlay", "fan", "fan_pct")}
        okk, m = h.apply_preset("custom:__tmp")
        del h.cfg["custom_presets"]["__tmp"]
        return ok(m) if okk else err(m)
    if action == "custom_delete":
        h.cfg["custom_presets"].pop(d["name"], None)
        B.save_cfg(h.cfg)
        return ok("Supprimé")
    if action == "auto":
        h.cfg["auto"].update({k: d[k] for k in ("enabled", "ac_preset", "dc_preset", "rules") if k in d})
        B.save_cfg(h.cfg)
        return ok()
    if action == "config":
        h.cfg["apply_on_start"] = bool(d.get("apply_on_start"))
        B.save_cfg(h.cfg)
        return ok()
    if action == "autostart":
        rc, out = B.set_autostart(bool(d["on"]))
        return ok() if rc == 0 else err(out)
    if action == "fan_table":
        okk, t = h.fan.read_table()
        if okk:
            B.LOG.add("ok", "courbe interne : " + " · ".join(f"#{i}: {tp}°→{v}" for i, tp, v in t))
        return ok("Lu", table=t) if okk else err(t)
    if action == "ping":
        okk, r = h.ctl.request("ping", timeout=8)
        return ok(str(r)) if okk else err(r)
    if action == "report":
        return ok("Rapport enregistré", text=report(), path=str(B.OUT_DIR / "rapport_aorus.txt"))
    if action == "bye":
        STATE["last"] = time.time() - 20
        return ok()
    if action == "quit":
        STATE["quit"].set()
        return ok()
    return err("action inconnue")


class Handler(BaseHTTPRequestHandler):
    server_version = "AorusCC"

    def log_message(self, *a):
        pass

    def _ok_host(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if not self._ok_host():
            return self._send(403, "{}")
        if u.path == "/intro.mp4":
            if parse_qs(u.query).get("t", [""])[0] != TOKEN:
                return self._send(403, "{}")
            try:
                return self._send(200, (B.RES_DIR / "ui" / "intro.mp4").read_bytes(), "video/mp4")
            except OSError:
                return self._send(404, "{}")
        if u.path == "/":
            if parse_qs(u.query).get("t", [""])[0] != TOKEN:
                return self._send(403, "Lien invalide — relancez l'application.", "text/plain; charset=utf-8")
            html = (B.RES_DIR / "ui" / "index.html").read_text("utf-8").replace("__TOKEN__", TOKEN)
            return self._send(200, html, "text/html; charset=utf-8")
        if self.headers.get("X-Token") != TOKEN:
            return self._send(403, "{}")
        if u.path == "/api/kbframe":
            return self._send(200, json.dumps({"k": HUB.kb.keyframe, "b": HUB.kb.backend}))
        if u.path == "/api/alive":
            return self._send(200, "{}")
        if u.path == "/api/history":
            return self._send(200, json.dumps(list(HUB.history)))
        if u.path == "/api/kblayout":
            import fusionhid
            return self._send(200, json.dumps({"w": fusionhid.WIDTH, "h": fusionhid.HEIGHT, "keys": fusionhid.LAYOUT}))
        if u.path == "/api/state":
            STATE["seen"], STATE["last"] = True, time.time()
            since = int(parse_qs(u.query).get("log", ["0"])[0] or 0)
            return self._send(200, json.dumps(HUB.snapshot(since), ensure_ascii=False))
        self._send(404, "{}")

    def do_POST(self):
        if not self._ok_host() or self.headers.get("X-Token") != TOKEN:
            return self._send(403, "{}")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
            STATE["last"] = time.time()
            res = handle(urlparse(self.path).path.rsplit("/", 1)[-1], d)
        except Exception as e:
            B.LOG.add("err", f"API · {e}")
            res = err(f"{type(e).__name__}: {e}")
        self._send(200, json.dumps(res, ensure_ascii=False))


def ui_active():
    return STATE["seen"] and time.time() - STATE["last"] < 12


def tray_menu():
    h = HUB
    pres = [(p["label"], "preset:" + k, h.active == k) for k, p in B.all_presets(h.cfg).items() if not k.startswith("custom:__")]
    fans = [(m["label"], "fan:" + k, h.fan.mode == k) for k, m in B.FAN_MODES.items() if k not in ("fixed", "cap")]
    lv = h.cfg["keyboard"]["level"]
    t = h.max_temp()
    return [("Ouvrir AORUS Open CC", "open"),
            None,
            (f"Température max : {t:.0f} °C" if t is not None else "Température : inconnue", "open"),
            ("Preset", pres), ("Ventilateurs", fans),
            ("Clavier : éteindre" if lv else "Clavier : allumer", "kb"),
            ("Garde thermique", "guard", bool(h.cfg.get("guard", {}).get("enabled"))),
            None,
            ("Quitter", "quit")]


def tray_command(cmd):
    h = HUB
    if cmd == "open":
        open_window(URL)
    elif cmd == "quit":
        STATE["quit"].set()
    elif cmd.startswith("preset:"):
        h.apply_preset(cmd[7:])
    elif cmd.startswith("fan:"):
        if cmd[4:] == "curve" and h.max_temp() is None:
            return TRAY and TRAY.notify("Ma courbe", "Températures indisponibles : lance LibreHardwareMonitor", True)
        h.fan.apply(cmd[4:])
    elif cmd == "kb":
        k = h.cfg["keyboard"]
        k["level"] = 0 if k["level"] else 2
        B.save_cfg(h.cfg)
        h.kb.apply()
    elif cmd == "guard":
        g = h.cfg.setdefault("guard", {"enabled": True, "temp": 93})
        g["enabled"] = not g.get("enabled")
        B.save_cfg(h.cfg)


def other_instance():
    """Si l'appli tourne déjà, on rouvre simplement sa fenêtre."""
    try:
        d = json.loads(INSTANCE.read_text("utf-8"))
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{d['port']}/api/alive", headers={"X-Token": d["token"]})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                return d
    except Exception:  # noqa: BLE001
        return None
    return None


def open_window(url, size=(1320, 860)):
    cands = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands += [os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"),
                      os.path.join(base, r"Google\Chrome\Application\chrome.exe")]
    prof = str(B.APP_DIR / "webview")
    for exe in cands:
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", f"--window-size={size[0]},{size[1]}", "--autoplay-policy=no-user-gesture-required",
                                  f"--user-data-dir={prof}", "--no-first-run", "--disable-extensions"])
                return True
            except OSError:
                continue
    return webbrowser.open(url)


def main():
    global HUB, TRAY, URL
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--fake", help="script simulant le firmware (tests)")
    ap.add_argument("--no-admin", action="store_true")
    ap.add_argument("--no-tray", action="store_true")
    a = ap.parse_args()
    if a.fake:
        os.environ["AORUS_FAKE_SERVICE"] = a.fake
    other = None if a.fake else other_instance()
    if other:                                   # déjà lancée : on rouvre la fenêtre et on s'arrête là
        if not a.no_window:
            open_window(f"http://127.0.0.1:{other['port']}/?t={other['token']}")
        return
    if B.IS_WIN and not a.no_admin and not B.is_admin():
        B.relaunch_as_admin()
    HUB = B.Hub()
    HUB.ui_active = ui_active
    HUB.start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    URL = f"http://127.0.0.1:{port}/?t={TOKEN}"
    try:
        print(URL, flush=True)
    except (AttributeError, OSError):   # pythonw : pas de console
        pass
    if not a.fake:
        try:
            B.APP_DIR.mkdir(parents=True, exist_ok=True)
            INSTANCE.write_text(json.dumps({"port": port, "token": TOKEN, "pid": os.getpid()}), "utf-8")
        except OSError:
            pass
    if B.IS_WIN and not a.no_tray:
        try:
            import tray
            TRAY = tray.Tray(B.RES_DIR / "ui" / "aorus.ico", "AORUS Open Control Center",
                             lambda: open_window(URL), tray_menu, tray_command)
            HUB.notify = TRAY.notify
        except Exception as e:  # noqa: BLE001
            B.LOG.add("warn", f"icône de notification indisponible : {e}")
    if not a.no_window:
        open_window(URL)
    t0, told = time.time(), 0
    while not STATE["quit"].is_set():
        time.sleep(1)
        if HUB.cfg.get("background", True) and (TRAY and TRAY.ok or a.no_window or a.fake):
            if TRAY and time.time() - told > 5:     # infobulle de l'icône : état en direct
                told = time.time()
                t = HUB.max_temp()
                fm = B.FAN_MODES.get(HUB.fan.mode, {}).get("label", "")
                p = B.all_presets(HUB.cfg).get(HUB.active, {}).get("label", "aucun preset")
                TRAY.set_tip(f"AORUS · {t:.0f} °C · {p} · ventilos {fm}" if t is not None else f"AORUS · {p} · ventilos {fm}")
            continue
        idle = time.time() - STATE["last"]
        if (STATE["seen"] and idle > 25) or (not STATE["seen"] and time.time() - t0 > 90):
            break
    HUB.shutdown()
    if TRAY:
        TRAY.stop()
    try:
        if json.loads(INSTANCE.read_text("utf-8")).get("pid") == os.getpid():
            INSTANCE.unlink()
    except (OSError, ValueError):
        pass
    srv.shutdown()


if __name__ == "__main__":
    main()
