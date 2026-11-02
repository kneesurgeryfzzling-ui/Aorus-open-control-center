"""Clavier Fusion USB (VID 0x1044) — protocole repris de FusionKeyboard.dll (FusionDevice).

Le 15G YC est géré par ControlCenter avec Manifacture=0 -> FusionKeyboard.dll (HID USB), et non
par le WMI RW_GMWMI (Aorus15Keyboard.dll), d'où l'erreur « Not supported » sur le WMI.

 - Effet matériel : rapport feature de 9 octets  [0, 8, 0, effet, vitesse, luminosité, couleur, direction, checksum]
 - Matrice « custom » : feature [0, 0x12, 0, slot, 8, 0, 0, 0, chk] puis 8 écritures de 65 octets (0 + 64 octets)
   = 512 octets = 128 touches × [0, R, G, B] ; l'index d'une touche est colonne*6 + ligne ;
   puis on active l'effet 51 + slot.
"""
import ctypes as C
import ctypes.wintypes as W
import sys
import threading
import time

IS_WIN = sys.platform == "win32"
VID = 0x1044
PID_CLASSIC = {0x7A38, 0x7A39, 0x7A3B, 0x7A3F}   # ITE / Holtek (feature = 9 octets)
PID_IONE = {0x7A3C, 0x7A3D, 0x7A3E}              # famille « iOne » : autre protocole, non géré
PID_X9 = {0x7A3A}

