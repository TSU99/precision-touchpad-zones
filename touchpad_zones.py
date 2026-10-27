"""Precision Touchpad zone editor.

Shows the supercurtain, curtain and right-click zones that Windows applies to a
Precision Touchpad, drawn to scale, with live finger contacts on top. Zones can be
edited with the spin boxes or by dragging their edges, then written to the registry.

Value reference:
https://learn.microsoft.com/windows-hardware/design/component-guidelines/touchpad-tuning-guidelines

    python touchpad_zones.py           GUI
    python touchpad_zones.py --probe   print detected devices and registry values
"""

import ctypes
import ctypes.wintypes as wt
import datetime
import os
import struct
import subprocess
import sys
import threading
import time
import winreg
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(APP_DIR, "backups")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

# ---------------------------------------------------------------- registry

PTP_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\PrecisionTouchPad"
ENUM_HID = r"SYSTEM\CurrentControlSet\Enum\HID"
HKLM, HKCU = winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER
WOW64 = winreg.KEY_WOW64_64KEY


class Location:
    """One registry key that can hold touchpad tuning values."""

    def __init__(self, key, label, hive, path):
        self.key, self.label, self.hive, self.path = key, label, hive, path

    @property
    def reg_path(self):
        return ("HKLM" if self.hive == HKLM else "HKCU") + "\\" + self.path

    def exists(self):
        try:
            winreg.CloseKey(winreg.OpenKey(self.hive, self.path, 0, winreg.KEY_READ | WOW64))
            return True
        except OSError:
            return False

    def read(self, names):
        """Return ({name: signed int}, error text or None)."""
        out = {}
        try:
            with winreg.OpenKey(self.hive, self.path, 0, winreg.KEY_READ | WOW64) as k:
                for n in names:
                    try:
                        v, t = winreg.QueryValueEx(k, n)
                    except FileNotFoundError:
                        continue
                    if t == winreg.REG_DWORD:
                        out[n] = v - (1 << 32) if v & 0x80000000 else v
        except FileNotFoundError:
            pass
        except OSError as e:
            return out, e.strerror or str(e)
        return out, None

    def write(self, name, value):
        with winreg.CreateKeyEx(self.hive, self.path, 0, winreg.KEY_SET_VALUE | WOW64) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value & 0xFFFFFFFF)

    def delete(self, name):
        try:
            with winreg.OpenKey(self.hive, self.path, 0, winreg.KEY_SET_VALUE | WOW64) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass


class Setting:
    def __init__(self, name, group, side, default, percent=False):
        self.name, self.group, self.side = name, group, side
        self.default, self.percent = default, percent


SIDES = {"L": "Left", "T": "Top", "R": "Right", "B": "Bottom"}
SETTINGS = [Setting("SuperCurtain" + n, "super", s, 100) for s, n in SIDES.items()]
SETTINGS += [Setting("Curtain" + n, "curtain", s, 0) for s, n in SIDES.items()]
SETTINGS += [
    Setting("RightClickZoneWidth", "rcz", "W", 50, percent=True),
    Setting("RightClickZoneHeight", "rcz", "H", 25, percent=True),
]
BY_NAME = {s.name: s for s in SETTINGS}

# Used to draw the implicit curtain when CurtainLeft/Right/Top are not all set.
LEGACY_DEFAULTS = {"SpaceBarOffset": 1000, "HorizontalOffset": 0,
                   "AAPNonCurtainTop": 1500, "AAPNonCurtainBaseWidth": 1500}

# Supercurtains 15/15/10 mm, curtains 25/25/20 mm.
SUGGESTED = {"SuperCurtainLeft": 1500, "SuperCurtainRight": 1500, "SuperCurtainTop": 1000,
             "CurtainLeft": 2500, "CurtainRight": 2500, "CurtainTop": 2000}

AAP_LEVELS = {0: "Most sensitive", 1: "High sensitivity", 2: "Medium sensitivity", 3: "Low sensitivity"}


def _subkeys(key):
    i = 0
    while True:
        try:
            yield winreg.EnumKey(key, i)
        except OSError:
            return
        i += 1


def _query(key, name):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


class Touchpad:
    """A Precision Touchpad HID collection found under Enum\\HID."""

    def __init__(self, instance_id, name):
        self.instance_id, self.name = instance_id, name
        self.layout = None  # PadLayout when the device is connected

    @property
    def device_location(self):
        return Location("device", "Device key (HKR)", HKLM,
                        "SYSTEM\\CurrentControlSet\\Enum\\" + self.instance_id + "\\Device Parameters")


def find_touchpads():
    """Every HID collection whose hardware ID says Digitizer / Touch Pad (0x0D / 0x05)."""
    pads = []
    try:
        root = winreg.OpenKey(HKLM, ENUM_HID, 0, winreg.KEY_READ | WOW64)
    except OSError:
        return pads
    with root:
        for dev in _subkeys(root):
            try:
                dk = winreg.OpenKey(root, dev, 0, winreg.KEY_READ | WOW64)
            except OSError:
                continue
            with dk:
                for inst in _subkeys(dk):
                    try:
                        with winreg.OpenKey(dk, inst, 0, winreg.KEY_READ | WOW64) as ik:
                            hwids = _query(ik, "HardwareID") or []
                            desc = _query(ik, "DeviceDesc") or ""
                    except OSError:
                        continue
                    if any("UP:000D_U:0005" in h.upper() for h in hwids):
                        pads.append(Touchpad(f"HID\\{dev}\\{inst}", desc.split(";")[-1] or dev))
    return pads


# ---------------------------------------------------------------- raw input / HID

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
hid = ctypes.WinDLL("hid")

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIDI_PREPARSEDDATA = 0x20000005
RIDI_DEVICENAME = 0x20000007
RIM_TYPEHID = 2
RIDEV_INPUTSINK = 0x00000100
HWND_MESSAGE = wt.HWND(-3)
HIDP_STATUS_SUCCESS = 0x00110000
HidP_Input = 0
UINT_ERR = 0xFFFFFFFF

