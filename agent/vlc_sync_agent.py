#!/usr/bin/env python3
"""
Video Sync - VLC Edition

Cross-platform desktop agent that:
- starts VLC with its HTTP control interface when needed
- reads VLC playback status
- sends play/pause/seek/rate state through a public WSS relay
- applies remote state with latency compensation
- gradually corrects small playback drift
- keeps VLC's HTTP interface bound to localhost

Tested design target: VLC 3.x, Python 3.10+.
"""

from __future__ import annotations

import base64
import hashlib
import math
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import requests
except ImportError:
    requests = None

try:
    import websocket
except ImportError:
    websocket = None


APP_NAME = "Video Sync - VLC Edition"
DEFAULT_SERVER = "wss://video-sync-vlc.onrender.com"
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8081
DEFAULT_HTTP_PASSWORD = "video-sync-local"

POLL_INTERVAL = 0.20

def safe_float(value, fallback=0.0):
    try:
        n = float(value)
        return n if math.isfinite(n) else fallback
    except (TypeError, ValueError):
        return fallback

def safe_rate(value):
    return max(0.05, min(8.0, safe_float(value, 1.0)))

CLOCK_INTERVAL = 5.0
SYNC_DEBOUNCE = 0.12

SMALL_DRIFT = 0.080
MEDIUM_DRIFT = 0.250
LARGE_DRIFT = 0.800

RATE_MIN = 0.985
RATE_MAX = 1.015

APP_DIR = Path.home() / ".video-sync-vlc"
CONFIG_PATH = APP_DIR / "config.json"


def now_ms() -> int:
    return int(time.time() * 1000)


def make_room_code(length: int = 6) -> str:
    raw = base64.b32encode(os.urandom(8)).decode("ascii").rstrip("=")
    return "".join(c for c in raw if c.isalnum())[:length].upper()


def normalize_server_url(url: str) -> str:
    url = url.strip()
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    return url.rstrip("/")


def load_config() -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)

    defaults = {
        "server_url": DEFAULT_SERVER,
        "room": "",
        "http_host": DEFAULT_HTTP_HOST,
        "http_port": DEFAULT_HTTP_PORT,
        "http_password": DEFAULT_HTTP_PASSWORD
    }

    if not CONFIG_PATH.exists():
        return defaults

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        defaults.update(data)
    except Exception:
        pass

    return defaults


def save_config(data: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )


def find_vlc() -> str | None:
    candidates = []

    found = shutil.which("vlc")
    if found:
        candidates.append(found)

    system = platform.system()

    if system == "Windows":
        program_files = os.environ.get("ProgramFiles")
        program_files_x86 = os.environ.get("ProgramFiles(x86)")

        for base in [program_files, program_files_x86]:
            if base:
                candidates.append(
                    os.path.join(base, "VideoLAN", "VLC", "vlc.exe")
                )

        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(
                os.path.join(local, "Programs", "VideoLAN", "VLC", "vlc.exe")
            )

    elif system == "Darwin":
        candidates.append("/Applications/VLC.app/Contents/MacOS/VLC")

    elif system == "Linux":
        candidates.extend([
            "/usr/bin/vlc",
            "/usr/local/bin/vlc",
            "/snap/bin/vlc"
        ])

    for path in candidates:
        if path and Path(path).exists():
            return path

    return None


