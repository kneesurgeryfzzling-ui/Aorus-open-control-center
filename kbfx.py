"""Moteur d'effets touche par touche (ripple, tank, feux d'artifice, matrix, orage, carte de chaleur, snake,
sable, vagues, plasma, ciel étoilé, tableau de bord système).

Chaque effet reçoit le temps, le delta, et la liste des touches *nouvellement pressées* ; il renvoie
{nom_de_touche: (r, g, b)}. Les touches absentes du dictionnaire sont éteintes.
"""
import colorsys
import math
import random
import sys

import fusionhid as H

IS_WIN = sys.platform == "win32"
W, HGT = H.WIDTH, H.HEIGHT
KEYS = [(n, *H.POS[n]) for n in H.POS]
NAMES = ["ripple", "tank", "fireworks", "matrix", "storm", "rain", "heat", "snake", "sand", "waves", "plasma", "starlight", "hud"]
REACTIVE = {"ripple", "tank", "fireworks", "matrix", "storm", "heat", "snake"}

# ----------------------------------------------------------------------------- lecture des touches (effets réactifs)
_VK = {"esc": 0x1B, "tab": 9, "caps": 0x14, "lshift": 0xA0, "rshift": 0xA1, "lctrl": 0xA2, "rctrl": 0xA3, "lalt": 0xA4,
       "ralt": 0xA5, "win": 0x5B, "app": 0x5D, "space": 0x20, "enter": 0x0D, "backspace": 8, "del": 0x2E, "home": 0x24,
       "end": 0x23, "pgup": 0x21, "pgdn": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "pause": 0x13,
       "numlk": 0x90, "numberslash": 0x6F, "asterisk": 0x6A, "minussign": 0x6D, "plussign": 0x6B, "numberdel": 0x6E,
       "graveaccent": 0xC0, "hyphenminus": 0xBD, "equalsign": 0xBB, "lsquarebracket": 0xDB, "rsquarebracket": 0xDD,
       "backslash": 0xDC, "semicolon": 0xBA, "apostrophe": 0xDE, "comma": 0xBC, "fullstop": 0xBE, "slash": 0xBF}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _VK[_c] = ord(_c.upper())
for _d in "0123456789":
    _VK[_d] = ord(_d)
for _i in range(1, 13):
    _VK[f"f{_i}"] = 0x6F + _i
for _i in range(10):
    _VK[f"number{_i}"] = 0x60 + _i
_VK = {k: v for k, v in _VK.items() if k in H.POS}


class KeyPoll:
    """Détecte les appuis via GetAsyncKeyState (pendant l'effet uniquement ; rien n'est enregistré)."""

    def __init__(self):
        self.prev = set()
        self.fn = None
        if IS_WIN:
            import ctypes
            self.fn = ctypes.windll.user32.GetAsyncKeyState

    def new(self):
        if not self.fn:
            return []
        down, out = set(), []
        for n, vk in _VK.items():
            st = self.fn(vk)
            if st & 0x8000:
                down.add(n)
                if n not in self.prev or st & 1:
                    out.append(n)
        self.prev = down
        return out


# ----------------------------------------------------------------------------- utilitaires
def hsv(h, s=1.0, v=1.0):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0, min(1, s)), max(0, min(1, v)))
    return (r * 255, g * 255, b * 255)


def mix(a, b, k):
    return tuple(a[i] * (1 - k) + b[i] * k for i in range(3))


def scale(c, k):
    return tuple(v * k for v in c)


def plus(a, b):
    return tuple(a[i] + b[i] for i in range(3))


def dist(x1, y1, x2, y2):
    return math.hypot(x1 - x2, y1 - y2)


def ramp(stops, v):
    """Dégradé multi-couleurs : stops = [(position, (r, g, b)), …]."""
    v = max(0.0, min(1.0, v))
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if v <= p1:
            return mix(c0, c1, (v - p0) / max(1e-6, p1 - p0))
    return stops[-1][1]


HEAT = [(0, (0, 0, 22)), (.15, (25, 0, 130)), (.35, (160, 0, 170)), (.55, (255, 25, 30)), (.8, (255, 170, 0)),
        (1, (255, 255, 220))]
LOAD = [(0, (0, 230, 120)), (.55, (255, 200, 0)), (1, (255, 30, 20))]