PAGE_DESKTOP, PAGE_DIGITIZER = 0x01, 0x0D
U_X, U_Y = 0x30, 0x31
U_TIP, U_CONFIDENCE, U_WIDTH, U_HEIGHT, U_CONTACT_ID, U_CONTACT_COUNT = 0x42, 0x47, 0x48, 0x49, 0x51, 0x54


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [("hDevice", wt.HANDLE), ("dwType", wt.DWORD)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT),
                ("InputReportByteLength", wt.USHORT), ("OutputReportByteLength", wt.USHORT),
                ("FeatureReportByteLength", wt.USHORT), ("Reserved", wt.USHORT * 17),
                ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT), ("NumberInputValueCaps", wt.USHORT),
                ("NumberInputDataIndices", wt.USHORT),
                ("NumberOutputButtonCaps", wt.USHORT), ("NumberOutputValueCaps", wt.USHORT),
                ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT), ("NumberFeatureValueCaps", wt.USHORT),
                ("NumberFeatureDataIndices", wt.USHORT)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    # The trailing Range/NotRange union starts with Usage (or UsageMin, UsageMax).
    _fields_ = [("UsagePage", wt.USHORT), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
                ("BitField", wt.USHORT), ("LinkCollection", wt.USHORT),
                ("LinkUsage", wt.USHORT), ("LinkUsagePage", wt.USHORT),
                ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte),
                ("IsDesignatorRange", ctypes.c_ubyte), ("IsAbsolute", ctypes.c_ubyte),
                ("HasNull", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte),
                ("BitSize", wt.USHORT), ("ReportCount", wt.USHORT), ("Reserved2", wt.USHORT * 5),
                ("UnitsExp", wt.ULONG), ("Units", wt.ULONG),
                ("LogicalMin", wt.LONG), ("LogicalMax", wt.LONG),
                ("PhysicalMin", wt.LONG), ("PhysicalMax", wt.LONG),
                ("Usage", wt.USHORT), ("UsageMax", wt.USHORT), ("_rest", wt.USHORT * 6)]