class VLCController:
    def __init__(self, host: str, port: int, password: str, log):
        self.host = host
        self.port = int(port)
        self.password = password
        self.log = log
        self.process: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _request(self, command: str | None = None, params: dict | None = None):
        if requests is None:
            raise RuntimeError(
                "Python package 'requests' is missing. Run: pip install -r requirements.txt"
            )

        query = {}
        if command:
            query["command"] = command

        if params:
            query.update(params)

        url = self.base_url + "/requests/status.json"
        auth = ("", self.password)

        response = requests.get(
            url,
            params=query,
            auth=auth,
            timeout=1.2
        )
        response.raise_for_status()
        return response.json()

    def status(self) -> dict:
        return self._request()

    def command(self, command: str, **params):
        return self._request(command, params)

    def is_reachable(self) -> bool:
        try:
            self.status()
            return True
        except Exception:
            return False

    def launch(self, media_path: str | None = None) -> None:
        if self.is_reachable():
            if media_path:
                self.open_media(media_path)
            return

        vlc_path = find_vlc()
        if not vlc_path:
            raise RuntimeError(
                "VLC was not found. Install VLC from VideoLAN first."
            )

        args = [
            vlc_path,
            "--extraintf=http",
            "--http-host", self.host,
            "--http-port", str(self.port),
            "--http-password", self.password,
            "--no-video-title-show",
        ]

        if media_path:
            args.append(str(Path(media_path).resolve()))

        self.log(f"Starting VLC: {vlc_path}")

        creationflags = 0
        if platform.system() == "Windows":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags
        )

        deadline = time.time() + 8

        while time.time() < deadline:
            if self.is_reachable():
                self.log("VLC HTTP interface connected.")
                return
            time.sleep(0.2)

        raise RuntimeError(
            "VLC started, but its local HTTP interface did not respond."
        )

    def open_media(self, media_path: str) -> None:
        # Send the decoded local filesystem path. requests will URL-encode the
        # query exactly once, and VLC's HTTP interface converts the decoded
        # input into a media URI. Pre-encoding with Path.as_uri() causes
        # spaces such as %20 to become %2520 on the wire.
        resolved = Path(media_path).resolve()
        if platform.system() == "Windows":
            input_value = resolved.as_posix()
        else:
            input_value = str(resolved)
        self.command("in_play", input=input_value)

    def play(self):
        return self.command("pl_play")

    def pause(self):
        return self.command("pl_forcepause")

    def stop(self):
        return self.command("pl_stop")

    def seek(self, seconds: float):
        return self.command("seek", val=f"{max(0.0, seconds):.3f}")

    def set_rate(self, rate: float):
        return self.command("rate", val=f"{max(0.05, rate):.4f}")


