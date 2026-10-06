"""
Smart Shutdown Pro (SST Pro)
Windows 10 / 11 power-management utility.

Run   : python src/main.py
Build : build.bat
"""
from __future__ import annotations

import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import customtkinter as ctk
import psutil
import pystray
from PIL import Image, ImageDraw

try:
    from plyer import notification as plyer_notification
except Exception:  # notifications are a nice-to-have, never fatal
    plyer_notification = None

try:
    import winsound
except ImportError:  # non-Windows dev machine
    winsound = None


# --------------------------------------------------------------------------- #
#  Constants
# --------------------------------------------------------------------------- #
APP_NAME = "Smart Shutdown Pro"
APP_TITLE = "SMART SHUTDOWN PRO"

C = {
    "bg": "#0B0F17",
    "card": "#141B27",
    "pod": "#1E293B",
    "pod_hover": "#2A3A52",
    "accent": "#00A8CC",
    "accent_hover": "#12BCE0",
    "text": "#E6EDF7",
    "muted": "#8B9BB4",
    "dim": "#4A5A73",
    "danger": "#EF4444",
    "danger_hover": "#DC2626",
    "ok": "#34D399",
}

FINAL_WARNING_SECS = 60
BATTERY_LIMIT_PCT = 20
CPU_IDLE_PCT = 5
CPU_IDLE_SECS = 180
NET_IDLE_BYTES = 5 * 1024  # 5 KB/s
NET_IDLE_SECS = 120
MAX_MINUTES = 5999

# label -> (command, verb used in sentences)
ACTIONS = {
    "Shutdown": ("shutdown /s /f /t 0", "shut down"),
    "Sleep (Suspend)": ("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", "go to sleep"),
    "Hibernate": ("shutdown /h", "hibernate"),
    "Lock Screen": ("rundll32.exe user32.dll,LockWorkStation", "lock"),
}