def _proto(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, argtypes
    return fn


P = ctypes.POINTER
GetRawInputDeviceList = _proto(user32.GetRawInputDeviceList, wt.UINT, ctypes.c_void_p, P(wt.UINT), wt.UINT)
GetRawInputDeviceInfoW = _proto(user32.GetRawInputDeviceInfoW, wt.UINT, wt.HANDLE, wt.UINT, ctypes.c_void_p, P(wt.UINT))
GetRawInputData = _proto(user32.GetRawInputData, wt.UINT, wt.HANDLE, wt.UINT, ctypes.c_void_p, P(wt.UINT), wt.UINT)
RegisterRawInputDevices = _proto(user32.RegisterRawInputDevices, wt.BOOL, P(RAWINPUTDEVICE), wt.UINT, wt.UINT)
DefWindowProcW = _proto(user32.DefWindowProcW, LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
RegisterClassW = _proto(user32.RegisterClassW, wt.ATOM, P(WNDCLASSW))
CreateWindowExW = _proto(user32.CreateWindowExW, wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
GetMessageW = _proto(user32.GetMessageW, wt.BOOL, P(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
TranslateMessage = _proto(user32.TranslateMessage, wt.BOOL, P(wt.MSG))
DispatchMessageW = _proto(user32.DispatchMessageW, LRESULT, P(wt.MSG))
GetModuleHandleW = _proto(kernel32.GetModuleHandleW, wt.HMODULE, wt.LPCWSTR)
HidP_GetCaps = _proto(hid.HidP_GetCaps, ctypes.c_long, ctypes.c_void_p, P(HIDP_CAPS))
HidP_GetValueCaps = _proto(hid.HidP_GetValueCaps, ctypes.c_long, ctypes.c_int, P(HIDP_VALUE_CAPS),
                           P(wt.USHORT), ctypes.c_void_p)
HidP_GetUsageValue = _proto(hid.HidP_GetUsageValue, ctypes.c_long, ctypes.c_int, wt.USHORT, wt.USHORT,
                            wt.USHORT, P(wt.ULONG), ctypes.c_void_p, ctypes.c_void_p, wt.ULONG)
HidP_GetUsages = _proto(hid.HidP_GetUsages, ctypes.c_long, ctypes.c_int, wt.USHORT, wt.USHORT,
                        P(wt.USHORT), P(wt.ULONG), ctypes.c_void_p, ctypes.c_void_p, wt.ULONG)
HidP_MaxUsageListLength = _proto(hid.HidP_MaxUsageListLength, wt.ULONG, ctypes.c_int, wt.USHORT, ctypes.c_void_p)


def _ok(status):
    return (status & 0xFFFFFFFF) == HIDP_STATUS_SUCCESS


def _logical(cap):
    lo, hi = cap.LogicalMin, cap.LogicalMax
    if hi < lo:  # unsigned max misread as negative
        hi &= (1 << cap.BitSize) - 1
    return lo, hi


def _mm_per_unit(cap):
    """Millimetres per logical unit, from the HID physical range and unit, or None."""
    lo, hi = _logical(cap)
    span = cap.PhysicalMax - cap.PhysicalMin
    if hi == lo or span <= 0 or (cap.Units >> 4) & 0xF != 1:
        return None
    unit_mm = {1: 10.0, 3: 25.4}.get(cap.Units & 0xF)  # SI linear = cm, English linear = inch
    if unit_mm is None:
        return None
    exp = cap.UnitsExp & 0xF
    exp = exp - 16 if exp > 7 else exp
    return span * (10.0 ** exp) * unit_mm / (hi - lo)


class PadLayout:
    """Where the contact fields live in one touchpad's input reports."""

    def __init__(self, handle, path, preparsed):
        self.handle, self.path = handle, path
        self._buf = preparsed
        self.pp = ctypes.addressof(preparsed)
        caps = HIDP_CAPS()
        if not _ok(HidP_GetCaps(self.pp, ctypes.byref(caps))):
            raise OSError("HidP_GetCaps failed")
        self.usage = (caps.UsagePage, caps.Usage)
        n = wt.USHORT(caps.NumberInputValueCaps)
        arr = (HIDP_VALUE_CAPS * max(1, n.value))()
        if n.value and not _ok(HidP_GetValueCaps(HidP_Input, arr, ctypes.byref(n), self.pp)):
            raise OSError("HidP_GetValueCaps failed")
        found = {}
        for c in arr[:n.value]:
            for u in (range(c.Usage, c.UsageMax + 1) if c.IsRange else (c.Usage,)):
                found.setdefault((c.UsagePage, u, c.LinkCollection), c)
        self.fingers = sorted(lc for (p, u, lc) in found
                              if (p, u) == (PAGE_DESKTOP, U_X) and (PAGE_DESKTOP, U_Y, lc) in found)
        self.count_lc = next((lc for (p, u, lc) in found if (p, u) == (PAGE_DIGITIZER, U_CONTACT_COUNT)), None)
        self.has_id = {lc: (PAGE_DIGITIZER, U_CONTACT_ID, lc) in found for lc in self.fingers}
        self.size_caps = {lc: (found.get((PAGE_DIGITIZER, U_WIDTH, lc)), found.get((PAGE_DIGITIZER, U_HEIGHT, lc)))
                          for lc in self.fingers}
        self.max_buttons = max(1, HidP_MaxUsageListLength(HidP_Input, PAGE_DIGITIZER, self.pp))
        self.remaining = 0
        self.size_mm = None
        if self.fingers:
            xc = found[(PAGE_DESKTOP, U_X, self.fingers[0])]
            yc = found[(PAGE_DESKTOP, U_Y, self.fingers[0])]
            self.x_range, self.y_range = _logical(xc), _logical(yc)
            sx, sy = _mm_per_unit(xc), _mm_per_unit(yc)
            if sx and sy:
                self.size_mm = ((self.x_range[1] - self.x_range[0]) * sx,
                                (self.y_range[1] - self.y_range[0]) * sy)

    @property
    def instance_id(self):
        # \\?\HID#VEN_XXXX&Col02#3&1a2b3c4d&0&0001#{guid}  ->  HID\VEN_XXXX&Col02\3&1a2b3c4d&0&0001
        parts = self.path[4:].split("#")
        return "\\".join(parts[:3]).upper() if len(parts) >= 3 else self.path.upper()

    def parse(self, rep, length):
        """Contacts in one input report, or None if it is not a touch report."""
        pp = self.pp

        def value(page, lc, usage):
            v = wt.ULONG()
            if _ok(HidP_GetUsageValue(HidP_Input, page, lc, usage, ctypes.byref(v), pp, rep, length)):
                return v.value
            return None

        if self.count_lc is not None:
            count = value(PAGE_DIGITIZER, self.count_lc, U_CONTACT_COUNT)
            if count is None:
                return None
            if count:  # first report of a frame; hybrid mode spreads the rest over later reports
                self.remaining = count
            n = min(self.remaining, len(self.fingers))
            self.remaining -= n
        else:
            n = len(self.fingers)

        buttons = (wt.USHORT * self.max_buttons)()
        out = []
        for lc in self.fingers[:n]:
            x, y = value(PAGE_DESKTOP, lc, U_X), value(PAGE_DESKTOP, lc, U_Y)
            if x is None or y is None:
                continue
            cnt = wt.ULONG(self.max_buttons)
            pressed = set()
            if _ok(HidP_GetUsages(HidP_Input, PAGE_DIGITIZER, lc, buttons, ctypes.byref(cnt), pp, rep, length)):
                pressed = set(buttons[:cnt.value])
            cid = value(PAGE_DIGITIZER, lc, U_CONTACT_ID) if self.has_id[lc] else None
            size = []
            for cap, usage in zip(self.size_caps[lc], (U_WIDTH, U_HEIGHT)):
                scale = _mm_per_unit(cap) if cap else None
                v = value(PAGE_DIGITIZER, lc, usage) if scale else None
                size.append(v * scale if v else None)
            (xlo, xhi), (ylo, yhi) = self.x_range, self.y_range
            out.append({
                "id": cid if cid is not None else lc,
                "tip": U_TIP in pressed,
                "confident": U_CONFIDENCE in pressed,
                "nx": (x - xlo) / max(1, xhi - xlo),
                "ny": (y - ylo) / max(1, yhi - ylo),
                "w_mm": size[0], "h_mm": size[1],
            })
        return out


def open_layout(handle):
    size = wt.UINT(0)
    GetRawInputDeviceInfoW(handle, RIDI_PREPARSEDDATA, None, ctypes.byref(size))
    if not size.value:
        return None
    pp = ctypes.create_string_buffer(size.value)
    if GetRawInputDeviceInfoW(handle, RIDI_PREPARSEDDATA, pp, ctypes.byref(size)) in (0, UINT_ERR):
        return None
    n = wt.UINT(0)
    GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, None, ctypes.byref(n))
    name = ctypes.create_unicode_buffer(n.value + 1)
    GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, name, ctypes.byref(n))
    try:
        layout = PadLayout(handle, name.value, pp)
    except OSError:
        return None
    return layout if layout.usage == (PAGE_DIGITIZER, 0x05) and layout.fingers else None


def list_touchpad_layouts():
    n, sz = wt.UINT(0), ctypes.sizeof(RAWINPUTDEVICELIST)
    if GetRawInputDeviceList(None, ctypes.byref(n), sz) == UINT_ERR or not n.value:
        return []
    arr = (RAWINPUTDEVICELIST * n.value)()
    got = GetRawInputDeviceList(arr, ctypes.byref(n), sz)
    if got == UINT_ERR:
        return []
    layouts = (open_layout(d.hDevice) for d in arr[:got] if d.dwType == RIM_TYPEHID)
    return [l for l in layouts if l]


class TouchReader(threading.Thread):
    """Receives touchpad reports through Raw Input on a hidden message-only window."""

    STALE = 0.25  # seconds without a report before a contact is dropped

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.contacts = {}
        self.layouts = {}
        self.error = None
        self._proc = WNDPROC(self._wndproc)

    def run(self):
        try:
            hinst = GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName="PTPZonesRawInput")
            if not RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
            hwnd = CreateWindowExW(0, wc.lpszClassName, "ptp-zones", 0, 0, 0, 0, 0,
                                   HWND_MESSAGE, None, hinst, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            rid = RAWINPUTDEVICE(PAGE_DIGITIZER, 0x05, RIDEV_INPUTSINK, hwnd)
            if not RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError as e:
            self.error = str(e)
            return
        msg = wt.MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == WM_INPUT:
            try:
                self._on_input(lp)
            except Exception as e:  # never let an exception escape into the window procedure
                self.error = f"report parse failed: {e}"
        return DefWindowProcW(hwnd, msg, wp, lp)

    def _on_input(self, lp):
        hdr_size = ctypes.sizeof(RAWINPUTHEADER)
        size = wt.UINT(0)
        GetRawInputData(lp, RID_INPUT, None, ctypes.byref(size), hdr_size)
        buf = ctypes.create_string_buffer(size.value)
        if GetRawInputData(lp, RID_INPUT, buf, ctypes.byref(size), hdr_size) != size.value:
            return
        hdr = RAWINPUTHEADER.from_buffer(buf)
        if hdr.dwType != RIM_TYPEHID:
            return
        if hdr.hDevice not in self.layouts:
            self.layouts[hdr.hDevice] = open_layout(hdr.hDevice)
        layout = self.layouts[hdr.hDevice]
        if layout is None:
            return
        size_hid, count = struct.unpack_from("<II", buf, hdr_size)
        base = ctypes.addressof(buf) + hdr_size + 8
        now = time.monotonic()
        for i in range(count):
            contacts = layout.parse(base + i * size_hid, size_hid)
            if not contacts:
                continue
            with self.lock:
                for c in contacts:
                    key = (layout.handle, c["id"])
                    if not c["tip"]:
                        self.contacts.pop(key, None)
                        continue
                    cur = self.contacts.get(key)
                    if cur is None or now - cur["t"] > self.STALE:
                        cur = self.contacts[key] = {"start": (c["nx"], c["ny"]), "device": layout.instance_id}
                    cur.update(c, t=now)

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            for k in [k for k, c in self.contacts.items() if now - c["t"] > self.STALE]:
                del self.contacts[k]
            return [dict(c) for c in self.contacts.values()]


# ---------------------------------------------------------------- GUI

BG, PANEL, FIELD, BORDER = "#15171c", "#1e222a", "#262b35", "#363d4a"
FG, MUTED, ACCENT = "#e7e9ee", "#8f98a8", "#4c9aff"
PAD_FILL, PAD_EDGE, GRID = "#2b303a", "#6b7587", "#353b47"
SUPER_FILL, SUPER_EDGE = "#7d2a31", "#ff5c63"
CURTAIN_FILL, CURTAIN_EDGE = "#6e5219", "#f5a524"
LEGACY_FILL = "#4a3d22"
RCZ_EDGE = "#4c9aff"
ZONE_COLORS = {"super": SUPER_EDGE, "curtain": CURTAIN_EDGE, "free": "#46d18a"}
ZONE_TEXT = {"super": "supercurtain: taps rejected",
             "curtain": "curtain: suppressed shortly after typing",
             "free": "free zone: always accepted"}


class Row:
    """A checkbox + spin box bound to one registry value. Unchecked = value absent."""

    def __init__(self, app, parent, setting, r, label):
        self.app, self.setting = app, setting
        self.on = tk.BooleanVar()
        self.var = tk.StringVar()
        self.loaded = (False, setting.default)
        ttk.Checkbutton(parent, text=label, variable=self.on, command=app.changed).grid(
            row=r, column=0, sticky="w", pady=1)
        self.spin = ttk.Spinbox(parent, textvariable=self.var, from_=0, to=100 if setting.percent else 300,
                                increment=1 if setting.percent else 0.5, width=6, command=self._edited)
        self.spin.grid(row=r, column=1, padx=(6, 2))
        ttk.Label(parent, text="%" if setting.percent else "mm", style="Muted.TLabel").grid(row=r, column=2, sticky="w")
        self.dot = ttk.Label(parent, text="", style="Accent.TLabel", width=2)
        self.dot.grid(row=r, column=3)
        self.source = ttk.Label(parent, text="", style="Muted.TLabel")
        self.source.grid(row=r, column=4, sticky="w")
        self.var.trace_add("write", lambda *_: self._edited())

    def _edited(self):
        if self.app.loading:
            return
        self.on.set(True)
        self.app.changed()

    def get(self):
        """Value in registry units (himetric or percent), or None if the text isn't a number."""
        try:
            v = float(self.var.get())
        except ValueError:
            return None
        if v < 0:
            return None
        return int(round(v)) if self.setting.percent else int(round(v * 100))

    def set(self, reg_value):
        self.var.set(str(reg_value) if self.setting.percent else f"{reg_value / 100:g}")

    def effective(self):
        v = self.get() if self.on.get() else None
        return self.setting.default if v is None else v

    @property
    def dirty(self):
        on = self.on.get()
        return on != self.loaded[0] or (on and self.get() != self.loaded[1])


class App:
    def __init__(self, root):
        self.root = root
        self.ui = root.winfo_fpixels("1i") / 96.0
        self.is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        self.loading = False
        self.drag = None
        self.present, self.read_errors = {}, {}
        self.legacy = dict(LEGACY_DEFAULTS)

        self.global_loc = Location("global", "Global (HKLM)", HKLM, PTP_PATH)
        self.user_loc = Location("user", "User (HKCU)", HKCU, PTP_PATH)

        layouts = {l.instance_id: l for l in list_touchpad_layouts()}
        self.pads = find_touchpads()
        for p in self.pads:
            p.layout = layouts.get(p.instance_id.upper())
        self.pads.sort(key=lambda p: p.layout is None)
        self.pad = self.pads[0] if self.pads else None

        self.reader = TouchReader()
        self.reader.start()

        self._style()
        self._build()
        self.select_pad()
        self.root.after(30, self._tick)

    # ------------------------------------------------------------ layout

    def _style(self):
        root, st = self.root, ttk.Style(self.root)
        st.theme_use("clam")
        root.configure(bg=BG)
        font = ("Segoe UI", 10)
        st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=BORDER,
                     lightcolor=BG, darkcolor=BG, troughcolor=PANEL, focuscolor=BORDER,
                     selectbackground=ACCENT, selectforeground="#0b1220", font=font, arrowcolor=FG,
                     insertcolor=FG)
        st.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="solid")
        st.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=("Segoe UI Semibold", 10))
        st.configure("Muted.TLabel", foreground=MUTED)
        st.configure("Accent.TLabel", foreground=ACCENT)
        st.configure("Warn.TLabel", foreground=CURTAIN_EDGE)
        st.configure("Ok.TLabel", foreground="#46d18a")
        st.configure("Title.TLabel", font=("Segoe UI Semibold", 15))
        st.configure("TButton", background=PANEL, padding=(10, 4))
        st.map("TButton", background=[("disabled", BG), ("active", "#2c323d")],
               foreground=[("disabled", MUTED)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#0b1220",
                     font=("Segoe UI Semibold", 10))
        st.map("Accent.TButton", background=[("active", "#6fb0ff")])
        for w in ("TCheckbutton", "TRadiobutton"):
            st.configure(w, indicatorbackground=FIELD, indicatorforeground=ACCENT,
                         upperbordercolor=BORDER, lowerbordercolor=BORDER)
            st.map(w, background=[("active", BG)], indicatorbackground=[("pressed", BORDER)],
                   foreground=[("disabled", MUTED)])
        st.configure("TSpinbox", padding=(4, 1))
        st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)],
               selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
        root.option_add("*TCombobox*Listbox.background", FIELD)
        root.option_add("*TCombobox*Listbox.foreground", FG)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        root.option_add("*TCombobox*Listbox.font", font)

    def _build(self):
        root, u = self.root, self.ui
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        head = ttk.Frame(root, padding=(14, 10, 14, 4))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(2, weight=1)
        ttk.Label(head, text="Precision Touchpad Zones", style="Title.TLabel").grid(row=0, column=0, padx=(0, 18))
        ttk.Label(head, text="Device", style="Muted.TLabel").grid(row=0, column=1, padx=(0, 6))
        self.device_box = ttk.Combobox(head, state="readonly", width=60,
                                       values=[self._pad_title(p) for p in self.pads] or ["No Precision Touchpad found"])
        self.device_box.current(0)
        self.device_box.bind("<<ComboboxSelected>>", lambda e: self.select_pad())
        self.device_box.grid(row=0, column=2, sticky="w")
        if self.is_admin:
            ttk.Label(head, text="● Administrator", style="Ok.TLabel").grid(row=0, column=3, padx=(10, 0))
        else:
            ttk.Label(head, text="● Read-only (not elevated)", style="Warn.TLabel").grid(row=0, column=3, padx=(10, 6))
            ttk.Button(head, text="Restart as admin", command=self.relaunch_admin).grid(row=0, column=4)

        body = ttk.Frame(root, padding=(14, 6, 14, 6))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(left, bg=BG, highlightthickness=0, width=int(700 * u), height=int(470 * u))
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))

        legend = ttk.Frame(left)
        legend.grid(row=1, column=0, sticky="w", pady=(6, 0))
        for color, text in ((SUPER_EDGE, "Supercurtain"), (CURTAIN_EDGE, "Curtain"),
                            ("#a08a5a", "Implicit curtain (legacy)"), (RCZ_EDGE, "Right-click zone"),
                            ("#46d18a", "Free zone")):
            ttk.Label(legend, text="■", foreground=color).pack(side="left")
            ttk.Label(legend, text=text, style="Muted.TLabel").pack(side="left", padx=(3, 14))
        self.touch_info = ttk.Label(left, text="", style="Muted.TLabel", justify="left")
        self.touch_info.grid(row=2, column=0, sticky="w", pady=(4, 0))

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="ns")
        self.rows = {}

        size = ttk.LabelFrame(right, text="Pad size", padding=(10, 6))
        size.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.pad_w, self.pad_h = tk.StringVar(value="105"), tk.StringVar(value="65")
        for i, (lbl, var) in enumerate((("W", self.pad_w), ("H", self.pad_h))):
            ttk.Label(size, text=lbl, style="Muted.TLabel").grid(row=0, column=i * 3, padx=(0 if i == 0 else 12, 4))
            ttk.Spinbox(size, textvariable=var, from_=20, to=300, increment=0.5, width=7,
                        command=self.redraw).grid(row=0, column=i * 3 + 1)
            ttk.Label(size, text="mm", style="Muted.TLabel").grid(row=0, column=i * 3 + 2, padx=(2, 0))
            var.trace_add("write", lambda *_: self.redraw())
        self.size_note = ttk.Label(size, text="", style="Muted.TLabel")
        self.size_note.grid(row=1, column=0, columnspan=6, sticky="w", pady=(2, 0))

        groups = (
            ("super", "Supercurtain · taps almost always rejected"),
            ("curtain", "Curtain · suppressed shortly after typing"),
            ("rcz", "Right-click zone (bottom corner)"),
        )
        for gi, (group, title) in enumerate(groups, start=1):
            f = ttk.LabelFrame(right, text=title, padding=(10, 4))
            f.grid(row=gi, column=0, sticky="ew", pady=(0, 8))
            f.columnconfigure(4, weight=1)
            r = 0
            for s in SETTINGS:
                if s.group != group:
                    continue
                label = {"W": "Width", "H": "Height"}.get(s.side) or SIDES[s.side]
                if s.name == "CurtainBottom":
                    label += " (opt.)"
                self.rows[s.name] = Row(self, f, s, r, label)
                r += 1
            if group == "curtain":
                self.curtain_warn = ttk.Label(f, text="", style="Warn.TLabel", wraplength=int(330 * u))
                self.curtain_warn.grid(row=r, column=0, columnspan=5, sticky="w")
                self.aap_info = ttk.Label(f, text="", style="Muted.TLabel", wraplength=int(330 * u))
                self.aap_info.grid(row=r + 1, column=0, columnspan=5, sticky="w")

        target = ttk.LabelFrame(right, text="Write to", padding=(10, 4))
        target.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        self.mode = tk.StringVar(value="auto")
        ttk.Radiobutton(target, text="Auto: wherever each value takes effect", value="auto",
                        variable=self.mode).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(target, text="Global key (HKLM\\…\\PrecisionTouchPad)", value="global",
                        variable=self.mode).grid(row=1, column=0, sticky="w")
        self.device_radio = ttk.Radiobutton(target, text="Device key (Enum\\HID\\…\\Device Parameters)",
                                            value="device", variable=self.mode)
        self.device_radio.grid(row=2, column=0, sticky="w")
        self.target_info = ttk.Label(target, text="", style="Muted.TLabel", wraplength=int(330 * u))
        self.target_info.grid(row=3, column=0, sticky="w", pady=(2, 0))

        btns = ttk.Frame(right)
        btns.grid(row=5, column=0, sticky="ew")
        for c in range(3):
            btns.columnconfigure(c, weight=1, uniform="b")
        ttk.Button(btns, text="Suggested", command=self.preset_suggested).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Windows defaults", command=self.preset_defaults).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Revert", command=self.load).grid(row=0, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Restore .reg…", command=self.restore).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Restart touchpad", command=self.restart_touchpad).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Apply…", style="Accent.TButton", command=self.apply).grid(row=1, column=2, sticky="ew", padx=2, pady=2)

        self.status = ttk.Label(root, text="", style="Muted.TLabel", padding=(14, 2, 14, 8))
        self.status.grid(row=2, column=0, sticky="ew")

    @staticmethod
    def _pad_title(p):
        return f"{p.name}  ·  {p.instance_id}" + ("" if p.layout else "  (not connected)")

    # ------------------------------------------------------------ data

    def select_pad(self):
        if self.pads:
            self.pad = self.pads[self.device_box.current()]
        size = self.pad.layout.size_mm if self.pad and self.pad.layout else None
        if size:
            self.pad_w.set(f"{size[0]:.1f}")
            self.pad_h.set(f"{size[1]:.1f}")
            self.size_note.config(text="Read from the touchpad's HID descriptor.")
        else:
            self.size_note.config(text="Not reported by the device: measure the pad and enter it.")
        if self.pad is None:
            self.device_radio.state(["disabled"])
            if self.mode.get() == "device":
                self.mode.set("auto")
        self.load()

    def locations(self, setting=None):
        """Locations in precedence order (first one that has the value wins)."""
        locs = []
        if setting is None or setting.group == "rcz":
            locs.append(self.user_loc)
        if self.pad:
            locs.append(self.pad.device_location)
        locs.append(self.global_loc)
        return locs

    def load(self):
        names = [s.name for s in SETTINGS] + list(LEGACY_DEFAULTS)
        self.present, self.read_errors = {}, {}
        for loc in self.locations():
            vals, err = loc.read(names)
            self.present[loc.key] = vals
            if err:
                self.read_errors[loc.key] = err
        # HKCU only overrides the right-click zone, and only when non-zero.
        self.present["user"] = {k: v for k, v in self.present["user"].items()
                                if k.startswith("RightClickZone") and v}

        self.loading = True
        for s in SETTINGS:
            row = self.rows[s.name]
            hits = [(loc, self.present[loc.key][s.name]) for loc in self.locations(s)
                    if s.name in self.present[loc.key]]
            if hits:
                row.on.set(True)
                row.set(hits[0][1])
                row.loaded = (True, hits[0][1])
                text = "from " + hits[0][0].label
                if len(hits) > 1:
                    text += f" (shadows {hits[1][0].label.split(' ')[0]} = {self._fmt(s, hits[1][1])})"
            else:
                row.on.set(False)
                row.set(s.default)
                row.loaded = (False, s.default)
                text = "not set, Windows default"
            row.source.config(text=text)
        self.loading = False

        for name, default in LEGACY_DEFAULTS.items():
            self.legacy[name] = next((self.present[l.key][name] for l in self.locations()[1:]
                                      if name in self.present[l.key]), default)

        aap, _ = self.user_loc.read(["AAPThreshold"])
        disabled, _ = self.global_loc.read(["AAPDisabled"])
        if disabled.get("AAPDisabled"):
            self.aap_info.config(text="AAPDisabled = 1 in HKLM: curtains never suppress input.")
        else:
            level = aap.get("AAPThreshold", 2)
            self.aap_info.config(text=f"Touchpad sensitivity: {AAP_LEVELS.get(level, level)}. "
                                      "Lower sensitivity = curtains suppress input for longer after typing.")

        if self.pad:
            dev = self.present.get("device", {})
            held = [s.name for s in SETTINGS if s.name in dev]
            if "device" in self.read_errors:
                info = f"Device key unreadable ({self.read_errors['device']})."
            elif held:
                info = "Device key holds " + ", ".join(held) + ". These override HKLM."
            else:
                info = "Device key holds none of these values, so the global HKLM key is what counts."
        else:
            info = "No touchpad device key; only the global key is available."
        self.target_info.config(text=info)
        self.changed()
        errs = "; ".join(f"{k}: {v}" for k, v in self.read_errors.items())
        self.status.config(text=("Read errors: " + errs) if errs else "Loaded from the registry.")

    @staticmethod
    def _fmt(s, v):
        return f"{v}%" if s.percent else f"{v / 100:g} mm"

    def changed(self):
        if self.loading:
            return
        dirty = 0
        for row in self.rows.values():
            row.dot.config(text="●" if row.dirty else "")
            dirty += row.dirty
        on = [n for n in ("CurtainLeft", "CurtainRight", "CurtainTop") if self.rows[n].on.get()]
        if 0 < len(on) < 3:
            self.curtain_warn.config(text="Curtains are ignored until Left, Right and Top are all set. "
                                          "Windows falls back to the implicit curtain.")
        else:
            self.curtain_warn.config(text="")
        if dirty:
            self.status.config(text=f"{dirty} pending change{'s' if dirty > 1 else ''}. Press Apply to write them.")
        self.redraw()

    def preset_suggested(self):
        for name, v in SUGGESTED.items():
            self.rows[name].set(v)
            self.rows[name].on.set(True)
        self.changed()

    def preset_defaults(self):
        for row in self.rows.values():
            if row.setting.group in ("super", "curtain"):
                row.on.set(False)
                row.set(row.setting.default)
        self.changed()

    # ------------------------------------------------------------ geometry

    def pad_size(self):
        try:
            w, h = float(self.pad_w.get()), float(self.pad_h.get())
            if w > 5 and h > 5:
                return w, h
        except ValueError:
            pass
        return 105.0, 65.0

    def zones(self):
        W, H = self.pad_size()
        mm = lambda n: self.rows[n].effective() / 100.0
        z = {"W": W, "H": H,
             "super": {s: mm("SuperCurtain" + n) for s, n in SIDES.items()},
             "curtain": {s: mm("Curtain" + n) for s, n in SIDES.items()},
             "explicit": all(self.rows[n].on.get() and self.rows[n].get() is not None
                             for n in ("CurtainLeft", "CurtainRight", "CurtainTop")),
             "rcz": (self.rows["RightClickZoneWidth"].effective(), self.rows["RightClickZoneHeight"].effective())}
        lg = self.legacy
        center = W / 2 - lg["HorizontalOffset"] / 100.0
        half = (lg["SpaceBarOffset"] + lg["AAPNonCurtainBaseWidth"]) / 100.0
        top = max(0.0, (lg["AAPNonCurtainTop"] - lg["SpaceBarOffset"]) / 100.0)
        z["free"] = (max(0.0, center - half), min(H, top), min(W, center + half))
        return z

    def zone_at(self, z, mx, my):
        W, H, sc = z["W"], z["H"], z["super"]
        if mx < sc["L"] or mx > W - sc["R"] or my < sc["T"] or my > H - sc["B"]:
            return "super"
        if z["explicit"]:
            c = z["curtain"]
            if mx < c["L"] or mx > W - c["R"] or my < c["T"] or my > H - c["B"]:
                return "curtain"
        else:
            fx0, fy0, fx1 = z["free"]
            if not (fx0 <= mx <= fx1 and my >= fy0):
                return "curtain"
        return "free"

    def geom(self):
        u = self.ui
        cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        W, H = self.pad_size()
        ml, mt, mr, mb = 70 * u, 50 * u, 30 * u, 40 * u
        s = max(0.1, min((cw - ml - mr) / W, (ch - mt - mb) / H))
        ox = ml + ((cw - ml - mr) - W * s) / 2
        oy = mt + ((ch - mt - mb) - H * s) / 2
        return ox, oy, s, W, H

    # ------------------------------------------------------------ drawing

    def redraw(self):
        if not hasattr(self, "canvas"):
            return
        c, u = self.canvas, self.ui
        c.delete("zones")
        ox, oy, s, W, H = self.geom()
        z = self.zones()
        X = lambda mx: ox + mx * s
        Y = lambda my: oy + my * s

        def rect(x0, y0, x1, y1, fill):
            if x1 > x0 and y1 > y0:
                c.create_rectangle(X(x0), Y(y0), X(x1), Y(y1), fill=fill, width=0, tags="zones")

        def bands(d, fill):
            rect(0, 0, d["L"], H, fill)
            rect(W - d["R"], 0, W, H, fill)
            rect(0, 0, W, d["T"], fill)
            rect(0, H - d["B"], W, H, fill)

        rect(0, 0, W, H, PAD_FILL)
        if z["explicit"]:
            bands(z["curtain"], CURTAIN_FILL)
        else:
            fx0, fy0, fx1 = z["free"]
            rect(0, 0, W, H, LEGACY_FILL)
            rect(fx0, fy0, fx1, H, PAD_FILL)
        bands(z["super"], SUPER_FILL)

        for gx in range(10, int(W), 10):
            c.create_line(X(gx), Y(0), X(gx), Y(H), fill=GRID, tags="zones")
        for gy in range(10, int(H), 10):
            c.create_line(X(0), Y(gy), X(W), Y(gy), fill=GRID, tags="zones")

        # Right-click zone, mirrored when mouse buttons are swapped.
        wp, hp = z["rcz"]
        rw, rh = W * min(wp, 100) / 100.0, H * min(hp, 100) / 100.0
        x0 = 0 if user32.GetSystemMetrics(23) else W - rw
        c.create_rectangle(X(x0), Y(H - rh), X(x0 + rw), Y(H), outline=RCZ_EDGE, dash=(6, 4),
                           width=max(1, int(1.5 * u)), tags="zones")
        c.create_text(X(x0 + rw / 2), Y(H - rh) + 10 * u, text="right-click", fill=RCZ_EDGE,
                      font=("Segoe UI", 8), tags="zones")

        if not z["explicit"]:
            fx0, fy0, fx1 = z["free"]
            c.create_text(X((fx0 + fx1) / 2), Y(fy0) + 12 * u, text="implicit curtain: free column (approx.)",
                          fill="#a08a5a", font=("Segoe UI", 8), tags="zones")

        # Inner edges (draggable) and their values in the margins.
        lw = max(2, int(2 * u))
        for group, color, active in (("curtain", CURTAIN_EDGE, z["explicit"]), ("super", SUPER_EDGE, True)):
            d = z[group]
            for side in "LTRB":
                v = d[side]
                if group == "curtain" and not self.rows["Curtain" + SIDES[side]].on.get():
                    continue
                dash = () if active else (3, 3)
                label = f"{v:g}"
                if side in "LR":
                    x = X(v) if side == "L" else X(W - v)
                    c.create_line(x, Y(0), x, Y(H), fill=color, width=lw, dash=dash, tags="zones")
                    ty = oy - (30 if group == "curtain" else 14) * u
                    c.create_text(x, ty, text=label, fill=color, font=("Segoe UI Semibold", 9), tags="zones")
                else:
                    y = Y(v) if side == "T" else Y(H - v)
                    c.create_line(X(0), y, X(W), y, fill=color, width=lw, dash=dash, tags="zones")
                    tx = ox - (36 if group == "curtain" else 10) * u
                    c.create_text(tx, y, text=label, fill=color, anchor="e", font=("Segoe UI Semibold", 9), tags="zones")

        c.create_rectangle(X(0), Y(0), X(W), Y(H), outline=PAD_EDGE, width=max(1, int(1.5 * u)), tags="zones")
        c.create_text(X(W / 2), Y(H) + 16 * u, text=f"{W:g} × {H:g} mm  ·  grid 10 mm  ·  values in mm  ·  keyboard side ↑",
                      fill=MUTED, font=("Segoe UI", 9), tags="zones")
        c.tag_raise("touch")

    def _tick(self):
        c, u = self.canvas, self.ui
        c.delete("touch")
        ox, oy, s, W, H = self.geom()
        z = self.zones()
        contacts = self.reader.snapshot()
        if self.pad and self.pad.layout:
            contacts = [t for t in contacts if t["device"] == self.pad.instance_id.upper()]
        lines = []
        for t in sorted(contacts, key=lambda t: t["id"]):
            sx, sy = t["start"][0] * W, t["start"][1] * H
            mx, my = t["nx"] * W, t["ny"] * H
            zone = self.zone_at(z, sx, sy)
            color = ZONE_COLORS[zone]
            rx = (t["w_mm"] / 2 * s) if t["w_mm"] else 9 * u
            ry = (t["h_mm"] / 2 * s) if t["h_mm"] else rx
            px, py = ox + mx * s, oy + my * s
            c.create_line(ox + sx * s, oy + sy * s, px, py, fill=color, dash=(2, 3), tags="touch")
            c.create_text(ox + sx * s, oy + sy * s, text="×", fill=color, font=("Segoe UI", 9), tags="touch")
            if t["confident"]:
                c.create_oval(px - rx, py - ry, px + rx, py + ry, fill=color, outline="white", width=1, tags="touch")
            else:
                c.create_oval(px - rx, py - ry, px + rx, py + ry, outline=color, dash=(4, 3), width=2, tags="touch")
                c.create_text(px, py - ry - 8 * u, text="palm", fill=color, font=("Segoe UI", 8), tags="touch")
            c.create_text(px, py, text=str(t["id"]), fill="#0b1220" if t["confident"] else color,
                          font=("Segoe UI Semibold", 8), tags="touch")
            size = f", contact {t['w_mm']:.1f}×{t['h_mm']:.1f} mm" if t["w_mm"] and t["h_mm"] else ""
            palm = ", flagged as palm" if not t["confident"] else ""
            lines.append(f"#{t['id']} started in {ZONE_TEXT[zone]}{palm}{size}")

        if self.reader.error:
            info = "Live view unavailable: " + self.reader.error
        elif lines:
            info = "\n".join(lines[:5])
        elif self.pad and not self.pad.layout:
            info = "This device isn't connected, so live touches can't be shown."
        else:
            info = ("Touch the pad to see contacts. A contact's colour shows where it started, "
                    "because the zones only judge where a touch starts.")
        if self.touch_info.cget("text") != info:
            self.touch_info.config(text=info)
        self.root.after(16, self._tick)

    # ------------------------------------------------------------ dragging edges

    def _hit(self, ex, ey):
        ox, oy, s, W, H = self.geom()
        tol = 7 * self.ui
        if not (ox - tol <= ex <= ox + W * s + tol and oy - tol <= ey <= oy + H * s + tol):
            return None
        best = None
        for group, prefix in (("super", "SuperCurtain"), ("curtain", "Curtain")):
            for side, sname in SIDES.items():
                row = self.rows[prefix + sname]
                v = row.effective() / 100.0
                pos = {"L": ox + v * s, "R": ox + (W - v) * s, "T": oy + v * s, "B": oy + (H - v) * s}[side]
                p = ex if side in "LR" else ey
                d = abs(p - pos)
                if d > tol:
                    continue
                inward = (p > pos) if side in "LT" else (p < pos)
                # When edges coincide, pulling inward grabs the curtain and outward the supercurtain.
                rank = (d, 0 if (group == "curtain") == inward else 1)
                if best is None or rank < best[0]:
                    best = (rank, row, side)
        return best and (best[1], best[2])

    def _on_motion(self, e):
        if self.drag:
            return
        hit = self._hit(e.x, e.y)
        self.canvas.config(cursor="" if not hit else ("sb_h_double_arrow" if hit[1] in "LR" else "sb_v_double_arrow"))

    def _on_press(self, e):
        self.drag = self._hit(e.x, e.y)

    def _on_drag(self, e):
        if not self.drag:
            return
        row, side = self.drag
        ox, oy, s, W, H = self.geom()
        mx, my = (e.x - ox) / s, (e.y - oy) / s
        v = {"L": mx, "R": W - mx, "T": my, "B": H - my}[side]
        limit = (W if side in "LR" else H) / 2
        v = round(max(0.0, min(limit, v)) * 2) / 2
        row.on.set(True)
        row.set(int(round(v * 100)))

    # ------------------------------------------------------------ writing

    def plan(self):
        ops, warnings = [], []
        mode = self.mode.get()
        for s in SETTINGS:
            row = self.rows[s.name]
            on, val = row.on.get(), row.get()
            if on and val is None:
                raise ValueError(f"{s.name}: '{row.var.get()}' is not a valid number.")
            if on and s.percent and not 1 <= val <= 100:
                raise ValueError(f"{s.name}: must be 1-100 %.")
            locs = self.locations(s)
            present = [(l, self.present[l.key][s.name]) for l in locs if s.name in self.present[l.key]]
            if mode == "auto":
                if on and (not present or present[0][1] != val):
                    ops.append(("set", present[0][0] if present else self.global_loc, s.name, val))
                elif not on:
                    ops += [("delete", l, s.name, None) for l, _ in present]
                continue
            target = self.global_loc if mode == "global" else self.pad.device_location
            cur = self.present[target.key].get(s.name)
            if on and cur != val:
                ops.append(("set", target, s.name, val))
            elif not on and cur is not None:
                ops.append(("delete", target, s.name, None))
            else:
                continue
            shadow = [l for l, _ in present if locs.index(l) < locs.index(target)]
            if shadow:
                warnings.append(f"{s.name} is also set in {shadow[0].label}, which takes precedence, "
                                "so this change won't take effect.")
        if any(self.rows[n].on.get() for n in ("CurtainLeft", "CurtainRight", "CurtainTop")) and \
                not all(self.rows[n].on.get() for n in ("CurtainLeft", "CurtainRight", "CurtainTop")):
            warnings.append("Curtains need CurtainLeft, CurtainRight and CurtainTop all set, or none of them apply.")
        return ops, warnings

    def apply(self):
        try:
            ops, warnings = self.plan()
        except ValueError as e:
            messagebox.showerror("Invalid value", str(e), parent=self.root)
            return
        if not ops:
            messagebox.showinfo("Nothing to apply", "The registry already matches these settings.", parent=self.root)
            return
        if any(loc.hive == HKLM for _, loc, _, _ in ops) and not self.is_admin:
            if messagebox.askyesno("Administrator rights needed",
                                   "Writing to HKEY_LOCAL_MACHINE needs administrator rights.\n\n"
                                   "Restart this tool as administrator? Unsaved edits will be lost.",
                                   parent=self.root):
                self.relaunch_admin()
            return
        lines = []
        for action, loc, name, val in ops:
            if action == "set":
                lines.append(f"SET     {loc.reg_path}\\{name} = {val}  (dword:{val:08x})")
            else:
                lines.append(f"DELETE  {loc.reg_path}\\{name}")
        text = "\n".join(lines)
        if warnings:
            text += "\n\nWarnings:\n• " + "\n• ".join(warnings)
        text += "\n\nThe affected keys are exported to the backups folder first."
        if not messagebox.askokcancel("Apply changes", text, parent=self.root):
            return
        try:
            saved = self.backup()
        except OSError as e:
            messagebox.showerror("Backup failed", f"{e}\n\nNothing was changed.", parent=self.root)
            return
        errors = []
        for action, loc, name, val in ops:
            try:
                loc.write(name, val) if action == "set" else loc.delete(name)
            except OSError as e:
                errors.append(f"{name} in {loc.label}: {e.strerror or e}")
        self.load()
        if errors:
            messagebox.showerror("Some writes failed", "\n".join(errors), parent=self.root)
            return
        self.status.config(text=f"Applied {len(ops)} change(s). Backup: {', '.join(os.path.basename(p) for p in saved)}")
        if messagebox.askyesno("Applied",
                               "Saved. Windows reads these values when the touchpad starts.\n\n"
                               "Restart the touchpad now? It stops responding for a second or two. "
                               "Otherwise reboot later.", parent=self.root):
            self.restart_touchpad()

    def backup(self):
        """Export every key that holds values to backups\\<timestamp>-<key>.reg."""
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        saved = []
        for loc in self.locations():
            if not loc.exists():
                continue
            path = os.path.join(BACKUP_DIR, f"{ts}-{loc.key}.reg")
            r = subprocess.run(["reg", "export", loc.reg_path, path, "/y"], capture_output=True,
                               text=True, errors="replace", creationflags=NO_WINDOW)
            if r.returncode != 0:
                raise OSError(f"reg export {loc.reg_path} failed: {(r.stderr or r.stdout).strip()}")
            saved.append(path)
        return saved

    def restore(self):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        path = filedialog.askopenfilename(parent=self.root, initialdir=BACKUP_DIR, title="Restore registry backup",
                                          filetypes=[("Registry files", "*.reg"), ("All files", "*.*")])
        if not path:
            return
        if not self.is_admin:
            if messagebox.askyesno("Administrator rights needed",
                                   "Importing HKLM keys needs administrator rights. Restart as administrator?",
                                   parent=self.root):
                self.relaunch_admin()
            return
        if not messagebox.askokcancel("Restore backup",
                                      f"Import {os.path.basename(path)}?\n\n"
                                      "This restores the values in the file. Values added since the backup "
                                      "are not removed. Use 'Windows defaults' + Apply for that.",
                                      parent=self.root):
            return
        r = subprocess.run(["reg", "import", path], capture_output=True, text=True, errors="replace",
                           creationflags=NO_WINDOW)
        self.load()
        if r.returncode != 0:
            messagebox.showerror("Restore failed", (r.stderr or r.stdout).strip(), parent=self.root)
        else:
            self.status.config(text=f"Restored {os.path.basename(path)}. Restart the touchpad to use it.")

    def restart_touchpad(self):
        if not self.pad:
            return
        if not self.is_admin:
            messagebox.showinfo("Administrator rights needed",
                                "Restarting the device needs administrator rights. Use 'Restart as admin', "
                                "or reboot / disable + enable the touchpad in Device Manager.", parent=self.root)
            return
        self.status.config(text="Restarting touchpad…")
        self.root.update_idletasks()
        r = subprocess.run(["pnputil", "/restart-device", self.pad.instance_id], capture_output=True,
                           text=True, errors="replace", creationflags=NO_WINDOW)
        out = (r.stdout or r.stderr).strip()
        if r.returncode == 0 and "fail" not in out.lower():
            self.status.config(text="Touchpad restarted. New zones are active.")
        else:
            messagebox.showwarning("Restart touchpad", (out or "pnputil failed.") +
                                   "\n\nReboot instead for the new values to load.", parent=self.root)
            self.status.config(text="Touchpad restart didn't complete. Reboot to load the new values.")

    def relaunch_admin(self):
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
        params = subprocess.list2cmdline([os.path.abspath(__file__)] + sys.argv[1:])
        if ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, APP_DIR, 1) > 32:
            self.root.destroy()


