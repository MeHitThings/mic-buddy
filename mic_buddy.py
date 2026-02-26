"""
Mic Buddy - A cute always-on-top mic status overlay for OBS.
Shows a kawaii face indicating whether microphones are muted or live.
"""

import json
import math
import os
import threading
import time
import tkinter as tk
from pathlib import Path

import PIL.Image
import PIL.ImageDraw
import pystray
import psutil

# ---------------------------------------------------------------------------
# OBS WebSocket helper  (obsws-python wraps OBS-WebSocket v5 nicely)
# ---------------------------------------------------------------------------
try:
    import obsws_python as obsws
except ImportError:
    obsws = None

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
PINK = "#FF69B4"            # hot-pink for the happy face
PINK_DARK = "#E0559E"       # slightly darker for outlines / tray
PURPLE = "#9B59B6"          # medium purple for muted face
PURPLE_DARK = "#7D3C98"
BG_TRANSPARENT = "#010101"  # colour-key for transparency

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "MicBuddy"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_config(cfg: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# OBS connection manager (runs in its own thread)
# ---------------------------------------------------------------------------
class OBSManager:
    """Watches for OBS, connects via WebSocket, polls mute state."""

    def __init__(self, on_state_change, on_connection_change):
        self._on_state = on_state_change        # callback(all_live: bool)
        self._on_conn = on_connection_change     # callback(connected: bool)
        self._ws = None
        self._connected = False
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # -- public API ----------------------------------------------------------
    def stop(self):
        self._running = False
        self._disconnect()

    @property
    def connected(self):
        return self._connected

    # -- internals -----------------------------------------------------------
    @staticmethod
    def _obs_running() -> bool:
        for p in psutil.process_iter(["name"]):
            try:
                if p.info["name"] and p.info["name"].lower() in (
                    "obs64.exe", "obs32.exe", "obs.exe",
                    "obs64", "obs32", "obs",
                ):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def _connect(self):
        if obsws is None:
            return
        try:
            self._ws = obsws.ReqClient(host="localhost", port=4455, password="", timeout=5)
            self._connected = True
            self._on_conn(True)
        except Exception:
            self._ws = None
            self._connected = False

    def _disconnect(self):
        self._connected = False
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.base_client.ws.close()
            except Exception:
                pass
            try:
                del ws
            except Exception:
                pass
        self._on_conn(False)

    def _poll_mute(self):
        """Return True if ALL mic inputs are unmuted (live)."""
        if not self._ws:
            return None
        try:
            resp = self._ws.get_input_list()
            inputs = resp.inputs if hasattr(resp, "inputs") else []

            mic_inputs = []
            for inp in inputs:
                kind = inp.get("inputKind", "") or ""
                # Common audio-input source types in OBS
                if any(k in kind for k in (
                    "wasapi_input", "pulse_input", "coreaudio_input",
                    "alsa_input", "jack_input", "audio_input",
                )):
                    mic_inputs.append(inp)

            if not mic_inputs:
                # No mic inputs found – treat as muted to be safe
                return False

            all_live = True
            for inp in mic_inputs:
                name = inp.get("inputName", "")
                try:
                    mute_resp = self._ws.get_input_mute(name=name)
                    muted = mute_resp.input_muted
                except Exception:
                    muted = True
                if muted:
                    all_live = False
                    break

            return all_live
        except Exception:
            # Connection probably died
            self._disconnect()
            return None

    def _loop(self):
        was_obs_running = False
        while self._running:
            obs_up = self._obs_running()

            if obs_up and not self._connected:
                self._connect()
            elif not obs_up and self._connected:
                self._disconnect()

            if not obs_up and was_obs_running:
                # OBS just closed
                self._on_conn(False)
            if obs_up and not was_obs_running:
                # OBS just appeared – try connecting
                if not self._connected:
                    self._connect()

            was_obs_running = obs_up

            if self._connected:
                result = self._poll_mute()
                if result is not None:
                    self._on_state(result)

            time.sleep(1)


# ---------------------------------------------------------------------------
# Overlay window  (tkinter, always on top, transparent, draggable)
# ---------------------------------------------------------------------------
class OverlayWindow:
    SIZE = 100
    ANIM_FPS = 30
    BREATHE_SPEED = 2.0     # seconds per full cycle
    BREATHE_AMOUNT = 0.04   # 4 % scale variation

    def __init__(self, root: tk.Tk):
        self.root = root
        self._visible = False
        self._all_live = False        # False = muted (purple), True = live (pink)
        self._display_live = 0.0      # 0.0 = muted … 1.0 = live  (for fading)
        self._target_live = 0.0
        self._breathe_t = 0.0
        self._drag_start_x = 0
        self._drag_start_y = 0

        # ---- window setup --------------------------------------------------
        self.root.title("Mic Buddy")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", BG_TRANSPARENT)
        self.root.configure(bg=BG_TRANSPARENT)
        self.root.geometry(f"{self.SIZE}x{self.SIZE}")

        # Restore position
        cfg = load_config()
        x = cfg.get("x", None)
        y = cfg.get("y", None)
        if x is not None and y is not None:
            self.root.geometry(f"+{x}+{y}")
        else:
            self._reset_position()

        # Canvas
        self.canvas = tk.Canvas(
            self.root, width=self.SIZE, height=self.SIZE,
            bg=BG_TRANSPARENT, highlightthickness=0,
        )
        self.canvas.pack()

        # Drag bindings
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)

        # Start hidden
        self.root.withdraw()

        # Start animation loop
        self._animate()

    # -- public API ----------------------------------------------------------
    def show(self):
        if not self._visible:
            self._visible = True
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)

    def hide(self):
        if self._visible:
            self._visible = False
            self.root.withdraw()

    def set_state(self, all_live: bool):
        self._all_live = all_live
        self._target_live = 1.0 if all_live else 0.0

    def reset_position(self):
        self._reset_position()

    # -- internals -----------------------------------------------------------
    def _reset_position(self):
        sw = self.root.winfo_screenwidth()
        x = sw - self.SIZE - 20
        y = 20
        self.root.geometry(f"+{x}+{y}")
        self._save_position()

    def _save_position(self):
        try:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            cfg = load_config()
            cfg["x"] = x
            cfg["y"] = y
            save_config(cfg)
        except Exception:
            pass

    def _on_drag_start(self, event):
        self._drag_start_x = event.x
        self._drag_start_y = event.y

    def _on_drag_motion(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_start_x)
        y = self.root.winfo_y() + (event.y - self._drag_start_y)
        self.root.geometry(f"+{x}+{y}")

    def _on_drag_end(self, _event):
        self._save_position()

    @staticmethod
    def _lerp_colour(c1: str, c2: str, t: float) -> str:
        """Linearly interpolate between two hex colours."""
        r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
        r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
        r = int(r1 + (r2 - r1) * t)
        g = int(g1 + (g2 - g1) * t)
        b = int(b1 + (b2 - b1) * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _draw_face(self):
        c = self.canvas
        c.delete("all")

        t = self._display_live  # 0 = muted/purple, 1 = live/pink

        # Breathing scale
        scale = 1.0 + math.sin(self._breathe_t * 2 * math.pi / self.BREATHE_SPEED) * self.BREATHE_AMOUNT
        half = self.SIZE / 2
        r = (self.SIZE / 2 - 4) * scale  # radius

        # Interpolated colours
        face_col = self._lerp_colour(PURPLE, PINK, t)
        outline_col = self._lerp_colour(PURPLE_DARK, PINK_DARK, t)

        # --- Face circle ---
        c.create_oval(
            half - r, half - r, half + r, half + r,
            fill=face_col, outline=outline_col, width=2,
        )

        # --- Cheek blush (subtle pink circles, more visible when live) ---
        blush_alpha = 0.3 + 0.4 * t
        blush_col = self._lerp_colour(face_col, "#FF8FAF", blush_alpha)
        br = r * 0.15
        # Left cheek
        c.create_oval(
            half - r * 0.55 - br, half + r * 0.05 - br,
            half - r * 0.55 + br, half + r * 0.05 + br,
            fill=blush_col, outline="",
        )
        # Right cheek
        c.create_oval(
            half + r * 0.55 - br, half + r * 0.05 - br,
            half + r * 0.55 + br, half + r * 0.05 + br,
            fill=blush_col, outline="",
        )

        # --- Eyes ---
        # When live (t→1): happy ^_^ eyes (arcs curving up)
        # When muted (t→0): neutral -_- eyes (horizontal lines)
        eye_y = half - r * 0.15
        eye_left_x = half - r * 0.28
        eye_right_x = half + r * 0.28
        eye_w = r * 0.2
        eye_lw = max(2, r * 0.06)

        # Arc height: 0 = flat line, positive = happy curve
        arc_h = r * 0.18 * t

        for ex in (eye_left_x, eye_right_x):
            if t > 0.1:
                # Draw as an arc (happy squinty ^_^)
                # We'll draw a small upside-down arc using line segments
                pts = []
                steps = 10
                for i in range(steps + 1):
                    frac = i / steps
                    px = ex - eye_w + 2 * eye_w * frac
                    py = eye_y - arc_h * math.sin(frac * math.pi)
                    pts.extend([px, py])
                c.create_line(*pts, fill=outline_col, width=eye_lw,
                              smooth=True, capstyle="round")
            if t < 0.9:
                # Draw as a flat line (-_-)
                alpha = 1.0 - t
                line_col = self._lerp_colour(face_col, outline_col, alpha)
                c.create_line(
                    ex - eye_w, eye_y, ex + eye_w, eye_y,
                    fill=line_col, width=eye_lw, capstyle="round",
                )

        # --- Mouth ---
        mouth_y = half + r * 0.3
        mouth_w = r * 0.35
        mouth_lw = max(2, r * 0.06)

        # Smile amount: 0 = straight, 1 = big smile
        smile = t
        smile_depth = r * 0.2 * smile

        if smile > 0.05:
            # Draw smile arc
            pts = []
            steps = 12
            for i in range(steps + 1):
                frac = i / steps
                px = half - mouth_w + 2 * mouth_w * frac
                py = mouth_y + smile_depth * math.sin(frac * math.pi)
                pts.extend([px, py])
            c.create_line(*pts, fill=outline_col, width=mouth_lw,
                          smooth=True, capstyle="round")
        if smile < 0.95:
            # Draw straight line for muted look
            alpha = 1.0 - smile
            line_col = self._lerp_colour(face_col, outline_col, alpha)
            c.create_line(
                half - mouth_w, mouth_y, half + mouth_w, mouth_y,
                fill=line_col, width=mouth_lw, capstyle="round",
            )

    def _animate(self):
        dt = 1.0 / self.ANIM_FPS

        # Smooth fade towards target
        fade_speed = 3.0  # per second
        diff = self._target_live - self._display_live
        step = fade_speed * dt
        if abs(diff) < step:
            self._display_live = self._target_live
        else:
            self._display_live += step if diff > 0 else -step

        self._breathe_t += dt

        if self._visible:
            self._draw_face()

        self.root.after(int(dt * 1000), self._animate)


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------
def make_tray_icon(colour: str, size: int = 64) -> PIL.Image.Image:
    """Create a simple circle icon for the system tray."""
    img = PIL.Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = PIL.ImageDraw.Draw(img)
    margin = 4
    draw.ellipse([margin, margin, size - margin, size - margin], fill=colour)
    return img


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
class MicBuddyApp:
    def __init__(self):
        self._all_live = False
        self._connected = False

        # Tk root
        self.root = tk.Tk()
        self.root.withdraw()  # hide the default root briefly

        # Overlay
        self.overlay = OverlayWindow(self.root)

        # OBS manager
        self.obs = OBSManager(
            on_state_change=self._on_state_change,
            on_connection_change=self._on_connection_change,
        )

        # System tray (runs in its own thread)
        self._build_tray()

    # -- callbacks (called from OBS thread) ----------------------------------
    def _on_state_change(self, all_live: bool):
        self._all_live = all_live
        # Schedule UI update on the main thread
        try:
            self.root.after_idle(self._update_ui)
        except Exception:
            pass

    def _on_connection_change(self, connected: bool):
        was_connected = self._connected
        self._connected = connected
        try:
            if connected:
                self.root.after_idle(self.overlay.show)
            else:
                self.root.after_idle(self.overlay.hide)
            self.root.after_idle(self._update_tray_menu)
        except Exception:
            pass

    # -- UI updates ----------------------------------------------------------
    def _update_ui(self):
        self.overlay.set_state(self._all_live)
        self._update_tray_icon()
        self._update_tray_menu()

    def _update_tray_icon(self):
        colour = PINK if self._all_live else PURPLE
        try:
            self._tray.icon = make_tray_icon(colour)
        except Exception:
            pass

    def _update_tray_menu(self):
        """Rebuild the tray menu to reflect current state."""
        try:
            self._tray.menu = self._make_menu()
        except Exception:
            pass

    # -- tray ----------------------------------------------------------------
    def _make_menu(self) -> pystray.Menu:
        conn_text = "OBS Status: Connected" if self._connected else "OBS Status: Waiting…"
        mic_text = "Mic Status: Live" if self._all_live else "Mic Status: Muted"
        return pystray.Menu(
            pystray.MenuItem("Mic Buddy", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(conn_text, None, enabled=False),
            pystray.MenuItem(mic_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Reset Position", self._on_reset_position),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    def _build_tray(self):
        icon_img = make_tray_icon(PURPLE)
        self._tray = pystray.Icon(
            "mic_buddy",
            icon=icon_img,
            title="Mic Buddy",
            menu=self._make_menu(),
        )
        tray_thread = threading.Thread(target=self._tray.run, daemon=True)
        tray_thread.start()

    def _on_reset_position(self, _icon=None, _item=None):
        try:
            self.root.after_idle(self.overlay.reset_position)
        except Exception:
            pass

    def _on_quit(self, _icon=None, _item=None):
        self.obs.stop()
        try:
            self._tray.stop()
        except Exception:
            pass
        try:
            self.root.after_idle(self.root.destroy)
        except Exception:
            pass

    # -- run -----------------------------------------------------------------
    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    app = MicBuddyApp()
    app.run()


if __name__ == "__main__":
    main()