TAB_MIN = "⏱️ Minute Timer"
TAB_CLOCK = "🕒 Clock Scheduler"
TAB_SMART = "🔋 Smart Automation"

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def resource_path(relative: str) -> str:
    """Works from source (src/main.py -> project root) and from PyInstaller."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


ICON_ICO = resource_path("assets/app_icon.ico")
ICON_PNG = resource_path("assets/app_icon.png")
THEME_PATH = resource_path("assets/cyber_glow.json")


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #
def fire(command: str) -> None:
    """Start a command without waiting for it (sleep/lock may not return)."""
    try:
        subprocess.Popen(
            command,
            creationflags=NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def run_wait(command: str, timeout: int = 10) -> None:
    try:
        subprocess.run(
            command,
            creationflags=NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def beep() -> None:
    if winsound is not None:
        try:
            winsound.MessageBeep()
        except RuntimeError:
            pass


def fmt_clock(seconds: float) -> str:
    s = max(int(math.ceil(seconds)), 0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_ms(seconds: float) -> str:
    s = max(int(seconds), 0)
    return f"{s // 60}:{s % 60:02d}"


def window_scale(win) -> float:
    try:
        return float(win._get_window_scaling())
    except Exception:
        return 1.0


def center_on_screen(win, w: int, h: int) -> None:
    """w/h are logical sizes; x/y are real pixels (customtkinter convention)."""
    scale = window_scale(win)
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    x = max(int((sw - w * scale) / 2), 0)
    y = max(int((sh - h * scale) / 2) - 20, 0)
    win.geometry(f"{w}x{h}+{x}+{y}")


def center_existing(win) -> None:
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    x = max((sw - win.winfo_width()) // 2, 0)
    y = max((sh - win.winfo_height()) // 2 - 20, 0)
    win.geometry(f"+{x}+{y}")


_mutex_handle = None


def acquire_single_instance() -> bool:
    global _mutex_handle
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    _mutex_handle = kernel32.CreateMutexW(None, False, "Local\\SmartShutdownPro_SingleInstance")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


# --------------------------------------------------------------------------- #
#  Engine: every loop lives in a daemon thread, the GUI only reads state
# --------------------------------------------------------------------------- #
class Engine:
    IDLE, TIMER, MONITOR, FINAL = "idle", "timer", "monitor", "final"

    def __init__(self, events: "queue.Queue"):
        self.events = events
        self.mode = self.IDLE
        self.remaining = 0.0
        self.total = 0.0
        self.status = "No action scheduled"
        self.action_label = "Shutdown"
        self.reason = ""
        self._stop = threading.Event()

    @property
    def active(self) -> bool:
        return self.mode != self.IDLE

    # -- public API --------------------------------------------------------- #
    def start_timer(self, target_ts: float, label: str, status: str) -> None:
        token = self._new_session(label)
        self.total = max(target_ts - time.time(), 1.0)
        self.remaining = self.total
        self.status = status
        self.mode = self.TIMER
        threading.Thread(target=self._countdown, args=(token, target_ts, label), daemon=True).start()

    def start_monitors(self, cfg: dict, label: str) -> None:
        token = self._new_session(label)
        self.status = "Monitoring: " + ", ".join(cfg["names"])
        self.mode = self.MONITOR
        threading.Thread(target=self._monitor_worker, args=(token, cfg), daemon=True).start()

    def cancel(self, abort_windows: bool = True, wait: bool = False) -> None:
        self._stop.set()
        self._set_idle()
        if abort_windows:
            t = threading.Thread(target=run_wait, args=("shutdown /a",), daemon=True)
            t.start()
            if wait:
                t.join(3)

    # -- internals ---------------------------------------------------------- #
    def _new_session(self, label: str) -> threading.Event:
        self._stop.set()
        token = threading.Event()
        self._stop = token
        self.action_label = label
        self.reason = ""
        return token

    def _set_idle(self) -> None:
        self.mode = self.IDLE
        self.remaining = 0.0
        self.total = 0.0
        self.status = "No action scheduled"

    def _countdown(self, token: threading.Event, target: float, label: str) -> None:
        warned = False
        while not token.is_set():
            rem = target - time.time()
            self.remaining = max(rem, 0.0)
            if rem <= FINAL_WARNING_SECS and not warned:
                warned = True
                self.mode = self.FINAL
                self.events.put(("warn",))
            if rem <= 0:
                break
            token.wait(0.1 if rem <= FINAL_WARNING_SECS + 5 else 0.25)
        if token.is_set():
            return
        token.set()
        self.events.put(("executing", label))
        fire(ACTIONS[label][0])
        self._set_idle()
        self.events.put(("finished", label))

    def _final(self, token: threading.Event, label: str, reason: str) -> None:
        """A smart trigger fired: give the user the 60-second grace window."""
        if token.is_set():
            return
        self.action_label = label
        self.reason = reason
        self.total = float(FINAL_WARNING_SECS)
        self.status = f"{reason} — final warning"
        self.events.put(("notify", "Power action triggered", f"{reason}. {label} in {FINAL_WARNING_SECS}s."))
        self._countdown(token, time.time() + FINAL_WARNING_SECS, label)

    @staticmethod
    def _rx_bytes() -> int:
        counters = psutil.net_io_counters()
        return counters.bytes_recv if counters else 0

    def _monitor_worker(self, token: threading.Event, cfg: dict) -> None:
        cpu_since = None
        net_since = None
        psutil.cpu_percent(interval=None)  # prime the counter
        last_rx = self._rx_bytes()
        last_t = time.monotonic()

        while not token.wait(1.0):
            now = time.monotonic()
            parts = []

            if cfg["battery"]:
                batt = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
                if batt is not None:
                    parts.append(f"Battery {batt.percent:.0f}%" + ("" if batt.power_plugged else " (unplugged)"))
                    if not batt.power_plugged and batt.percent < BATTERY_LIMIT_PCT:
                        self._final(token, "Hibernate", f"Battery below {BATTERY_LIMIT_PCT}% while unplugged")
                        return

            if cfg["cpu"]:
                cpu = psutil.cpu_percent(interval=None)
                if cpu < CPU_IDLE_PCT:
                    cpu_since = cpu_since or now
                    held = now - cpu_since
                    if held >= CPU_IDLE_SECS:
                        self._final(token, cfg["action"], "CPU idle for 3 minutes")
                        return
                    parts.append(f"CPU idle {fmt_ms(held)}/3:00")
                else:
                    cpu_since = None
                    parts.append(f"CPU {cpu:.0f}%")

            if cfg["net"]:
                rx = self._rx_bytes()
                speed = (rx - last_rx) / max(now - last_t, 1e-6)
                last_rx, last_t = rx, now
                if speed < NET_IDLE_BYTES:
                    net_since = net_since or now
                    held = now - net_since
                    if held >= NET_IDLE_SECS:
                        self._final(token, cfg["action"], "Network idle for 2 minutes")
                        return
                    parts.append(f"Net idle {fmt_ms(held)}/2:00")
                else:
                    net_since = None
                    parts.append(f"Net {speed / 1024:.0f} KB/s")

            if not token.is_set() and self.mode == self.MONITOR:
                self.status = "Monitoring · " + " · ".join(parts) if parts else self.status


# --------------------------------------------------------------------------- #
#  Final 60-second warning (modal, always on top)
# --------------------------------------------------------------------------- #
class WarningModal(ctk.CTkToplevel):
    def __init__(self, app: "App"):
        super().__init__(app, fg_color=C["bg"])
        self.app = app
        self.engine = app.engine
        self._job = None
        self._last_sec = None

        self.title("Final warning")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # only ABORT closes it
        center_on_screen(self, 460, 430)
        self.attributes("-topmost", True)
        app.apply_icon(self)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        card = ctk.CTkFrame(self, fg_color=C["card"], corner_radius=12)
        card.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)
        card.grid_columnconfigure(0, weight=1)

        verb = ACTIONS.get(self.engine.action_label, ("", "shut down"))[1]
        ctk.CTkLabel(card, text="⚠  FINAL WARNING", font=app.f_caption_bold, text_color=C["danger"]).grid(
            row=0, column=0, pady=(20, 4)
        )
        ctk.CTkLabel(
            card, text=f"Your computer will {verb} in", font=app.f_h2, text_color=C["text"]
        ).grid(row=1, column=0, pady=(0, 0))
        self.count = ctk.CTkLabel(
            card,
            text=str(FINAL_WARNING_SECS),
            font=ctk.CTkFont(family="Consolas", size=104, weight="bold"),
            text_color=C["danger"],
        )
        self.count.grid(row=2, column=0)
        ctk.CTkLabel(card, text="seconds", font=app.f_body, text_color=C["muted"]).grid(row=3, column=0)
        ctk.CTkLabel(
            card,
            text=self.engine.reason or "Scheduled timer reached",
            font=app.f_small,
            text_color=C["muted"],
            wraplength=360,
            justify="center",
        ).grid(row=4, column=0, pady=(8, 10), padx=16)

        self.bar = ctk.CTkProgressBar(
            card, height=8, corner_radius=8, fg_color=C["pod"], progress_color=C["danger"]
        )
        self.bar.set(1)
        self.bar.grid(row=5, column=0, sticky="ew", padx=28, pady=(0, 16))

        ctk.CTkButton(
            card,
            text="ABORT POWER ACTION 🛑",
            height=54,
            corner_radius=12,
            font=app.f_btn,
            fg_color=C["danger"],
            hover_color=C["danger_hover"],
            text_color="#FFFFFF",
            command=app.abort,
        ).grid(row=6, column=0, sticky="ew", padx=28, pady=(0, 24))

        self.after(150, self._grab)
        self._tick()

    def _grab(self) -> None:
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

    def _tick(self) -> None:
        if self.engine.mode != Engine.FINAL:
            self.close()
            return
        rem = self.engine.remaining
        sec = max(int(math.ceil(rem)), 0)
        if sec != self._last_sec:
            self._last_sec = sec
            self.count.configure(text=str(sec))
            beep()  # once per second
        self.bar.set(min(max(rem / FINAL_WARNING_SECS, 0.0), 1.0))
        self._job = self.after(100, self._tick)

    def close(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Main window
# --------------------------------------------------------------------------- #
class App(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        if os.path.exists(THEME_PATH):
            ctk.set_default_color_theme(THEME_PATH)
        super().__init__(fg_color=C["bg"])

        self.events: "queue.Queue" = queue.Queue()
        self.engine = Engine(self.events)
        self.tray = None
        self._modal = None
        self._hidden = False
        self._tray_hint_shown = False
        self._flash_job = None
        self._resize_job = None
        self._cache = {}
        self._lockables = []
        self._wrap_labels = []

        fam = "Segoe UI"
        self.f_title = ctk.CTkFont(family=fam, size=20, weight="bold")
        self.f_h2 = ctk.CTkFont(family=fam, size=18, weight="bold")
        self.f_body = ctk.CTkFont(family=fam, size=14)
        self.f_body_bold = ctk.CTkFont(family=fam, size=14, weight="bold")
        self.f_small = ctk.CTkFont(family=fam, size=12)
        self.f_tab = ctk.CTkFont(family=fam, size=13, weight="bold")
        self.f_caption_bold = ctk.CTkFont(family=fam, size=12, weight="bold")
        self.f_btn = ctk.CTkFont(family=fam, size=15, weight="bold")
        self.f_clock = ctk.CTkFont(family="Consolas", size=72, weight="bold")
        self.f_input = ctk.CTkFont(family=fam, size=22, weight="bold")

        self.title(APP_NAME)
        scale = window_scale(self)
        h = int(min(840, self.winfo_screenheight() / scale - 100))
        center_on_screen(self, 540, max(h, 640))
        self.minsize(480, 640)
        self.apply_icon(self)
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._build_header()
        self._build_clock()
        self._build_action()
        self._build_tabs()
        self._build_controls()

        self.bind("<Configure>", self._on_configure)
        self.after(120, self._apply_responsive)
        self.after(100, self._poll)

    # ---- window plumbing -------------------------------------------------- #
    def apply_icon(self, win) -> None:
        if not os.path.exists(ICON_ICO):
            return

        def _set():
            try:
                win.iconbitmap(ICON_ICO)
            except Exception:
                pass

        _set()
        win.after(250, _set)  # customtkinter overrides the icon shortly after creation

    def notify(self, title: str, message: str) -> None:
        if plyer_notification is None:
            return

        def _go():
            try:
                plyer_notification.notify(
                    title=title,
                    message=message,
                    app_name=APP_NAME,
                    app_icon=ICON_ICO if os.path.exists(ICON_ICO) else "",
                    timeout=5,
                )
            except Exception:
                pass

        threading.Thread(target=_go, daemon=True).start()

    # ---- UI construction -------------------------------------------------- #
    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 8))
        hdr.grid_columnconfigure(1, weight=1)

        if os.path.exists(ICON_PNG):
            try:
                img = Image.open(ICON_PNG).convert("RGBA")
                self.logo = ctk.CTkImage(light_image=img, dark_image=img, size=(44, 44))
                ctk.CTkLabel(hdr, image=self.logo, text="").grid(row=0, column=0, rowspan=2, padx=(0, 12))
            except Exception:
                pass

        ctk.CTkLabel(hdr, text=APP_TITLE, font=self.f_title, text_color=C["text"], anchor="w").grid(
            row=0, column=1, sticky="w"
        )
        ctk.CTkLabel(
            hdr, text="Smart power management for Windows", font=self.f_small, text_color=C["muted"], anchor="w"
        ).grid(row=1, column=1, sticky="w")

        self.pill = ctk.CTkLabel(
            hdr,
            text="IDLE",
            font=self.f_caption_bold,
            text_color=C["muted"],
            fg_color=C["card"],
            corner_radius=10,
            width=112,
            height=28,
        )
        self.pill.grid(row=0, column=2, rowspan=2, padx=(12, 0))

    def _build_clock(self) -> None:
        card = ctk.CTkFrame(self, fg_color=C["card"], corner_radius=12)
        card.grid(row=1, column=0, sticky="ew", padx=20, pady=6)
        card.grid_columnconfigure(0, weight=1)
        self.clock_card = card

        ctk.CTkLabel(card, text="TIME REMAINING", font=self.f_caption_bold, text_color=C["muted"]).grid(
            row=0, column=0, pady=(14, 0)
        )
        self.clock = ctk.CTkLabel(card, text="00:00:00", font=self.f_clock, text_color=C["dim"])
        self.clock.grid(row=1, column=0)
        self.bar = ctk.CTkProgressBar(card, height=6, corner_radius=8, fg_color=C["pod"], progress_color=C["accent"])
        self.bar.set(0)
        self.bar.grid(row=2, column=0, sticky="ew", padx=24, pady=(2, 8))
        self.status = ctk.CTkLabel(
            card, text="No action scheduled", font=self.f_body, text_color=C["muted"], wraplength=420, justify="center"
        )
        self.status.grid(row=3, column=0, padx=16, pady=(0, 14))

    def _build_action(self) -> None:
        card = ctk.CTkFrame(self, fg_color=C["card"], corner_radius=12)
        card.grid(row=2, column=0, sticky="ew", padx=20, pady=6)
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="ACTION", font=self.f_caption_bold, text_color=C["muted"]).grid(
            row=0, column=0, padx=(18, 12), pady=14
        )
        self.action_var = ctk.StringVar(value="Shutdown")
        self.action_menu = ctk.CTkOptionMenu(
            card,
            values=list(ACTIONS),
            variable=self.action_var,
            height=38,
            corner_radius=10,
            font=self.f_body,
            dropdown_font=self.f_body,
            fg_color=C["pod"],
            button_color=C["accent"],
            button_hover_color=C["accent_hover"],
            text_color=C["text"],
            dropdown_fg_color=C["pod"],
            dropdown_hover_color=C["pod_hover"],
            dropdown_text_color=C["text"],
        )
        self.action_menu.grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=12)
        self._lockables.append(self.action_menu)

    def _glow(self, entry: ctk.CTkEntry) -> None:
        entry.bind("<FocusIn>", lambda _e: entry.configure(border_color=C["accent"]), add="+")
        entry.bind("<FocusOut>", lambda _e: entry.configure(border_color=C["pod"]), add="+")

    def _build_tabs(self) -> None:
        self.tabs = ctk.CTkTabview(
            self,
            corner_radius=12,
            border_width=0,
            fg_color=C["card"],
            segmented_button_fg_color=C["bg"],
            segmented_button_selected_color=C["pod"],
            segmented_button_selected_hover_color=C["pod_hover"],
            segmented_button_unselected_color=C["bg"],
            segmented_button_unselected_hover_color="#18212F",
            text_color=C["text"],
        )
        self.tabs.grid(row=3, column=0, sticky="nsew", padx=20, pady=6)
        t_min = self.tabs.add(TAB_MIN)
        t_clock = self.tabs.add(TAB_CLOCK)
        t_smart = self.tabs.add(TAB_SMART)
        try:
            self.tabs._segmented_button.configure(font=self.f_tab, height=34, corner_radius=10)
        except Exception:
            pass

        # -- Tab 1: minute timer ------------------------------------------- #
        t_min.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(t_min, text="Minutes until the action runs", font=self.f_body, text_color=C["muted"]).grid(
            row=0, column=0, pady=(8, 6)
        )
        self.minutes_entry = ctk.CTkEntry(
            t_min,
            width=190,
            height=48,
            corner_radius=10,
            border_color=C["pod"],
            fg_color=C["bg"],
            justify="center",
            font=self.f_input,
            placeholder_text="30",
        )
        self.minutes_entry.grid(row=1, column=0)
        self._glow(self.minutes_entry)
        self.minutes_entry.bind("<Return>", self.on_start)
        self._lockables.append(self.minutes_entry)

        presets = ctk.CTkFrame(t_min, fg_color="transparent")
        presets.grid(row=2, column=0, sticky="ew", padx=10, pady=(12, 4))
        for i, mins in enumerate((30, 45, 60)):
            presets.grid_columnconfigure(i, weight=1)
            btn = ctk.CTkButton(
                presets,
                text=f"{mins} Min",
                height=38,
                corner_radius=10,
                font=self.f_body_bold,
                fg_color=C["pod"],
                hover_color=C["pod_hover"],
                text_color=C["text"],
                text_color_disabled=C["dim"],
                command=lambda v=mins: self._set_minutes(v),
            )
            btn.grid(row=0, column=i, sticky="ew", padx=4)
            self._lockables.append(btn)

        # -- Tab 2: clock scheduler ---------------------------------------- #
        t_clock.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(t_clock, text="Run at (24-hour clock)", font=self.f_body, text_color=C["muted"]).grid(
            row=0, column=0, pady=(8, 6)
        )
        self.clock_entry = ctk.CTkEntry(
            t_clock,
            width=190,
            height=48,
            corner_radius=10,
            border_color=C["pod"],
            fg_color=C["bg"],
            justify="center",
            font=self.f_input,
            placeholder_text="23:45",
        )
        self.clock_entry.grid(row=1, column=0)
        self._glow(self.clock_entry)
        self.clock_entry.bind("<Return>", self.on_start)
        self.clock_entry.bind("<KeyRelease>", self._update_clock_hint)
        self._lockables.append(self.clock_entry)
        self.clock_hint = ctk.CTkLabel(
            t_clock, text="If the time already passed today, it runs tomorrow.", font=self.f_small,
            text_color=C["muted"], wraplength=400, justify="center",
        )
        self.clock_hint.grid(row=2, column=0, pady=(12, 0), padx=10)
        self._wrap_labels.append((self.clock_hint, 90))

        # -- Tab 3: smart automation --------------------------------------- #
        t_smart.grid_columnconfigure(0, weight=1)
        t_smart.grid_rowconfigure(0, weight=1)
        scroll = ctk.CTkScrollableFrame(
            t_smart,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=C["pod"],
            scrollbar_button_hover_color=C["pod_hover"],
        )
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        self.sw_batt = self._smart_row(
            scroll, 0, "Battery protection",
            f"Hibernates automatically if the battery drops below {BATTERY_LIMIT_PCT}% while unplugged.",
        )
        self.sw_cpu = self._smart_row(
            scroll, 1, "CPU idle monitor",
            f"Runs the action when CPU stays under {CPU_IDLE_PCT}% for 3 minutes (render finished).",
        )
        self.sw_net = self._smart_row(
            scroll, 2, "Download complete",
            "Runs the action when download speed stays under 5 KB/s for 2 minutes.",
        )
        has_battery = False
        try:
            has_battery = psutil.sensors_battery() is not None
        except Exception:
            pass
        if not has_battery:
            self.sw_batt.configure(state="disabled")
        else:
            self._lockables.append(self.sw_batt)
        self._lockables += [self.sw_cpu, self.sw_net]
        if not has_battery:
            self.batt_desc.configure(text="No battery detected on this PC.")

    def _smart_row(self, parent, row: int, title: str, desc: str) -> ctk.CTkSwitch:
        pod = ctk.CTkFrame(parent, fg_color=C["pod"], corner_radius=10)
        pod.grid(row=row, column=0, sticky="ew", padx=2, pady=4)
        pod.grid_columnconfigure(0, weight=1)
        sw = ctk.CTkSwitch(
            pod,
            text=title,
            font=self.f_body_bold,
            switch_width=44,
            switch_height=22,
            fg_color="#2B3A52",
            progress_color=C["accent"],
            button_color=C["text"],
            button_hover_color="#FFFFFF",
            text_color=C["text"],
            text_color_disabled=C["dim"],
        )
        sw.grid(row=0, column=0, sticky="w", padx=14, pady=(12, 2))
        lbl = ctk.CTkLabel(
            pod, text=desc, font=self.f_small, text_color=C["muted"], anchor="w", justify="left", wraplength=340
        )
        lbl.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 12))
        self._wrap_labels.append((lbl, 150))
        if row == 0:
            self.batt_desc = lbl
        return sw

    def _build_controls(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", padx=20, pady=(8, 2))
        bar.grid_columnconfigure(0, weight=2)
        bar.grid_columnconfigure(1, weight=1)

        self.start_btn = ctk.CTkButton(
            bar,
            text="▶  START",
            height=52,
            corner_radius=12,
            font=self.f_btn,
            fg_color=C["accent"],
            hover_color=C["accent_hover"],
            text_color="#04121A",
            text_color_disabled=C["muted"],
            command=self.on_start,
        )
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.cancel_btn = ctk.CTkButton(
            bar,
            text="CANCEL",
            height=52,
            corner_radius=12,
            font=self.f_btn,
            fg_color="transparent",
            border_width=2,
            border_color=C["pod"],
            hover_color=C["pod"],
            text_color=C["text"],
            text_color_disabled=C["dim"],
            state="disabled",
            command=self.abort,
        )
        self.cancel_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        self.feedback = ctk.CTkLabel(
            self, text=self._default_feedback(), font=self.f_small, text_color=C["muted"], wraplength=440
        )
        self.feedback.grid(row=5, column=0, padx=20, pady=(4, 14))
        self._wrap_labels.append((self.feedback, 60))
        self._wrap_labels.append((self.status, 90))

    # ---- actions ---------------------------------------------------------- #
    @staticmethod
    def _default_feedback() -> str:
        return "Closing the window keeps SST Pro running in the system tray."

    def _flash(self, msg: str, error: bool = False) -> None:
        self.feedback.configure(text=msg, text_color=C["danger"] if error else C["ok"])
        if self._flash_job is not None:
            self.after_cancel(self._flash_job)
        self._flash_job = self.after(5000, self._reset_feedback)

    def _reset_feedback(self) -> None:
        self._flash_job = None
        self.feedback.configure(text=self._default_feedback(), text_color=C["muted"])

    def _set_minutes(self, value: int) -> None:
        self.minutes_entry.delete(0, "end")
        self.minutes_entry.insert(0, str(value))

    def _read_minutes(self) -> int:
        raw = self.minutes_entry.get().strip()
        if not raw.isdigit():
            raise ValueError("Enter whole minutes, for example 30.")
        value = int(raw)
        if not 1 <= value <= MAX_MINUTES:
            raise ValueError(f"Minutes must be between 1 and {MAX_MINUTES}.")
        return value

    def _read_clock(self) -> datetime:
        raw = self.clock_entry.get().strip()
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw)
        if not m:
            raise ValueError("Use 24-hour HH:MM, for example 23:45.")
        now = datetime.now()
        target = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if target <= now:  # already passed today -> tomorrow
            target += timedelta(days=1)
        return target

    def _update_clock_hint(self, _event=None) -> None:
        if not self.clock_entry.get().strip():
            self.clock_hint.configure(text="If the time already passed today, it runs tomorrow.", text_color=C["muted"])
            return
        try:
            t = self._read_clock()
        except ValueError:
            self.clock_hint.configure(text="Type the time as HH:MM, for example 23:45.", text_color=C["muted"])
            return
        day = "today" if t.date() == datetime.now().date() else "tomorrow"
        mins = int((t - datetime.now()).total_seconds() // 60)
        self.clock_hint.configure(
            text=f"Will run {day} at {t:%H:%M} (in {mins // 60}h {mins % 60:02d}m)", text_color=C["accent"]
        )

    def _read_monitors(self, label: str) -> dict:
        cfg = {
            "battery": bool(self.sw_batt.get()),
            "cpu": bool(self.sw_cpu.get()),
            "net": bool(self.sw_net.get()),
            "action": label,
            "names": [],
        }
        if cfg["battery"]:
            cfg["names"].append("battery")
        if cfg["cpu"]:
            cfg["names"].append("CPU idle")
        if cfg["net"]:
            cfg["names"].append("network idle")
        if not cfg["names"]:
            raise ValueError("Turn on at least one automation first.")
        return cfg

    def on_start(self, _event=None) -> None:
        if self.engine.active:
            return
        label = self.action_var.get()
        tab = self.tabs.get()
        try:
            if tab == TAB_MIN:
                minutes = self._read_minutes()
                target = time.time() + minutes * 60
                status = f"{label} in {minutes} min · at {datetime.fromtimestamp(target):%H:%M:%S}"
                self.engine.start_timer(target, label, status)
                note = ("Timer started", f"{label} in {minutes} minute(s).")
            elif tab == TAB_CLOCK:
                target_dt = self._read_clock()
                day = "today" if target_dt.date() == datetime.now().date() else "tomorrow"
                status = f"{label} {day} at {target_dt:%H:%M}"
                self.engine.start_timer(target_dt.timestamp(), label, status)
                note = ("Schedule set", status + ".")
            else:
                cfg = self._read_monitors(label)
                self.engine.start_monitors(cfg, label)
                note = ("Smart automation armed", "Watching: " + ", ".join(cfg["names"]) + ".")
        except ValueError as exc:
            self._flash(str(exc), error=True)
            return
        self._set_locked(True)
        self._flash(note[1])
        self.notify(*note)

    def abort(self) -> None:
        was_active = self.engine.active
        self.engine.cancel(abort_windows=True)
        self._set_locked(False)
        if self._modal is not None:
            self._modal.close()
            self._modal = None
        self._flash("Cancelled. Nothing is scheduled.")
        if was_active:
            self.notify("Power action aborted", "All countdowns were cancelled.")

    def _set_locked(self, locked: bool) -> None:
        state = "disabled" if locked else "normal"
        for w in self._lockables:
            try:
                w.configure(state=state)
            except Exception:
                pass
        self.start_btn.configure(state=state, fg_color=C["pod"] if locked else C["accent"])
        self.cancel_btn.configure(state="normal" if locked else "disabled")

    # ---- event loop (GUI thread only) ------------------------------------- #
    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self._refresh()
        self.after(100, self._poll)

    def _handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "warn":
            self._show_warning()
        elif kind == "notify":
            self.notify(event[1], event[2])
        elif kind == "finished":
            self._set_locked(False)
            self._flash(f"Done: {event[1]} executed.")
        elif kind == "show":
            self.restore_window()
        elif kind == "exit":
            self._quit()

    def _refresh(self) -> None:
        eng = self.engine
        mode = eng.mode
        if mode in (Engine.TIMER, Engine.FINAL):
            rem = eng.remaining
            frac = rem / eng.total if eng.total else 0.0
        else:
            rem, frac = 0.0, 0.0

        text = fmt_clock(rem)
        clock_color = {Engine.IDLE: C["dim"], Engine.MONITOR: C["dim"], Engine.TIMER: C["accent"], Engine.FINAL: C["danger"]}[mode]
        pill_text, pill_color = {
            Engine.IDLE: ("IDLE", C["muted"]),
            Engine.TIMER: ("ARMED", C["accent"]),
            Engine.MONITOR: ("MONITORING", C["accent"]),
            Engine.FINAL: ("FINAL", C["danger"]),
        }[mode]

        def update(key, value, apply):
            if self._cache.get(key) != value:
                self._cache[key] = value
                apply(value)

        update("clock", text, lambda v: self.clock.configure(text=v))
        update("clock_c", clock_color, lambda v: self.clock.configure(text_color=v))
        update("pill", (pill_text, pill_color), lambda v: self.pill.configure(text=v[0], text_color=v[1]))
        update("status", eng.status, lambda v: self.status.configure(text=v))
        update("bar_c", C["danger"] if mode == Engine.FINAL else C["accent"], lambda v: self.bar.configure(progress_color=v))
        self.bar.set(min(max(frac, 0.0), 1.0))

        if self.tray is not None:
            tip = f"{APP_NAME} · {text}" if mode in (Engine.TIMER, Engine.FINAL) else (
                f"{APP_NAME} · monitoring" if mode == Engine.MONITOR else APP_NAME
            )
            update("tray", tip, lambda v: setattr(self.tray, "title", v))

    # ---- responsive layout ------------------------------------------------- #
    def _on_configure(self, event) -> None:
        if event.widget is not self:
            return
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(60, self._apply_responsive)

    def _apply_responsive(self) -> None:
        self._resize_job = None
        scale = window_scale(self)
        uw = self.winfo_width() / scale
        uh = self.winfo_height() / scale
        size = int(max(40, min(104, uw / 5.9, uh / 11.0)))
        if self.f_clock.cget("size") != size:
            self.f_clock.configure(size=size)
        for label, margin in self._wrap_labels:
            try:
                label.configure(wraplength=max(int(uw - margin - 40), 200))
            except Exception:
                pass

    # ---- warning modal ----------------------------------------------------- #
    def _show_warning(self) -> None:
        self.restore_window(topmost_pulse=True)
        if self._modal is None or not self._modal.winfo_exists():
            self._modal = WarningModal(self)

    # ---- tray -------------------------------------------------------------- #
    def _tray_image(self) -> Image.Image:
        try:
            return Image.open(ICON_PNG).convert("RGBA").resize((64, 64), Image.LANCZOS)
        except Exception:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((6, 6, 58, 58), fill=C["accent"])
            d.rectangle((29, 14, 35, 34), fill=C["bg"])
            return img

    def hide_to_tray(self) -> None:
        self._hidden = True
        self.withdraw()
        if self.tray is None:
            menu = pystray.Menu(
                pystray.MenuItem("Show App", lambda _i, _m: self.events.put(("show",)), default=True),
                pystray.MenuItem("Exit App", lambda _i, _m: self.events.put(("exit",))),
            )
            self.tray = pystray.Icon("SmartShutdownPro", self._tray_image(), APP_NAME, menu)
            self.tray.run_detached()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.notify(APP_NAME, "Still running in the system tray. Right-click the icon for options.")

    def _stop_tray(self) -> None:
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
            self._cache.pop("tray", None)

    def restore_window(self, topmost_pulse: bool = False) -> None:
        self._stop_tray()
        was_hidden = self._hidden
        self._hidden = False
        if was_hidden:
            center_existing(self)
        self.deiconify()
        self.state("normal")
        self.lift()
        self.attributes("-topmost", True)
        if not topmost_pulse:
            self.after(250, lambda: self.attributes("-topmost", False))
        else:
            self.after(1500, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def _quit(self) -> None:
        self.engine.cancel(abort_windows=True, wait=True)
        self._stop_tray()
        try:
            self.destroy()
        except Exception:
            pass


def main() -> None:
    if not acquire_single_instance():
        return
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