def _cells():
    """Grille 1 × 1 utilisée par le snake : cellule -> touche qui la recouvre."""
    out = {}
    for n, _l, x, y, w, h in H.LAYOUT:
        for cx in range(int(x), int(x + w) + 1):
            for cy in range(int(y), int(y + h) + 1):
                if x <= cx + .5 < x + w and y <= cy + .5 < y + h:
                    out[(cx, cy)] = n
    return out


CELLS = _cells()


class Fx:
    def __init__(self, name, zones, speed=5, ctx=None):
        self.name, self.zones, self.sp = name, zones, max(1, min(10, int(speed))) / 5.0
        self.ctx = ctx
        self.ext, self._ext_seen = 0.0, 0.0   # éclair envoyé par l'interface (horodatage)
        self.s = {}
        self.fn = getattr(self, "_" + name)

    def frame(self, t, dt, presses):
        if self.ext > self._ext_seen:
            self._ext_seen = self.ext
            self.s["xnew"], self.s["xflash"] = True, t
        out = self.fn(t, dt, presses)
        return {n: tuple(max(0, min(255, v)) for v in c) for n, c in out.items()}

    # ---- ripple : ondes circulaires qui partent de chaque touche pressée (et gouttes au repos)
    def _ripple(self, t, dt, pr):
        s = self.s
        rip = s.setdefault("r", [])
        for n in pr:
            rip.append((*H.POS[n], t, hsv(random.random(), .75, 1)))
            s["act"] = t
        if t - s.get("act", -9) > 1.6 and t - s.get("auto", -9) > 1.1:
            s["auto"] = t
            rip.append((random.uniform(1, W - 1), random.uniform(0.5, 5.5), t, hsv(random.random(), .8, 1)))
        s["r"] = rip = [r for r in rip if t - r[2] < 2.6]
        out = {}
        for n, x, y in KEYS:
            c = scale(self.zones[H.zone_of_x(x)], .10)
            for rx, ry, t0, col in rip:
                age = t - t0
                k = math.exp(-((dist(x, y, rx, ry) - age * 7 * self.sp) / .85) ** 2) * max(0, 1 - age / 2.4)
                if k > .02:
                    c = plus(c, scale(col, k))
            out[n] = c
        return out

    # ---- sable : dunes dorées, rafales de vent, grains qui scintillent
    def _sand(self, t, dt, pr):
        s = self.s
        gr = s.setdefault("g", {})
        for _ in range(2):
            if random.random() < .5:
                gr[random.choice(KEYS)[0]] = 1.0
        out = {}
        for n, x, y in KEYS:
            v = .5 + .5 * math.sin(x * .55 - t * .5 * self.sp + 1.3 * math.sin(y * .9 + t * .25))
            v2 = .5 + .5 * math.sin(x * .21 + y * .4 - t * .2)
            c = scale((238, 178, 86), .22 + .5 * v * .7 + .3 * v2 * .5)
            gust = max(0, math.sin(x * .25 - t * 1.1 * self.sp)) ** 6
            c = mix(c, (255, 232, 170), gust * .45)
            if n in gr:
                c = mix(c, (255, 245, 210), gr[n])
            out[n] = c
        for n in list(gr):
            gr[n] -= dt * 3
            if gr[n] <= 0:
                del gr[n]
        return out

    # ---- vagues : océan avec crêtes d'écume
    def _waves(self, t, dt, pr):
        out = {}
        for n, x, y in KEYS:
            h = .5 + .5 * math.sin(x * .6 - t * 1.4 * self.sp + y * .5)
            h = .65 * h + .35 * (.5 + .5 * math.sin(x * .27 + t * .8 * self.sp - y * .9))
            c = mix((0, 30, 140), (0, 190, 255), h)
            if h > .86:
                c = mix(c, (235, 250, 255), (h - .86) / .14)
            out[n] = c
        return out

    # ---- plasma
    def _plasma(self, t, dt, pr):
        out = {}
        for n, x, y in KEYS:
            v = (math.sin(x * .5 + t * self.sp) + math.sin(y * .9 - t * 1.2 * self.sp)
                 + math.sin((x + y) * .3 + t * .7) + math.sin(dist(x, y, W / 2, 3) * .4 - t * self.sp))
            out[n] = hsv(v / 8 + .5 + t * .02, 1, .95)
        return out

    # ---- matrix : pluie de caractères verts
    def _matrix(self, t, dt, pr):
        s = self.s
        cols = s.setdefault("c", {})
        for c in range(int(W) + 1):
            if c not in cols:
                cols[c] = [-random.uniform(0, 8), random.uniform(3, 7), random.uniform(3, 6)]
        for n in pr:
            x, y = H.POS[n]
            cols[int(x)] = [y - 1, 9, 5]
        for c, d in cols.items():
            d[0] += d[1] * dt * self.sp
            if d[0] - d[2] > HGT + 1:
                cols[c] = [-random.uniform(0, 5), random.uniform(3, 7), random.uniform(3, 6)]
        out = {}
        for n, x, y in KEYS:
            hd, _sp, ln = cols[int(x)]
            d = hd - y
            if 0 <= d < ln:
                v = (1 - d / ln) ** 1.5
                out[n] = (190, 255, 200) if d < .8 else (0, 255 * v, 50 * v)
            else:
                out[n] = (0, 6, 0)
        return out

    # ---- orage : nuages sombres + éclairs
    def _storm(self, t, dt, pr):
        s = self.s
        if pr or s.pop("xnew", False) or random.random() < dt * .45:
            x = H.POS[pr[0]][0] if pr else random.uniform(1, W - 1)
            path, y = [], -.5
            while y < HGT + .5:
                path.append((x, y))
                x += random.uniform(-.9, .9)
                y += .6
            s["bolt"] = (t, path)
        age = t - s["bolt"][0] if "bolt" in s else 9
        out = {}
        for n, x, y in KEYS:
            cloud = .5 + .5 * math.sin(x * .4 + t * .3 * self.sp) * math.sin(y * .8 - t * .2)
            c = scale((14, 18, 70), .5 + .6 * cloud)
            if age < .4:
                c = plus(c, scale((70, 80, 130), 1 - age / .4))
            if age < .2 and any(dist(x, y, bx, by) < .75 for bx, by in s["bolt"][1]):
                c = (255, 255, 255)
            out[n] = c
        return out

    # ---- feux d'artifice
    def _fireworks(self, t, dt, pr):
        s = self.s
        rk, ex = s.setdefault("rk", []), s.setdefault("ex", [])
        for n in pr:
            rk.append([H.POS[n][0], 5.8, random.uniform(.8, 2.8), random.random()])
            s["act"] = t
        if t - s.get("act", -9) > 1.8 and t - s.get("auto", -9) > 1.0:
            s["auto"] = t
            rk.append([random.uniform(2, W - 2), 5.8, random.uniform(.8, 2.8), random.random()])
        for r in rk[:]:
            r[1] -= 7.5 * dt * self.sp
            if r[1] <= r[2]:
                ex.append((r[0], r[1], t, r[3]))
                rk.remove(r)
        s["ex"] = ex = [e for e in ex if t - e[2] < 1.5]
        out = {n: (4, 2, 10) for n, _x, _y in KEYS}
        for n, x, y in KEYS:
            c = out[n]
            for rx, ry, _t, _h in rk:
                if dist(x, y, rx, ry) < .7:
                    c = plus(c, (255, 190, 90))
            for ex_x, ex_y, t0, h in ex:
                age = t - t0
                k = math.exp(-((dist(x, y, ex_x, ex_y) - age * 4.2) / 1.0) ** 2) * max(0, 1 - age / 1.4)
                if k > .02:
                    c = plus(c, scale(hsv(h + random.uniform(-.05, .05), .85, 1), k))
            out[n] = c
        return out

    # ---- tank : roule, vise la touche pressée et tire
    def _tank(self, t, dt, pr):
        s = self.s
        if not s:
            s.update(tx=W / 2, target=W / 2, ty=.4, shells=[], ex=[], cool=0, idle=t, armed=False, fired=-9)
        for n in pr:
            cx, cy = H.POS[n]
            s.update(target=max(1.2, min(W - 1.2, cx)), ty=cy if cy < 3 else .4, idle=t, armed=True)
        if t - s["idle"] > 1.9:
            s.update(target=random.uniform(1.5, W - 1.5), ty=random.choice([.4, .6, 1.2]), idle=t, armed=True)
        dx = s["target"] - s["tx"]
        step = 6 * self.sp * dt
        moving = abs(dx) > step
        s["tx"] = s["tx"] + math.copysign(step, dx) if moving else s["target"]
        s["cool"] -= dt
        if not moving and s["armed"] and s["cool"] <= 0:
            s["shells"].append([s["tx"], 2.6, s["ty"]])
            s.update(cool=.8, armed=False, fired=t)
        for sh in s["shells"][:]:
            sh[1] -= 11 * dt * self.sp
            if sh[1] <= sh[2]:
                s["ex"].append((sh[0], sh[2], t))
                s["shells"].remove(sh)
        s["ex"] = [e for e in s["ex"] if t - e[2] < .7]
        tx = s["tx"]
        out = {}
        for n, x, y in KEYS:
            c = (3, 12, 2)
            if y > 4.5:
                c = (22, 42, 10)
            if y > 4.5 and abs(x - tx) <= 1.7:
                c = (35, 70, 14) if moving and int(t * 12 + x) % 2 else (95, 155, 40)
            elif 3.5 < y <= 4.5 and abs(x - tx) <= .95:
                c = (70, 125, 32)
            elif 2.0 < y <= 3.5 and abs(x - tx) <= .6:
                c = (170, 190, 100)
            if t - s["fired"] < .12 and dist(x, y, tx, 2.2) < 1.1:
                c = (255, 235, 130)
            for sx, sy, _ty in s["shells"]:
                if dist(x, y, sx, sy) < .75:
                    c = (255, 255, 210)
                elif abs(x - sx) < .6 and 0 < y - sy < 1.7:
                    c = plus(c, (190, 90, 10))
            for ex_x, ex_y, t0 in s["ex"]:
                age = t - t0
                r = 2.6 * min(1, age / .35)
                if dist(x, y, ex_x, ex_y) < r:
                    f = max(0, 1 - age / .7)
                    c = plus(c, mix((255, 40, 0), (255, 220, 120), f * f) if f > .4 else scale((255, 60, 0), f * 2))
            out[n] = c
        return out

    # ---- carte de chaleur : les touches chauffent quand tu tapes et refroidissent lentement
    def _heat(self, t, dt, pr):
        hm = self.s.setdefault("h", {})
        for n in pr:
            x0, y0 = H.POS[n]
            for m, x, y in KEYS:
                d = dist(x0, y0, x, y)
                if d < 1.6:
                    hm[m] = min(1.0, hm.get(m, 0) + (.32 if d < .1 else .1 * (1.6 - d)))
        dec = dt * .05 * self.sp
        out = {}
        for n, _x, _y in KEYS:
            v = hm.get(n, 0)
            if v > 0:
                v = hm[n] = max(0.0, v - dec)
            out[n] = ramp(HEAT, v)
        return out

    # ---- snake : il joue tout seul ; appuie sur une touche pour y poser la pomme
    def _snake(self, t, dt, pr):
        s = self.s
        cells = list(CELLS)
        if "b" not in s:
            start = min(CELLS, key=lambda c: abs(c[0] - 7) + abs(c[1] - 3))
            s.update(b=[start], food=random.choice(cells), acc=0.0, flash=-9)
        if pr:
            x, y = H.POS[pr[-1]]
            c = (int(x), int(y))
            if c in CELLS and c not in s["b"]:
                s["food"] = c
        s["acc"] += dt
        step = .16 / self.sp
        while s["acc"] >= step:
            s["acc"] -= step
            body, head = s["b"], s["b"][0]
            nxt = self._snake_path(head, s["food"], set(body[:-1]))
            if nxt is None:
                s.update(b=[random.choice(cells)], flash=t)
                break
            body.insert(0, nxt)
            if nxt == s["food"]:
                free = [c for c in cells if c not in body]
                s["food"] = random.choice(free) if free else body[-1]
                if len(body) > 28:
                    s.update(b=body[:3], flash=t)
            else:
                body.pop()
        lit = {}
        body = s["b"]
        for i, c in enumerate(reversed(body)):
            k = (i + 1) / len(body)
            lit[CELLS[c]] = hsv(.33 - .25 * (1 - k), .85, .25 + .75 * k)
        lit[CELLS[body[0]]] = (210, 255, 210)
        fp = .55 + .45 * math.sin(t * 8)
        lit[CELLS[s["food"]]] = (255 * fp, 20 * fp, 40 * fp)
        bg = (30, 30, 30) if t - s["flash"] < .25 else (0, 3, 8)
        return {n: lit.get(n, bg) for n, _x, _y in KEYS}

    @staticmethod
    def _snake_path(start, goal, blocked):
        prev, q, i = {start: None}, [start], 0
        while i < len(q):
            c = q[i]
            i += 1
            if c == goal:
                break
            for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (c[0] + d[0], c[1] + d[1])
                if n in CELLS and n not in prev and n not in blocked:
                    prev[n] = c
                    q.append(n)
        if goal in prev and goal != start:
            c = goal
            while prev[c] != start:
                c = prev[c]
            return c
        free = [(start[0] + a, start[1] + b) for a, b in ((1, 0), (-1, 0), (0, 1), (0, -1))]
        free = [c for c in free if c in CELLS and c not in blocked]
        return random.choice(free) if free else None

    # ---- ciel étoilé : des touches s'allument doucement dans les couleurs des zones
    def _starlight(self, t, dt, pr):
        st = self.s.setdefault("st", {})
        rate = 7 * self.sp * dt
        for _ in range(int(rate) + (1 if random.random() < rate % 1 else 0)):
            n = random.choice(KEYS)[0]
            if n not in st:
                z = self.zones[random.randrange(3)]
                st[n] = [0.0, mix(z, (255, 255, 255), random.random() * .35)]
        out = {}
        for n, x, _y in KEYS:
            c = scale(self.zones[H.zone_of_x(x)], .05)
            if n in st:
                st[n][0] += dt * .8 * self.sp
                a = st[n][0]
                if a >= 1:
                    del st[n]
                else:
                    c = plus(c, scale(st[n][1], math.sin(a * math.pi)))
            out[n] = c
        return out

    # ---- tableau de bord : F1–F12 = température, chiffres = CPU, AZERTY = RAM, pavé num. = GPU
    def _hud(self, t, dt, pr):
        c = self.ctx() if self.ctx else {}
        temp = c.get("temp")
        raw = [(c.get("cpu") or 0) / 100, (c.get("ram") or 0) / 100, (c.get("gpu") or 0) / 100,
               0 if temp is None else (temp - 35) / 60]
        sm = self.s.setdefault("sm", list(raw))
        for i, v in enumerate(raw):
            sm[i] += (max(0, min(1, v)) - sm[i]) * min(1, dt * 3)
        cpu, ram, gpu, tf = sm
        hot = tf > .85 and int(t * 4) % 2
        out = {}
        for n, x, y in KEYS:
            row = int(y)
            c = scale(self.zones[H.zone_of_x(x)], .07)
            if x < 16:
                f = x / 15.5
                if row == 0 and f <= max(tf, .04):
                    c = (255, 255, 255) if hot else ramp(LOAD, tf)
                elif row == 1 and f <= cpu:
                    c = ramp(LOAD, f)
                elif row == 2 and f <= ram:
                    c = mix((90, 40, 255), (230, 80, 255), f)
            elif row == 0:
                c = ramp(LOAD, tf)
            elif (5.5 - y) / 4 < gpu:
                c = mix((0, 200, 255), (255, 40, 200), (5.5 - y) / 4)
            out[n] = c
        return out

    # ---- pluie : gouttes qui tombent rangée par rangée, éclaboussures sur la barre d'espace, éclairs de l'interface
    def _rain(self, t, dt, pr):
        s = self.s
        dr = s.setdefault("d", [])
        sp = s.setdefault("sp", [])
        c = self.ctx() if self.ctx else {}
        dens = .35 + .65 * min(1, (c.get("cpu") or 30) / 80)
        if random.random() < dt * 9 * dens * self.sp:
            dr.append([random.uniform(0, W), -.5, random.uniform(5, 8), random.uniform(.6, 1)])
        for n in pr:
            x, y = H.POS[n]
            sp.append([x, y, t, 1.0])
        for d in dr[:]:
            d[1] += d[2] * dt * self.sp
            if d[1] > HGT - .3:
                sp.append([d[0], HGT - .5, t, d[3] * .8])
                dr.remove(d)
        s["sp"] = sp = [p for p in sp if t - p[2] < .6]
        fl = max(0.0, 1 - (t - s.get("xflash", -9)) / .5)
        if random.random() < dt * .04 * self.sp:
            s["xflash"] = t
        out = {}
        for n, x, y in KEYS:
            col = mix((2, 8, 26), scale(self.zones[H.zone_of_x(x)], .12), .35)
            for dx, dy, _v, b in dr:
                ddy = dy - y
                if abs(x - dx) < .55 and -1.6 < ddy < .5:
                    k = (1 - abs(ddy - .3) / 1.9) * b
                    col = plus(col, scale((120, 170, 255), max(0, k)))
            for px, py, t0, b in sp:
                age = t - t0
                k = math.exp(-((dist(x, y, px, py) - age * 5) / .6) ** 2) * (1 - age / .6) * b
                if k > .03:
                    col = plus(col, scale((150, 200, 255), k))
            if fl > 0:
                col = plus(col, scale((200, 215, 255), fl * (.6 + .4 * math.sin(fl * 30) ** 2)))
            out[n] = col
        return out