# index de matrice par touche (Helpers.getKeyIndex)
KEY_INDEX = {
    'esc': 11, 'f1': 17, 'f2': 23, 'f3': 29, 'f4': 35, 'f5': 41, 'f6': 47, 'f7': 53, 'f8': 59, 'f9': 65, 'f10': 71,
    'f11': 77, 'f12': 83, 'pause': 89, 'del': 95, 'home': 101, 'pgup': 107, 'pgdn': 113, 'end': 119,
    'graveaccent': 10, '1': 16, '2': 22, '3': 28, '4': 34, '5': 40, '6': 46, '7': 52, '8': 58, '9': 64, '0': 70,
    'hyphenminus': 76, 'equalsign': 82, 'backspace': 94, 'numlk': 100, 'numberslash': 106, 'asterisk': 112,
    'minussign': 118, 'tab': 9, 'q': 15, 'w': 21, 'e': 27, 'r': 33, 't': 39, 'y': 45, 'u': 51, 'i': 57, 'o': 63,
    'p': 69, 'lsquarebracket': 75, 'rsquarebracket': 81, 'backslash': 87, 'number7': 99, 'number8': 105,
    'number9': 111, 'caps': 8, 'a': 14, 's': 20, 'd': 26, 'f': 32, 'g': 38, 'h': 44, 'j': 50, 'k': 56, 'l': 62,
    'semicolon': 68, 'apostrophe': 74, 'enter': 92, 'number4': 98, 'number5': 104, 'number6': 110, 'lshift': 7,
    'z': 19, 'x': 25, 'c': 31, 'v': 37, 'b': 43, 'n': 49, 'm': 55, 'comma': 61, 'fullstop': 67, 'slash': 73,
    'rshift': 85, 'up': 91, 'number1': 97, 'number2': 103, 'number3': 109, 'lctrl': 6, 'fn': 12, 'win': 18,
    'lalt': 24, 'space': 42, 'ralt': 60, 'app': 66, 'rctrl': 72, 'left': 84, 'down': 90, 'right': 96,
    'number0': 102, 'numberdel': 108, 'plussign': 116, 'uksharp': 86, 'ukbar': 13, 'numberenter': 114,
}
KEYS = sorted(set(KEY_INDEX.values()))
COLS = sorted({k // 6 for k in KEYS})  # colonnes présentes (1..19)


def checksum(b):
    """b[1..7] sommés puis 255 - somme (octet), comme dans SetLightEffect."""
    return (255 - (sum(b[1:8]) & 0xFF)) & 0xFF


def effect_report(effect, speed, brightness, color, direction=1):
    b = [0, 8, 0, effect & 255, speed & 255, brightness & 255, color & 255, direction & 255, 0]
    b[8] = checksum(b)
    return b


def matrix_header(slot):
    b = [0, 0x12, 0, slot & 255, 8, 0, 0, 0, 0]
    b[8] = checksum(b)
    return b


def build_matrix(colfn):
    """colfn(col, row) -> (r, g, b). Retourne les 512 octets de la matrice."""
    m = bytearray(512)
    for idx in KEYS:
        r, g, b = colfn(idx // 6, idx % 6)
        o = idx * 4
        m[o + 1], m[o + 2], m[o + 3] = (max(0, min(255, int(v))) for v in (r, g, b))
    return m


def chunks(matrix):
    return [bytes([0]) + bytes(matrix[i * 64:(i + 1) * 64]) for i in range(8)]  # 8 × 65 octets


# ----------------------------------------------------------------------------- disposition physique (unités = 1 touche)
def _build_layout():
    L = []  # (nom, étiquette, x, y, w, h)

    def row(y, items, x=0.0):
        for name, label, w in items:
            if name:
                L.append((name, label, x, y, w, 1))
            x += w
        return x
    row(0, [("esc", "Esc", 1), ("", "", .25)] + [(f"f{i}", f"F{i}", 1) for i in range(1, 5)] + [("", "", .25)]
        + [(f"f{i}", f"F{i}", 1) for i in range(5, 9)] + [("", "", .25)] + [(f"f{i}", f"F{i}", 1) for i in range(9, 13)]
        + [("pause", "Pause", 1), ("del", "Suppr", 1)])
    row(1, [("graveaccent", "`", 1)] + [(str(i % 10), str(i % 10), 1) for i in range(1, 11)]
        + [("hyphenminus", "-", 1), ("equalsign", "=", 1), ("backspace", "⌫", 2)])
    row(2, [("tab", "Tab", 1.5)] + [(c, c.upper(), 1) for c in "qwertyuiop"]
        + [("lsquarebracket", "[", 1), ("rsquarebracket", "]", 1), ("backslash", "\\", 1.5)])
    row(3, [("caps", "Verr", 1.75)] + [(c, c.upper(), 1) for c in "asdfghjkl"]
        + [("semicolon", ";", 1), ("apostrophe", "'", 1), ("enter", "Entrée", 2.25)])
    row(4, [("lshift", "Maj", 2.25)] + [(c, c.upper(), 1) for c in "zxcvbnm"]
        + [("comma", ",", 1), ("fullstop", ".", 1), ("slash", "/", 1), ("rshift", "Maj", 1.75), ("up", "▲", 1)])
    row(5, [("lctrl", "Ctrl", 1.25), ("fn", "Fn", 1), ("win", "Win", 1), ("lalt", "Alt", 1.25), ("space", "", 4.25),
            ("ralt", "Alt", 1.25), ("app", "≡", 1), ("rctrl", "Ctrl", 1.5), ("left", "◀", 1), ("down", "▼", 1), ("right", "▶", 1)])
    X = 16.5  # pavé numérique
    for i, (n, lb) in enumerate((("home", "Orig"), ("pgup", "PgHt"), ("pgdn", "PgBs"), ("end", "Fin"))):
        L.append((n, lb, X + i, 0, 1, 1))
    for i, (n, lb) in enumerate((("numlk", "Num"), ("numberslash", "/"), ("asterisk", "*"), ("minussign", "-"))):
        L.append((n, lb, X + i, 1, 1, 1))
    for r, names in ((2, ("number7", "number8", "number9")), (3, ("number4", "number5", "number6")), (4, ("number1", "number2", "number3"))):
        for i, n in enumerate(names):
            L.append((n, n[-1], X + i, r, 1, 1))
    L += [("plussign", "+", X + 3, 2, 1, 2), ("numberenter", "↵", X + 3, 4, 1, 2),
          ("number0", "0", X, 5, 2, 1), ("numberdel", ".", X + 2, 5, 1, 1)]
    return L


LAYOUT = [k for k in _build_layout() if k[0] in KEY_INDEX]
WIDTH = max(k[2] + k[4] for k in LAYOUT)
HEIGHT = 6.0
POS = {k[0]: (k[2] + k[4] / 2, k[3] + k[5] / 2) for k in LAYOUT}


def zone_of_x(x):
    return 0 if x < WIDTH / 3 else 1 if x < 2 * WIDTH / 3 else 2


GAIN_GIGABYTE = [14, 100, 36]  # équilibre du blanc d'origine pour ce clavier : (34, 238, 85) / 238


def correct(rgb, gain):
    """Corrige la balance des LED (le rouge de ce clavier est bien plus puissant que le vert/bleu).
    Les canaux sont pondérés par `gain` (%), puis la couleur est remise à la luminosité voulue."""
    if not gain:
        return rgb
    u = [max(0.0, min(255.0, float(c))) / 255 for c in rgb]
    v = [u[i] * gain[i] / 100 for i in range(3)]
    m = max(v)
    if m <= 0:
        return (0, 0, 0)
    k = max(u) / m
    return tuple(255 * min(1.0, v[i] * k) for i in range(3))


def matrix_from_names(fn, gain=None):
    """fn(nom, cx, cy) -> (r, g, b) ; construit la matrice de 512 octets (avec correction `gain`)."""
    m = bytearray(512)
    for n, _l, x, y, w, h in LAYOUT:
        r, g, b = correct(fn(n, x + w / 2, y + h / 2), gain)
        o = KEY_INDEX[n] * 4
        m[o + 1], m[o + 2], m[o + 3] = (max(0, min(255, int(v))) for v in (r, g, b))
    return m


# ----------------------------------------------------------------------------- accès Windows (ctypes)
if IS_WIN:
    class GUID(C.Structure):
        _fields_ = [("d1", W.DWORD), ("d2", W.WORD), ("d3", W.WORD), ("d4", C.c_ubyte * 8)]

    class SP_DID(C.Structure):
        _fields_ = [("cbSize", W.DWORD), ("guid", GUID), ("Flags", W.DWORD), ("Reserved", C.c_void_p)]

    class HIDD_ATTR(C.Structure):
        _fields_ = [("Size", W.ULONG), ("VendorID", W.USHORT), ("ProductID", W.USHORT), ("Version", W.USHORT)]

    class HIDP_CAPS(C.Structure):
        _fields_ = [("Usage", W.USHORT), ("UsagePage", W.USHORT), ("InputLen", W.USHORT), ("OutputLen", W.USHORT),
                    ("FeatureLen", W.USHORT), ("Reserved", W.USHORT * 17)] + \
                   [(n, W.USHORT) for n in ("NLC", "NIB", "NIV", "NID", "NOB", "NOV", "NOD", "NFB", "NFV", "NFD")]

    _setupapi = C.WinDLL("setupapi", use_last_error=True)
    _hid = C.WinDLL("hid", use_last_error=True)
    _k32 = C.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = W.HANDLE
    _k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    _k32.WriteFile.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
    _k32.CloseHandle.argtypes = [W.HANDLE]
    _setupapi.SetupDiGetClassDevsW.restype = C.c_void_p
    _setupapi.SetupDiGetClassDevsW.argtypes = [C.POINTER(GUID), W.LPCWSTR, W.HWND, W.DWORD]
    _setupapi.SetupDiEnumDeviceInterfaces.argtypes = [C.c_void_p, C.c_void_p, C.POINTER(GUID), W.DWORD, C.POINTER(SP_DID)]
    _setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [C.c_void_p, C.POINTER(SP_DID), C.c_void_p, W.DWORD,
                                                           C.POINTER(W.DWORD), C.c_void_p]
    _setupapi.SetupDiDestroyDeviceInfoList.argtypes = [C.c_void_p]
    _hid.HidD_SetFeature.argtypes = [W.HANDLE, C.c_void_p, W.ULONG]
    _hid.HidD_GetAttributes.argtypes = [W.HANDLE, C.POINTER(HIDD_ATTR)]
    _hid.HidD_GetPreparsedData.argtypes = [W.HANDLE, C.POINTER(C.c_void_p)]
    _hid.HidD_FreePreparsedData.argtypes = [C.c_void_p]
    _hid.HidP_GetCaps.argtypes = [C.c_void_p, C.POINTER(HIDP_CAPS)]
    INVALID = C.c_void_p(-1).value

    def _paths():
        g = GUID()
        _hid.HidD_GetHidGuid(C.byref(g))
        hdev = _setupapi.SetupDiGetClassDevsW(C.byref(g), None, None, 0x12)  # PRESENT | DEVICEINTERFACE
        out, i = [], 0
        try:
            while True:
                d = SP_DID()
                d.cbSize = C.sizeof(SP_DID)
                if not _setupapi.SetupDiEnumDeviceInterfaces(hdev, None, C.byref(g), i, C.byref(d)):
                    break
                i += 1
                need = W.DWORD(0)
                _setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, C.byref(d), None, 0, C.byref(need), None)
                if need.value == 0:
                    continue
                buf = C.create_string_buffer(need.value)
                C.cast(buf, C.POINTER(W.DWORD))[0] = 8 if C.sizeof(C.c_void_p) == 8 else 6
                if _setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, C.byref(d), buf, need.value, None, None):
                    out.append(C.wstring_at(C.addressof(buf) + 4))
        finally:
            _setupapi.SetupDiDestroyDeviceInfoList(hdev)
        return out

    def scan():
        """Tous les périphériques HID de VID 0x1044 (Gigabyte) avec leurs tailles de rapports."""
        res = []
        for p in _paths():
            if "vid_1044" not in p.lower():
                continue
            h = _k32.CreateFileW(p, 0xC0000000, 3, None, 3, 0, None)
            if h in (None, INVALID):
                h = _k32.CreateFileW(p, 0, 3, None, 3, 0, None)  # accès « interrogation » seulement
                if h in (None, INVALID):
                    res.append(dict(path=p, pid=None, err="ouverture impossible"))
                    continue
            at = HIDD_ATTR()
            at.Size = C.sizeof(at)
            _hid.HidD_GetAttributes(h, C.byref(at))
            pre, caps = C.c_void_p(), HIDP_CAPS()
            _hid.HidD_GetPreparsedData(h, C.byref(pre))
            if pre:
                _hid.HidP_GetCaps(pre, C.byref(caps))
                _hid.HidD_FreePreparsedData(pre)
            _k32.CloseHandle(h)
            res.append(dict(path=p, vid=at.VendorID, pid=at.ProductID, feature=caps.FeatureLen, out=caps.OutputLen,
                            page=caps.UsagePage, usage=caps.Usage))
        return res

    class WinDev:
        def __init__(self, path):
            self.h = _k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)
            if self.h in (None, INVALID):
                raise OSError(f"ouverture impossible (erreur {C.get_last_error()})")

        def set_feature(self, data):
            buf = (C.c_ubyte * len(data))(*data)
            if not _hid.HidD_SetFeature(self.h, buf, len(data)):
                raise OSError(f"HidD_SetFeature a échoué (erreur {C.get_last_error()})")

        def write(self, data):
            buf = (C.c_ubyte * len(data))(*data)
            n = W.DWORD(0)
            if not _k32.WriteFile(self.h, buf, len(data), C.byref(n), None):
                raise OSError(f"WriteFile a échoué (erreur {C.get_last_error()})")

        def close(self):
            _k32.CloseHandle(self.h)
