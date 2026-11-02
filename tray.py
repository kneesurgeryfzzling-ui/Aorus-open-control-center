"""Icône dans la zone de notification Windows (ctypes pur, sans dépendance).

Tray(icon_path, tip, on_open, build_menu, on_command)
  - clic gauche / double-clic : on_open()
  - clic droit : menu construit par build_menu() -> liste d'entrées
        ("texte", id, coché)  |  None (séparateur)  |  ("texte", [sous-entrées])
    l'id choisi est passé à on_command(id)
  - notify(titre, message, warn=False) : bulle de notification Windows
  - set_tip(texte), stop()
"""
import sys
import threading

IS_WIN = sys.platform == "win32"

if IS_WIN:
    import ctypes as C
    import ctypes.wintypes as W

    LRESULT = C.c_ssize_t
    WNDPROC = C.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

    class GUID(C.Structure):
        _fields_ = [("d1", W.DWORD), ("d2", W.WORD), ("d3", W.WORD), ("d4", C.c_ubyte * 8)]

    class NOTIFYICONDATAW(C.Structure):
        _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT),
                    ("uCallbackMessage", W.UINT), ("hIcon", W.HICON), ("szTip", W.WCHAR * 128),
                    ("dwState", W.DWORD), ("dwStateMask", W.DWORD), ("szInfo", W.WCHAR * 256),
                    ("uVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD),
                    ("guidItem", GUID), ("hBalloonIcon", W.HICON)]

    class WNDCLASSW(C.Structure):
        _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", C.c_int), ("cbWndExtra", C.c_int),
                    ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                    ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]

    u32 = C.WinDLL("user32", use_last_error=True)
    sh32 = C.WinDLL("shell32", use_last_error=True)
    k32 = C.WinDLL("kernel32", use_last_error=True)
    u32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    u32.DefWindowProcW.restype = LRESULT
    u32.RegisterClassW.argtypes = [C.POINTER(WNDCLASSW)]
    u32.RegisterClassW.restype = W.ATOM
    u32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int, C.c_int, C.c_int,
                                    W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
    u32.CreateWindowExW.restype = W.HWND
    u32.GetMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
    u32.TranslateMessage.argtypes = [C.POINTER(W.MSG)]
    u32.DispatchMessageW.argtypes = [C.POINTER(W.MSG)]
    u32.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    u32.CreatePopupMenu.restype = W.HMENU
    u32.AppendMenuW.argtypes = [W.HMENU, W.UINT, C.c_size_t, W.LPCWSTR]
    u32.TrackPopupMenu.argtypes = [W.HMENU, W.UINT, C.c_int, C.c_int, C.c_int, W.HWND, C.c_void_p]
    u32.TrackPopupMenu.restype = C.c_int
    u32.SetMenuDefaultItem.argtypes = [W.HMENU, W.UINT, W.UINT]
    u32.DestroyMenu.argtypes = [W.HMENU]
    u32.SetForegroundWindow.argtypes = [W.HWND]
    u32.DestroyWindow.argtypes = [W.HWND]
    u32.LoadImageW.argtypes = [W.HINSTANCE, W.LPCWSTR, W.UINT, C.c_int, C.c_int, W.UINT]
    u32.LoadImageW.restype = W.HANDLE
    u32.LoadIconW.argtypes = [W.HINSTANCE, C.c_void_p]
    u32.LoadIconW.restype = W.HICON
    u32.RegisterWindowMessageW.argtypes = [W.LPCWSTR]
    u32.RegisterWindowMessageW.restype = W.UINT
    sh32.Shell_NotifyIconW.argtypes = [W.DWORD, C.POINTER(NOTIFYICONDATAW)]
    sh32.Shell_NotifyIconW.restype = W.BOOL
    k32.GetModuleHandleW.argtypes = [W.LPCWSTR]
    k32.GetModuleHandleW.restype = W.HMODULE

    WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_NULL = 0x0002, 0x0010, 0x0111, 0x0000
    WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0203, 0x0205, 0x007B
    WM_TRAY = 0x0400 + 20
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
    MF_STRING, MF_SEPARATOR, MF_POPUP, MF_CHECKED = 0, 0x800, 0x10, 0x8
    TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80