class SyncClient:
    def __init__(self, ui_queue: queue.Queue, log):
        self.ui_queue = ui_queue
        self.log = log
        self.ws = None
        self.thread = None
        self.running = False
        self.room = None
        self.role = None
        self.connected = False
        self.clock_offset_ms = 0.0
        self.last_ping_sent = 0

    def _emit(self, event: str, data=None):
        self.ui_queue.put((event, data))

    def connect(self, server_url: str):
        if websocket is None:
            raise RuntimeError(
                "Python package 'websocket-client' is missing. "
                "Run: pip install -r requirements.txt"
            )

        server_url = normalize_server_url(server_url)

        if not server_url.startswith(("ws://", "wss://")):
            raise ValueError("Server URL must start with ws:// or wss://")

        self.disconnect()

        self.running = True

        def run():
            while self.running:
                try:
                    self.log(f"Connecting to {server_url}")
                    self.ws = websocket.create_connection(
                        server_url,
                        timeout=5,
                        enable_multithread=True
                    )

                    self.connected = True
                    self._emit("connected", None)
                    self.log("Connected to sync server.")

                    while self.running:
                        try:
                            raw = self.ws.recv()
                            if raw is None:
                                break

                            message = json.loads(raw)
                            self._handle(message)

                        except websocket.WebSocketTimeoutException:
                            continue
                        except Exception as exc:
                            if self.running:
                                self.log(f"WebSocket receive error: {exc}")
                            break

                except Exception as exc:
                    if self.running:
                        self.log(f"Connection failed: {exc}")
                        self._emit("connection-error", str(exc))

                finally:
                    self.connected = False
                    self._emit("disconnected", None)

                    try:
                        if self.ws:
                            self.ws.close()
                    except Exception:
                        pass

                    self.ws = None

                if self.running:
                    time.sleep(2)

        self.thread = threading.Thread(
            target=run,
            name="sync-websocket",
            daemon=True
        )
        self.thread.start()

    def disconnect(self):
        self.running = False

        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

        self.ws = None
        self.connected = False

    def send(self, message: dict) -> bool:
        if not self.ws or not self.connected:
            return False

        try:
            self.ws.send(json.dumps(message, allow_nan=False, separators=(",", ":")))
            return True
        except Exception as exc:
            self.log(f"Send error: {exc}")
            return False

    def create_room(self, room: str):
        self.send({
            "type": "create-room",
            "room": room
        })

    def join_room(self, room: str):
        self.send({
            "type": "join-room",
            "room": room
        })

    def leave_room(self):
        self.send({"type": "leave-room"})
        self.room = None
        self.role = None

    def send_sync(
        self,
        time_seconds: float,
        playing: bool,
        rate: float,
        video_key: str,
        action: str = "state"
    ):
        self.send({
            "type": "sync",
            "action": action,
            "time": float(time_seconds),
            "playing": bool(playing),
            "playbackRate": float(rate),
            "videoKey": video_key
        })

    def send_clock_ping(self):
        self.last_ping_sent = now_ms()
        self.send({
            "type": "clock-ping",
            "clientTime": self.last_ping_sent
        })

    def _handle(self, message: dict):
        message_type = message.get("type")

        if message_type == "connected":
            return

        if message_type == "room-created":
            self.room = message.get("room")
            self.role = message.get("role")
            self._emit("room", {
                "room": self.room,
                "role": self.role
            })
            return

        if message_type == "room-joined":
            self.room = message.get("room")
            self.role = message.get("role")
            self._emit("room", {
                "room": self.room,
                "role": self.role
            })
            return

        if message_type == "peer-joined":
            self._emit("peer-joined", None)
            return

        if message_type == "peer-left":
            self._emit("peer-left", None)
            return

        if message_type == "session-left":
            self.room = None
            self.role = None
            self._emit("left", None)
            return

        if message_type == "clock-pong":
            sent = float(message.get("clientTime", now_ms()))
            server_time = float(message.get("serverTime", now_ms()))
            received = now_ms()

            midpoint = (sent + received) / 2.0
            self.clock_offset_ms = server_time - midpoint
            self._emit("clock", self.clock_offset_ms)
            return

        if message_type == "sync":
            self._emit("remote-sync", message)
            return

        if message_type == "error":
            self._emit("server-error", message.get("message", "Unknown error"))
            return


class VideoSyncApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("650x690")
        self.root.minsize(620, 650)

        self.config = load_config()
        self.events = queue.Queue()

        self.log_lines = []
        self.controller = VLCController(
            self.config["http_host"],
            self.config["http_port"],
            self.config["http_password"],
            self.log
        )
        self.sync = SyncClient(self.events, self.log)

        self.video_key = ""
        self.last_local_state = None
        self.last_sent_state = None
        self.remote_apply_lock_until = 0.0
        self.last_remote_sync = 0.0
        self.last_remote_event_id = ""
        self.last_sent_at = 0.0
        self.peer_present = False
        self.vlc_ok = False
        self.running = True

        self.server_var = tk.StringVar(
            value=self.config["server_url"]
        )
        self.room_var = tk.StringVar(
            value=self.config.get("room", "")
        )
        self.status_var = tk.StringVar(value="Disconnected")
        self.vlc_status_var = tk.StringVar(value="VLC: Not connected")
        self.role_var = tk.StringVar(value="No session")
        self.time_var = tk.StringVar(value="00:00 / 00:00")
        self.drift_var = tk.StringVar(value="Sync: —")
        self.file_var = tk.StringVar(value="No media detected")
        self.peer_var = tk.StringVar(value="Peer: —")
        self.clock_var = tk.StringVar(value="Clock: —")

        self._build_ui()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.root.after(100, self.process_events)
        self.root.after(200, self.poll_vlc)
        self.root.after(1000, self.clock_loop)

    def log(self, message: str):
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-100:]
        try:
            self.root.after(0, self._refresh_log)
        except Exception:
            pass

    def _refresh_log(self):
        if hasattr(self, "log_text"):
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", "\n".join(self.log_lines))
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="VIDEO SYNC",
            font=("TkDefaultFont", 18, "bold")
        ).pack(anchor="w")

        ttk.Label(
            outer,
            text="Synchronize local VLC playback between two computers",
        ).pack(anchor="w", pady=(0, 14))

        server_frame = ttk.LabelFrame(
            outer, text="Connection", padding=12
        )
        server_frame.pack(fill="x", pady=5)

        ttk.Label(server_frame, text="Server").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(
            server_frame,
            textvariable=self.server_var
        ).grid(row=0, column=1, sticky="ew", padx=8)

        self.connect_button = ttk.Button(
            server_frame,
            text="Connect",
            command=self.toggle_server
        )
        self.connect_button.grid(row=0, column=2)

        server_frame.columnconfigure(1, weight=1)

        session_frame = ttk.LabelFrame(
            outer, text="Session", padding=12
        )
        session_frame.pack(fill="x", pady=5)

        ttk.Label(session_frame, text="Room").grid(
            row=0, column=0, sticky="w"
        )

        ttk.Entry(
            session_frame,
            textvariable=self.room_var,
            width=14
        ).grid(row=0, column=1, sticky="w", padx=8)

        ttk.Button(
            session_frame,
            text="Create",
            command=self.create_room
        ).grid(row=0, column=2, padx=3)

        ttk.Button(
            session_frame,
            text="Join",
            command=self.join_room
        ).grid(row=0, column=3, padx=3)

        ttk.Button(
            session_frame,
            text="Leave",
            command=self.leave_room
        ).grid(row=0, column=4, padx=3)

        ttk.Label(
            session_frame,
            textvariable=self.role_var
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))

        vlc_frame = ttk.LabelFrame(
            outer, text="VLC", padding=12
        )
        vlc_frame.pack(fill="x", pady=5)

        ttk.Label(
            vlc_frame,
            textvariable=self.vlc_status_var
        ).pack(anchor="w")

        ttk.Label(
            vlc_frame,
            textvariable=self.file_var,
            wraplength=560
        ).pack(anchor="w", pady=(4, 8))

        buttons = ttk.Frame(vlc_frame)
        buttons.pack(fill="x")

        ttk.Button(
            buttons, text="Open / Start VLC",
            command=self.open_vlc
        ).pack(side="left", padx=(0, 5))

        ttk.Button(
            buttons, text="Choose Video",
            command=self.choose_video
        ).pack(side="left", padx=5)

        ttk.Button(
            buttons, text="Sync Now",
            command=lambda: self.send_current_state("manual-sync")
        ).pack(side="left", padx=5)

        playback_buttons = ttk.Frame(vlc_frame)
        playback_buttons.pack(fill="x", pady=(10, 0))

        ttk.Button(
            playback_buttons, text="▶ Play", width=12,
            command=self.play_video
        ).pack(side="left", padx=(0, 6))

        ttk.Button(
            playback_buttons, text="⏸ Pause", width=12,
            command=self.pause_video
        ).pack(side="left", padx=6)

        ttk.Button(
            playback_buttons, text="↻ Restart", width=12,
            command=self.restart_video
        ).pack(side="left", padx=6)

        state_frame = ttk.LabelFrame(
            outer, text="Playback", padding=12
        )
        state_frame.pack(fill="x", pady=5)

        ttk.Label(
            state_frame,
            textvariable=self.time_var,
            font=("TkDefaultFont", 14, "bold")
        ).pack(anchor="w")

        ttk.Label(
            state_frame,
            textvariable=self.drift_var
        ).pack(anchor="w", pady=(5, 0))

        ttk.Label(
            state_frame,
            textvariable=self.peer_var
        ).pack(anchor="w", pady=(3, 0))

        ttk.Label(
            state_frame,
            textvariable=self.clock_var
        ).pack(anchor="w", pady=(3, 0))

        log_frame = ttk.LabelFrame(
            outer, text="Log", padding=8
        )
        log_frame.pack(fill="both", expand=True, pady=(5, 0))

        self.log_text = tk.Text(
            log_frame,
            height=8,
            wrap="word",
            state="disabled"
        )
        self.log_text.pack(fill="both", expand=True)

        self.log("Ready.")
        self.log("Start VLC through this app so its HTTP interface is enabled.")

    def toggle_server(self):
        if self.sync.running:
            self.sync.disconnect()
            self.connect_button.configure(text="Connect")
            self.status_var.set("Disconnected")
            return

        try:
            url = normalize_server_url(self.server_var.get())
            self.server_var.set(url)

            self.config["server_url"] = url
            save_config(self.config)

            self.sync.connect(url)
            self.connect_button.configure(text="Disconnect")
            self.status_var.set("Connecting...")
        except Exception as exc:
            messagebox.showerror("Connection", str(exc))

    def create_room(self):
        if not self.sync.connected:
            messagebox.showwarning("Not connected", "Connect to the server first.")
            return

        room = self.room_var.get().strip().upper() or make_room_code()
        self.room_var.set(room)
        self.config["room"] = room
        save_config(self.config)
        self.sync.create_room(room)

    def join_room(self):
        if not self.sync.connected:
            messagebox.showwarning("Not connected", "Connect to the server first.")
            return

        room = self.room_var.get().strip().upper()
        if not room:
            messagebox.showwarning("Room required", "Enter the room code.")
            return

        self.config["room"] = room
        save_config(self.config)
        self.sync.join_room(room)

    def leave_room(self):
        self.sync.leave_room()
        self.peer_present = False
        self.role_var.set("No session")
        self.peer_var.set("Peer: —")

    def open_vlc(self):
        try:
            self.controller.launch()
            self.vlc_ok = True
            self.vlc_status_var.set("VLC: Connected")
        except Exception as exc:
            self.vlc_ok = False
            messagebox.showerror(
                "VLC",
                f"{exc}\n\nInstall VLC from the official VideoLAN website if needed."
            )

    def choose_video(self):
        path = filedialog.askopenfilename(
            title="Choose local video",
            filetypes=[
                ("Video files", "*.mkv *.mp4 *.avi *.mov *.webm *.wmv *.m4v *.ts"),
                ("All files", "*.*")
            ]
        )

        if not path:
            return

        try:
            self.controller.launch(path)
            self.vlc_ok = True
            self.file_var.set(Path(path).name)
            self.video_key = self.make_video_key(path)
            self.vlc_status_var.set("VLC: Connected")
            self.log(f"Opened: {path}")
        except Exception as exc:
            messagebox.showerror("VLC", str(exc))

    def _playback_command(self, action: str):
        if not self.vlc_ok:
            messagebox.showwarning("VLC", "Start VLC and open a video first.")
            return

        try:
            if action == "play":
                self.controller.play()
                self.log("Play pressed.")
            elif action == "pause":
                self.controller.pause()
                self.log("Pause pressed.")
            elif action == "restart":
                # Restart means return to 00:00 and start playing.
                self.controller.seek(0.0)
                self.controller.play()
                self.log("Restart pressed.")
            else:
                return

            # VLC updates its HTTP status asynchronously. Give it a moment,
            # then immediately publish the new state instead of waiting for
            # the next polling/heartbeat cycle.
            self.root.after(250, lambda: self._send_playback_state(action))
        except Exception as exc:
            messagebox.showerror("Playback", str(exc))
            self.log(f"Playback command failed: {exc}")

    def _send_playback_state(self, action: str):
        try:
            status = self.controller.status()
            self.last_local_state = {
                "time": max(0.0, safe_float(status.get("time"), 0.0)),
                "length": max(0.0, safe_float(status.get("length"), 0.0)),
                "playing": str(status.get("state", "stopped")).lower() == "playing",
                "rate": safe_rate(status.get("rate")),
                "filename": str(status.get("filename") or "")
            }
            self.send_current_state(f"button-{action}")
        except Exception as exc:
            self.log(f"Could not send playback state: {exc}")

    def play_video(self):
        self._playback_command("play")

    def pause_video(self):
        self._playback_command("pause")

    def restart_video(self):
        self._playback_command("restart")

    def make_video_key(self, path: str) -> str:
        # The same video copied to another computer normally has a different
        # filesystem modification time. Using mtime made valid peers look like
        # different videos and caused the receiver to silently ignore sync.
        # Filename + size is stable across machines and is sufficient as a
        # lightweight identity check for this local two-computer workflow.
        p = Path(path)

        try:
            stat = p.stat()
            sample = f"{p.name.casefold()}|{stat.st_size}"
        except Exception:
            sample = p.name.casefold()

        return hashlib.sha256(sample.encode("utf-8")).hexdigest()[:16]

    def poll_vlc(self):
        if not self.running:
            return

        try:
            status = self.controller.status()
            self.vlc_ok = True
            self.vlc_status_var.set("VLC: Connected")

            time_seconds = max(0.0, safe_float(status.get("time"), 0.0))
            length = max(0.0, safe_float(status.get("length"), 0.0))
            state = str(status.get("state", "stopped")).lower()
            playing = state == "playing"
            rate = safe_rate(status.get("rate"))

            info = status.get("information") or {}
            meta = info.get("meta") or {}
            filename = (
                status.get("filename")
                or meta.get("filename")
                or meta.get("title")
                or ""
            )

            if filename:
                self.file_var.set(str(filename))

            self.time_var.set(
                f"{self.format_time(time_seconds)} / {self.format_time(length)}"
            )

            current = {
                "time": time_seconds,
                "length": length,
                "playing": playing,
                "rate": rate,
                "filename": str(filename)
            }

            previous = self.last_local_state
            self.last_local_state = current

            if previous:
                state_changed = (
                    previous["playing"] != current["playing"]
                    or abs(previous["time"] - current["time"]) > 0.75
                    or abs(previous["rate"] - current["rate"]) > 0.01
                )

                if state_changed and time.time() >= self.remote_apply_lock_until:
                    self.send_current_state("local-change")

            if (
                self.sync.connected
                and self.sync.room
                and time.time() - self.last_remote_sync > 1.0
            ):
                # Only the master sends periodic authoritative state.
                # Client devices react to the master's state.
                if self.sync.role == "master":
                    self.send_current_state("heartbeat")

        except Exception:
            self.vlc_ok = False
            self.vlc_status_var.set("VLC: Not connected")

        self.root.after(int(POLL_INTERVAL * 1000), self.poll_vlc)

    def clock_loop(self):
        if not self.running:
            return

        if self.sync.connected:
            self.sync.send_clock_ping()

        self.root.after(int(CLOCK_INTERVAL * 1000), self.clock_loop)

    def send_current_state(self, action: str):
        if not self.sync.connected or not self.sync.room:
            return

        if not self.vlc_ok or not self.last_local_state:
            return

        state = self.last_local_state

        key = self.video_key
        if not key and state.get("filename"):
            key = hashlib.sha256(
                state["filename"].casefold().encode("utf-8")
            ).hexdigest()[:16]

        payload_key = key or ""
        now = time.time()

        current_signature = (
            round(state["time"], 1),
            state["playing"],
            round(state["rate"], 3),
            payload_key
        )

        if action == "heartbeat":
            # One authoritative heartbeat per second is enough.
            if now - self.last_sent_at < 1.0:
                return

        self.last_sent_state = current_signature
        self.last_sent_at = now
        self.sync.send_sync(
            time_seconds=state["time"],
            playing=state["playing"],
            rate=state["rate"],
            video_key=payload_key,
            action=action
        )

    def apply_remote_sync(self, message: dict):
        if not self.vlc_ok:
            return

        remote_time = max(0.0, safe_float(message.get("time"), 0.0))
        remote_playing = bool(message.get("playing", False))
        remote_rate = safe_rate(message.get("playbackRate"))
        sent_at = safe_float(message.get("sentAt"), now_ms())

        # Convert server timestamp to our local clock.
        estimated_now_server_ms = now_ms() + self.sync.clock_offset_ms

        elapsed = max(
            0.0,
            (estimated_now_server_ms - sent_at) / 1000.0
        )

        target_time = remote_time
        if remote_playing:
            target_time += elapsed * remote_rate

        remote_key = str(message.get("videoKey", ""))
        if remote_key and self.video_key and remote_key != self.video_key:
            # Do not block synchronization solely on metadata differences.
            # Files copied between computers can legitimately have different
            # filesystem metadata. The user has already selected the local
            # video, so playback state is still safe to synchronize.
            self.peer_var.set("Peer: connected (video identity differs)")

        try:
            local = self.controller.status()
            local_time = max(0.0, safe_float(local.get("time"), 0.0))
            local_state = str(local.get("state", "")).lower()
            local_playing = local_state == "playing"
            local_rate = safe_rate(local.get("rate"))

            drift = target_time - local_time
            self.drift_var.set(
                f"Sync: {drift * 1000:+.0f} ms"
            )

            self.remote_apply_lock_until = time.time() + 0.5
            self.last_remote_sync = time.time()

            if abs(drift) >= LARGE_DRIFT:
                self.controller.seek(target_time)
                self.controller.set_rate(remote_rate)

            elif abs(drift) >= MEDIUM_DRIFT:
                self.controller.seek(target_time)
                self.controller.set_rate(remote_rate)

            elif abs(drift) >= SMALL_DRIFT:
                correction = max(
                    RATE_MIN,
                    min(
                        RATE_MAX,
                        1.0 + max(-0.015, min(0.015, drift * 0.06))
                    )
                )

                if remote_playing:
                    self.controller.set_rate(correction * remote_rate)
                else:
                    self.controller.set_rate(remote_rate)

            else:
                self.controller.set_rate(remote_rate)

            if remote_playing and not local_playing:
                self.controller.play()
            elif not remote_playing and local_playing:
                self.controller.pause()

            self.peer_var.set(
                f"Peer: {message.get('sourceRole', 'connected')}"
            )

        except Exception as exc:
            self.log(f"Remote apply failed: {exc}")

    def process_events(self):
        if not self.running:
            return

        try:
            while True:
                event, data = self.events.get_nowait()

                if event == "connected":
                    self.status_var.set("Connected")
                    self.connect_button.configure(text="Disconnect")

                elif event == "disconnected":
                    self.status_var.set("Disconnected")
                    self.connect_button.configure(text="Connect")

                elif event == "connection-error":
                    self.status_var.set("Retrying...")
                    self.log(str(data))

                elif event == "room":
                    self.room_var.set(data["room"])
                    self.role_var.set(
                        f"Room: {data['room']}   Role: {data['role']}"
                    )
                    self.log(
                        f"Session active: {data['room']} ({data['role']})"
                    )

                elif event == "peer-joined":
                    self.peer_present = True
                    self.peer_var.set("Peer: connected")
                    self.log("Peer joined.")

                    if self.sync.role == "master":
                        self.send_current_state("initial-sync")

                elif event == "peer-left":
                    self.peer_present = False
                    self.peer_var.set("Peer: disconnected")
                    self.log("Peer left.")

                elif event == "left":
                    self.role_var.set("No session")
                    self.peer_var.set("Peer: —")

                elif event == "clock":
                    self.clock_var.set(
                        f"Clock offset: {data:+.0f} ms"
                    )

                elif event == "remote-sync":
                    # MVP authority remains master -> client. The important
                    # part is that every received state is applied to the
                    # client, including play/pause and seeks.
                    if self.sync.role == "client":
                        self.apply_remote_sync(data)

                elif event == "server-error":
                    self.log(f"Server error: {data}")

        except queue.Empty:
            pass

        self.root.after(100, self.process_events)

    @staticmethod
    def format_time(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)

        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"

        return f"{minutes:02d}:{secs:02d}"

    def on_close(self):
        self.running = False

        try:
            self.sync.leave_room()
            self.sync.disconnect()
        except Exception:
            pass

        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required.")

    root = tk.Tk()
    app = VideoSyncApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