else:
    def scan():
        return []


class FakeDev:
    """Enregistre les paquets (tests sous Linux)."""
    def __init__(self, log):
        self.log, self.n_feature, self.n_write, self.last = log, 0, 0, None

    def set_feature(self, data):
        self.n_feature += 1
        self.last = list(data)

    def write(self, data):
        self.n_write += 1

    def close(self):
        pass


class FusionKb:
    def __init__(self, dev, info=None, gap=0.05):
        self.dev, self.info, self.gap = dev, info or {}, gap
        self.fast_gap = 0.0 if info and info.get("fake") else 0.006  # écart entre paquets en animation (s'adapte)
        self.lock = threading.Lock()

    def slow_down(self):
        self.fast_gap = min(0.05, max(0.01, self.fast_gap * 2))

    @staticmethod
    def find(log, fake_log=None):
        """Retourne (FusionKb | None, résumé des périphériques vus)."""
        if fake_log:
            return FusionKb(FakeDev(fake_log), dict(pid=0x7A3F, fake=True), gap=0.0), []
        seen = scan()
        for d in seen:
            log(f"HID Gigabyte : pid={d['pid'] and hex(d['pid'])} feature={d.get('feature')} sortie={d.get('out')} "
                f"page={d.get('page')} usage={d.get('usage')} {d.get('err', '')}")
        cand = [d for d in seen if d.get("pid") in PID_CLASSIC and d.get("feature") == 9]
        cand.sort(key=lambda d: d.get("out") != 65)  # préfère l'interface qui accepte 65 octets
        for d in cand:
            try:
                return FusionKb(WinDev(d["path"]), d), seen
            except OSError as e:
                log(f"HID : {e}")
        return None, seen

    def effect(self, effect, speed, brightness, color, direction=1):
        with self.lock:
            self.dev.set_feature(bytes(effect_report(effect, speed, brightness, color, direction)))
            time.sleep(0.065)

    def matrix(self, m, slot=0):
        with self.lock:
            self.dev.set_feature(bytes(matrix_header(slot)))
            time.sleep(0.065)
            for c in chunks(m):
                self.dev.write(c)
                time.sleep(self.gap)

    def custom(self, m, brightness, slot=0, fast=False):
        if not fast:
            self.matrix(m, slot)
            time.sleep(self.gap * 4)
            self.effect(51 + slot, 5, brightness, 2, 1)
            return
        g = self.fast_gap
        with self.lock:
            self.dev.set_feature(bytes(matrix_header(slot)))
            time.sleep(0.02)
            for c in chunks(m):
                self.dev.write(c)
                time.sleep(g)
            self.dev.set_feature(bytes(effect_report(51 + slot, 5, brightness, 2, 1)))
            time.sleep(0.015)