# ---------------------------------------------------------------- entry points

def probe():
    layouts = {l.instance_id: l for l in list_touchpad_layouts()}
    pads = find_touchpads()
    print(f"Precision touchpads in registry: {len(pads)}")
    for p in pads:
        lay = layouts.get(p.instance_id.upper())
        print(f"  {p.name}\n    {p.instance_id}\n    connected: {bool(lay)}")
        if lay:
            size = "unknown" if not lay.size_mm else f"{lay.size_mm[0]:.1f} x {lay.size_mm[1]:.1f} mm"
            print(f"    size: {size}  X{lay.x_range} Y{lay.y_range}  finger collections: {lay.fingers}"
                  f"  contact-count collection: {lay.count_lc}")
        vals, err = p.device_location.read([s.name for s in SETTINGS])
        print(f"    device key values: {vals or '-'}" + (f"  (error: {err})" if err else ""))
    for loc in (Location("global", "HKLM", HKLM, PTP_PATH), Location("user", "HKCU", HKCU, PTP_PATH)):
        vals, err = loc.read([s.name for s in SETTINGS] + list(LEGACY_DEFAULTS) + ["AAPThreshold", "AAPDisabled"])
        print(f"{loc.reg_path}: {vals or '-'}" + (f"  (error: {err})" if err else ""))


def main():
    if "--probe" in sys.argv:
        probe()
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()
    root = tk.Tk()
    root.title("Precision Touchpad Zones")
    App(root)
    root.minsize(int(root.winfo_fpixels("1i") / 96 * 1000), int(root.winfo_fpixels("1i") / 96 * 600))
    if "--smoke" in sys.argv:
        root.after(2500, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