class Tray:
    def __init__(self, icon_path, tip, on_open, build_menu, on_command):
        self.icon_path, self.tip = str(icon_path), tip
        self.on_open, self.build_menu, self.on_command = on_open, build_menu, on_command
        self.hwnd, self.ok, self._proc = None, False, None
        self.ready = threading.Event()
        if IS_WIN:
            threading.Thread(target=self._run, daemon=True).start()
            self.ready.wait(5)

    # ------------------------------------------------------------------ fil de la fenêtre cachée
    def _nid(self, flags):
        n = NOTIFYICONDATAW()
        n.cbSize = C.sizeof(NOTIFYICONDATAW)
        n.hWnd, n.uID, n.uFlags = self.hwnd, 1, flags
        n.uCallbackMessage, n.hIcon = WM_TRAY, self.hicon
        n.szTip = self.tip[:127]
        return n

    def _add(self):
        self.ok = bool(sh32.Shell_NotifyIconW(NIM_ADD, C.byref(self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP))))

    def _run(self):
        try:
            hinst = k32.GetModuleHandleW(None)
            self.hicon = u32.LoadImageW(None, self.icon_path, 1, 0, 0, 0x10 | 0x40) or u32.LoadIconW(None, C.c_void_p(32512))
            self.taskbar_created = u32.RegisterWindowMessageW("TaskbarCreated")
            self._proc = WNDPROC(self._wndproc)
            wc = WNDCLASSW()
            wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._proc, hinst, "AorusOpenCCTray"
            u32.RegisterClassW(C.byref(wc))
            self.hwnd = u32.CreateWindowExW(0, "AorusOpenCCTray", "AORUS Open CC", 0, 0, 0, 0, 0, None, None, hinst, None)
            self._add()
        except Exception:  # noqa: BLE001
            self.ok = False
        finally:
            self.ready.set()
        if not self.hwnd:
            return
        msg = W.MSG()
        while u32.GetMessageW(C.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(C.byref(msg))
            u32.DispatchMessageW(C.byref(msg))

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TRAY:
                ev = lp & 0xFFFF
                if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    threading.Thread(target=self.on_open, daemon=True).start()
                elif ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                return 0
            if msg == self.taskbar_created:      # l'explorateur a redémarré : on remet l'icône
                self._add()
                return 0
            if msg == WM_CLOSE:
                u32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                sh32.Shell_NotifyIconW(NIM_DELETE, C.byref(self._nid(0)))
                u32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001
            pass
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    def _menu(self):
        try:
            items = self.build_menu()
        except Exception:  # noqa: BLE001
            items = [("Ouvrir", "open", False), None, ("Quitter", "quit", False)]
        ids, menus = {}, []

        def build(entries):
            m = u32.CreatePopupMenu()
            menus.append(m)
            for e in entries:
                if e is None:
                    u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
                elif isinstance(e[1], list):
                    u32.AppendMenuW(m, MF_POPUP, build(e[1]), e[0])
                else:
                    i = len(ids) + 100
                    ids[i] = e[1]
                    u32.AppendMenuW(m, MF_STRING | (MF_CHECKED if len(e) > 2 and e[2] else 0), i, e[0])
            return m
        root = build(items)
        u32.SetMenuDefaultItem(root, 100, 0)
        pt = W.POINT()
        u32.GetCursorPos(C.byref(pt))
        u32.SetForegroundWindow(self.hwnd)
        cmd = u32.TrackPopupMenu(root, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        u32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        u32.DestroyMenu(root)            # détruit aussi les sous-menus
        if cmd in ids:
            threading.Thread(target=self.on_command, args=(ids[cmd],), daemon=True).start()

    # ------------------------------------------------------------------ API
    def notify(self, title, msg, warn=False):
        if not (IS_WIN and self.ok):
            return
        n = self._nid(NIF_INFO)
        n.szInfoTitle, n.szInfo = title[:63], msg[:255]
        n.dwInfoFlags = 2 if warn else 1
        sh32.Shell_NotifyIconW(NIM_MODIFY, C.byref(n))

    def set_tip(self, tip):
        self.tip = tip
        if IS_WIN and self.ok:
            sh32.Shell_NotifyIconW(NIM_MODIFY, C.byref(self._nid(NIF_TIP)))

    def stop(self):
        if IS_WIN and self.hwnd:
            u32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
