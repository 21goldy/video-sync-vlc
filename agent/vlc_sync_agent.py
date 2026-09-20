#!/usr/bin/env python3
"""
Video Sync VLC v3
Event-driven, bidirectional VLC synchronization.

Design:
- Both computers are peers; either can Play/Pause/Seek/Restart.
- The server orders commands and assigns a shared execution time.
- Clients estimate server clock offset with NTP-style ping/pong.
- Playback position is NOT continuously forced by heartbeats.
- Drift is corrected gently with VLC playback-rate changes.
- A hard seek is used only for large drift and only with a cooldown.
- Local UI actions are the only authoritative commands; remote commands are
  marked as remote so they never echo back into the room.
"""
import base64
import hashlib
import json
import os
import platform
import queue
import random
import subprocess
import sys
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import requests
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import websocket


APP_VERSION = "3.0.0"
DEFAULT_SERVER = "wss://video-sync-vlc.onrender.com"
VLC_HOST = "127.0.0.1"
VLC_PORT = 8081
VLC_PASSWORD = "video-sync-local"

POLL_MS = 250
CLOCK_INTERVAL = 2.0
DRIFT_INTERVAL = 0.75
SMALL_DRIFT = 0.12       # seconds
LARGE_DRIFT = 3.00       # seconds
HARD_SEEK_COOLDOWN = 8.0
MAX_RATE_ADJUST = 0.035  # +/-3.5%
COMMAND_DELAY_MS = 250

def now_ms():
    return int(time.time() * 1000)

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def media_key(path):
    p = Path(path).resolve()
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    # filename + size is fast and stable across OSes.
    return f"{p.name.lower()}::{size}"

def format_time(seconds):
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class VLCController:
    def __init__(self, log):
        self.log = log
        self.process = None
        self.media_path = None
        self.port = VLC_PORT
        self.base = f"http://{VLC_HOST}:{self.port}"
        self.launching = False
        self.auth = ("", VLC_PASSWORD)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update({"Connection": "close"})
        self._lock = threading.Lock()

    def _request(self, command, **params):
        params = {"command": command, **params}
        try:
            r = self.session.get(
                self.base + "/requests/status.json",
                params=params,
                timeout=1.5,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            self.log(f"VLC HTTP error: {e}")
            return None

    def _set_port(self, port):
        self.port = int(port)
        self.base = f"http://{VLC_HOST}:{self.port}"

    def _port_available(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((VLC_HOST, int(port)))
                return True
            except OSError:
                return False

    def _choose_port(self):
        if self._port_available(VLC_PORT):
            return VLC_PORT
        for port in range(VLC_PORT + 1, VLC_PORT + 20):
            if self._port_available(port):
                return port
        return VLC_PORT

    def status(self):
        try:
            r = self.session.get(
                self.base + "/requests/status.json",
                timeout=1.0,
            )
            r.raise_for_status()
            d = r.json()
            state = str(d.get("state", "stopped"))
            return {
                "connected": True,
                "state": state,
                "time": float(d.get("time", 0) or 0),
                "length": float(d.get("length", 0) or 0),
                "rate": float(d.get("rate", 1) or 1),
                "position": float(d.get("position", 0) or 0),
                "filename": self.media_path.name if self.media_path else "",
            }
        except Exception:
            return {"connected": False, "state": "unknown", "time": 0,
                    "length": 0, "rate": 1, "position": 0, "filename": ""}

    def command(self, command, **params):
        return self._request(command, **params)

    def play(self):
        return self.command("pl_forceresume")

    def pause(self):
        return self.command("pl_forcepause")

    def seek(self, seconds):
        return self.command("seek", val=str(max(0.0, float(seconds))))

    def rate(self, value):
        value = clamp(float(value), 0.10, 4.0)
        return self.command("rate", val=f"{value:.5f}")

    def open_media(self, path):
        p = Path(path).resolve()
        self.media_path = p
        # VLC's HTTP API expects a decoded input; requests performs query
        # encoding once. Do not pre-encode spaces as %20.
        value = p.as_posix() if platform.system() == "Windows" else str(p)
        return self.command("in_play", input=value)

    def find_vlc(self):
        system = platform.system()
        candidates = []
        if system == "Windows":
            candidates = [
                os.environ.get("ProgramFiles", r"C:\Program Files") + r"\VideoLAN\VLC\vlc.exe",
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)") + r"\VideoLAN\VLC\vlc.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\VLC\vlc.exe"),
            ]
            for c in candidates:
                if c and os.path.exists(c):
                    return c
            return "vlc.exe"
        if system == "Darwin":
            return "/Applications/VLC.app/Contents/MacOS/VLC"
        return "vlc"

    def start(self, path=None):
        # First use an already-running VLC only if its HTTP API is actually reachable.
        if self.status().get("connected"):
            if path:
                result = self.open_media(path)
                return result is not None
            return True

        exe = self.find_vlc()
        if not (os.path.isabs(exe) and os.path.exists(exe)) and shutil.which(exe) is None:
            self.log("VLC executable was not found. Install VLC and try again.")
            return False

        self._set_port(self._choose_port())
        args = [
            exe,
            "--extraintf=http",
            "--http-host", VLC_HOST,
            "--http-port", str(self.port),
            "--http-password", VLC_PASSWORD,
            "--no-video-title-show",
            "--no-one-instance",
        ]
        if path:
            args.append(str(Path(path).resolve()))

        self.launching = True
        try:
            creationflags = 0
            startupinfo = None
            if platform.system() == "Windows":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
        except Exception as e:
            self.launching = False
            self.log(f"Could not start VLC: {e}")
            return False

        deadline = time.time() + 12
        while time.time() < deadline:
            if self.status().get("connected"):
                self.launching = False
                self.log(f"VLC HTTP interface connected on port {self.port}.")
                if path:
                    self.media_path = Path(path).resolve()
                return True
            if self.process and self.process.poll() is not None:
                self.launching = False
                self.log(f"VLC exited while starting (code {self.process.returncode}).")
                return False
            time.sleep(0.2)
        self.launching = False
        self.log(f"VLC did not expose its HTTP interface on 127.0.0.1:{self.port}.")
        self.log("Try starting VLC once from its normal desktop shortcut, then click Open / Start VLC again.")
        return False


class SyncClient:
    def __init__(self, ui_log, ui_update):
        self.log = ui_log
        self.ui_update = ui_update
        self.ws = None
        self.ws_thread = None
        self.stop_event = threading.Event()
        self.connected = False
        self.connecting = False
        self.room = ""
        self.role = ""
        self.client_id = ""
        self.peer_connected = False

        self.clock_offset_ms = 0.0
        self.rtt_ms = 0.0
        self.last_clock = 0.0

        self.last_seq = 0
        self.shared = {
            "mediaKey": None,
            "mediaName": None,
            "position": 0.0,
            "playing": False,
            "rate": 1.0,
            "atServerMs": now_ms(),
            "seq": 0,
        }

        self.pending_commands = {}
        self.executed_ids = set()
        self.last_hard_seek = 0.0
        self.local_expected_until = 0.0
        self.local_expected_state = None
        self.last_rate_set = 1.0
        self.vlc = VLCController(ui_log)

        self.last_local = None
        self.last_user_action = 0.0

    def connect(self, url):
        if self.connected or self.connecting:
            return
        self.stop_event.clear()
        self.connecting = True
        self.ws_thread = threading.Thread(
            target=self._run_ws, args=(url,), daemon=True
        )
        self.ws_thread.start()

    def _run_ws(self, url):
        try:
            self.ws = websocket.create_connection(
                url,
                timeout=5,
                enable_multithread=True,
            )
            self.connected = True
            self.log("Connected to sync server.")
            self.send({"type": "hello"})
            self._send_clock_ping()

            while not self.stop_event.is_set():
                try:
                    raw = self.ws.recv()
                    if raw is None:
                        break
                    self.handle_message(json.loads(raw))
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception as e:
                    if not self.stop_event.is_set():
                        self.log(f"WebSocket error: {e}")
                    break
        except Exception as e:
            self.log(f"Connection failed: {e}")
        finally:
            self.connected = False
            self.connecting = False
            self.peer_connected = False
            self.ui_update()
            try:
                if self.ws:
                    self.ws.close()
            except Exception:
                pass
            self.ws = None

    def send(self, obj):
        if not self.ws or not self.connected:
            return False
        try:
            self.ws.send(json.dumps(obj, separators=(",", ":")))
            return True
        except Exception as e:
            self.log(f"Send failed: {e}")
            return False

    def _send_clock_ping(self):
        self.last_clock = time.time()
        self.send({
            "type": "clock_ping",
            "pingId": str(uuid.uuid4()),
            "clientWallMs": now_ms(),
        })

    def handle_message(self, m):
        t = m.get("type")
        if t == "clock_pong":
            sent = self.last_clock
            rtt = max(0.0, time.time() - sent)
            server_ms = float(m.get("serverMs", now_ms()))
            midpoint = now_ms() - (rtt * 1000 / 2)
            sample = server_ms - midpoint
            if self.rtt_ms == 0:
                self.rtt_ms = rtt * 1000
                self.clock_offset_ms = sample
            else:
                alpha = 0.20 if rtt * 1000 < self.rtt_ms * 1.5 else 0.08
                self.rtt_ms = (1-alpha) * self.rtt_ms + alpha * (rtt*1000)
                self.clock_offset_ms = (1-alpha) * self.clock_offset_ms + alpha * sample
            return

        if t in ("created", "joined"):
            self.room = m["room"]
            self.role = m["role"]
            self.client_id = m["clientId"]
            self.log(f"Room {self.room} joined as {self.role}.")
            self.ui_update()
            return

        if t == "peer":
            self.peer_connected = bool(m.get("connected"))
            self.ui_update()
            return

        if t == "media":
            self.shared["mediaKey"] = m.get("mediaKey")
            self.shared["mediaName"] = m.get("mediaName")
            return

        if t == "state":
            self._accept_state(m)
            return

        if t == "command":
            self._accept_command(m)
            return

        if t == "error":
            self.log(f"Server: {m.get('code')}: {m.get('message')}")
            return

    def _accept_state(self, m):
        seq = int(m.get("seq", 0))
        if seq < self.last_seq:
            return
        self.last_seq = seq
        self.shared.update({
            "mediaKey": m.get("mediaKey"),
            "mediaName": m.get("mediaName"),
            "position": float(m.get("position", 0) or 0),
            "playing": bool(m.get("playing")),
            "rate": float(m.get("rate", 1) or 1),
            "atServerMs": int(m.get("serverMs", now_ms())),
            "seq": seq,
        })
        self.ui_update()

    def _accept_command(self, m):
        seq = int(m.get("seq", 0))
        if seq <= self.last_seq or seq in self.executed_ids:
            return
        self.last_seq = seq
        self.shared.update({
            "position": float(m.get("position", self.shared["position"]) or 0),
            "playing": bool(m.get("playing", self.shared["playing"])),
            "rate": float(m.get("rate", self.shared["rate"]) or 1),
            "atServerMs": int(m.get("atServerMs", now_ms())),
            "seq": seq,
        })
        command_id = str(m.get("commandId", uuid.uuid4()))
        self.pending_commands[command_id] = m
        self._schedule_command(command_id, m)
        self.ui_update()

    def _schedule_command(self, command_id, m):
        target_local_ms = int(m.get("atServerMs", now_ms()) - self.clock_offset_ms)
        delay = max(0, target_local_ms - now_ms())
        self.ui_update()
        # Tk scheduling is handled by the main UI thread through callback.
        self.ui_update(schedule=(delay, command_id, m))

    def execute_command(self, command_id, m):
        if command_id in self.executed_ids:
            return
        self.executed_ids.add(command_id)
        self.pending_commands.pop(command_id, None)

        action = m.get("action")
        position = float(m.get("position", 0) or 0)
        rate = float(m.get("rate", 1) or 1)

        self.local_expected_until = time.time() + 1.2
        self.local_expected_state = "playing" if action in ("play", "restart") else ("paused" if action == "pause" else None)

        if action == "play":
            self.vlc.seek(position)
            self.vlc.rate(rate)
            self.vlc.play()
        elif action == "pause":
            self.vlc.seek(position)
            self.vlc.pause()
        elif action == "seek":
            self.vlc.seek(position)
        elif action == "restart":
            self.vlc.seek(0)
            self.vlc.rate(1)
            self.vlc.play()
        elif action == "rate":
            self.vlc.rate(rate)

    def create_room(self, room):
        self.send({"type":"create", "room":room, "name":platform.node()})

    def join_room(self, room):
        self.send({"type":"join", "room":room, "name":platform.node()})

    def leave(self):
        self.send({"type":"leave"})
        self.room = ""
        self.role = ""
        self.peer_connected = False
        self.ui_update()

    def disconnect(self):
        self.stop_event.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def send_action(self, action, position=None, rate=None):
        st = self.vlc.status()
        if not st.get("connected"):
            self.log("VLC is not connected.")
            return

        if position is None:
            position = st.get("time", 0.0)
        # The server schedules commands a few hundred milliseconds in the
        # future. For actions that preserve the current timeline, predict the
        # position at that shared execution time instead of seeking backwards.
        if action in ("play", "pause", "rate") and st.get("state") == "playing":
            position += (COMMAND_DELAY_MS / 1000.0) * float(st.get("rate", 1) or 1)

        payload = {
            "type":"command",
            "commandId":str(uuid.uuid4()),
            "action":action,
            "position":max(0.0, float(position)),
            "rate":float(rate if rate is not None else st.get("rate",1)),
            "mediaKey":media_key(self.vlc.media_path) if self.vlc.media_path else None,
            "mediaName":self.vlc.media_path.name if self.vlc.media_path else None,
        }
        self.last_user_action = time.time()
        self.send(payload)

    def play(self):
        self.send_action("play")

    def pause(self):
        self.send_action("pause")

    def restart(self):
        self.send_action("restart", position=0, rate=1)

    def seek(self, position):
        self.send_action("seek", position=max(0, position))

    def set_rate(self, rate):
        self.send_action("rate", rate=rate)

    def open_video(self, path):
        path = str(Path(path).resolve())
        def worker():
            if not self.vlc.start():
                self.ui_update(message="VLC could not be started. See Activity log.")
                return
            result = self.vlc.open_media(path)
            if result is None:
                self.log("VLC rejected the selected media.")
                self.ui_update(message="VLC could not open the selected video.")
                return
            deadline = time.time() + 8
            while time.time() < deadline:
                st = self.vlc.status()
                if st.get("connected") and (st.get("length", 0) > 0 or st.get("filename")):
                    break
                time.sleep(0.2)
            key = media_key(path)
            self.shared["mediaKey"] = key
            self.shared["mediaName"] = Path(path).name
            self.send({
                "type":"media",
                "mediaKey":key,
                "mediaName":Path(path).name,
            })
            self.log(f"Loaded: {Path(path).name}")
            self.ui_update(message=f"Loaded: {Path(path).name}")
        threading.Thread(target=worker, daemon=True).start()

    def tick(self):
        # periodic clock synchronization
        if self.connected and time.time() - self.last_clock > CLOCK_INTERVAL:
            self._send_clock_ping()

        st = self.vlc.status()
        if st.get("connected"):
            self._detect_native_vlc_action(st)
            if self.shared.get("seq", 0) > 0:
                self._smooth_sync(st)

        self.ui_update(status=st, sync_ms=self.current_sync_ms(st))

    def _detect_native_vlc_action(self, st):
        """Mirror direct VLC play/pause/seek actions without echoing remote commands."""
        t = time.time()
        current = float(st.get("time", 0) or 0)
        state = st.get("state")
        rate = float(st.get("rate", 1) or 1)

        if self.last_local is None:
            self.last_local = (t, current, state, rate)
            return

        lt, lp, ls, lr = self.last_local
        dt = max(0.0, t - lt)
        expected = lp + (dt * lr if ls == "playing" else 0.0)
        position_jump = abs(current - expected)
        state_changed = state != ls

        # Remote commands and our own correction operations are ignored during
        # the short settling window. After that, a human using VLC directly
        # can still control the shared session.
        if t >= self.local_expected_until:
            if state_changed:
                if state == "playing":
                    self.send_action("play", position=current)
                elif state == "paused":
                    self.send_action("pause", position=current)
            elif position_jump > 2.0:
                self.send_action("seek", position=current)

        self.last_local = (t, current, state, rate)

    def current_sync_ms(self, st):
        if not st.get("connected") or not self.shared.get("mediaKey"):
            return None
        target = self.predicted_shared_position()
        return (float(st.get("time",0)) - target) * 1000

    def predicted_shared_position(self):
        p = float(self.shared.get("position",0))
        if not self.shared.get("playing"):
            return p
        elapsed = (now_ms() - int(self.shared.get("atServerMs",now_ms()))) / 1000.0
        return max(0.0, p + elapsed * float(self.shared.get("rate",1) or 1))

    def _smooth_sync(self, st):
        if not self.peer_connected:
            return
        # Never correct immediately after executing an event.
        if time.time() < self.local_expected_until:
            return

        target = self.predicted_shared_position()
        local = float(st.get("time", 0) or 0)
        drift = target - local
        abs_drift = abs(drift)

        if abs_drift < SMALL_DRIFT:
            # Return rate to normal slowly, but don't issue a VLC command
            # unless meaningfully different.
            if abs(float(st.get("rate",1))-1.0) > 0.002:
                self.vlc.rate(1.0)
            return

        if abs_drift >= LARGE_DRIFT:
            if time.time() - self.last_hard_seek >= HARD_SEEK_COOLDOWN:
                self.last_hard_seek = time.time()
                self.local_expected_until = time.time() + 1.0
                self.log(f"Smooth sync: correcting {drift:+.2f}s")
                self.vlc.seek(target)
            return

        # Proportional rate correction. Max +/-3.5%, so playback remains smooth.
        correction = clamp(drift * 0.045, -MAX_RATE_ADJUST, MAX_RATE_ADJUST)
        desired = clamp(1.0 + correction, 0.95, 1.05)
        current = float(st.get("rate",1) or 1)
        if abs(current - desired) > 0.004:
            self.vlc.rate(desired)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Video Sync — VLC v{APP_VERSION}")
        self.root.geometry("760x720")
        self.root.minsize(680, 620)

        self.log_lines = []
        self.client = SyncClient(self.log, self.ui_update)
        self.last_status = {}
        self.slider_dragging = False
        self._schedule_keys = set()

        self._build()
        self.root.bind("<space>", lambda e: (self.client.pause() if self.last_status.get("state") == "playing" else self.client.play()))
        self.root.bind("<Left>", lambda e: self.nudge(-10))
        self.root.bind("<Right>", lambda e: self.nudge(10))
        self.root.bind("<r>", lambda e: self.client.restart())
        self.root.after(250, self.loop)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build(self):
        root = ttk.Frame(self.root, padding=18)
        root.pack(fill="both", expand=True)

        title = ttk.Label(root, text="VIDEO SYNC", font=("TkDefaultFont", 24, "bold"))
        title.pack(anchor="w")
        ttk.Label(root, text="Smooth bidirectional VLC synchronization",
                  font=("TkDefaultFont", 11)).pack(anchor="w", pady=(0,14))

        conn = ttk.LabelFrame(root, text="Connection", padding=10)
        conn.pack(fill="x", pady=5)
        self.server_var = tk.StringVar(value=DEFAULT_SERVER)
        ttk.Entry(conn, textvariable=self.server_var).pack(side="left", fill="x", expand=True)
        self.connect_btn = ttk.Button(conn, text="Connect", command=self.toggle_connection)
        self.connect_btn.pack(side="left", padx=(8,0))

        sess = ttk.LabelFrame(root, text="Session", padding=10)
        sess.pack(fill="x", pady=5)
        ttk.Label(sess, text="Room").grid(row=0,column=0,sticky="w")
        self.room_var = tk.StringVar(value="ROOM1")
        ttk.Entry(sess, textvariable=self.room_var, width=18).grid(row=0,column=1,padx=8)
        ttk.Button(sess,text="Create",command=self.create_room).grid(row=0,column=2,padx=3)
        ttk.Button(sess,text="Join",command=self.join_room).grid(row=0,column=3,padx=3)
        ttk.Button(sess,text="Leave",command=self.leave_room).grid(row=0,column=4,padx=3)
        self.session_label=ttk.Label(sess,text="Not in a room")
        self.session_label.grid(row=1,column=0,columnspan=5,sticky="w",pady=(8,0))

        vlc = ttk.LabelFrame(root, text="VLC", padding=10)
        vlc.pack(fill="x", pady=5)
        self.vlc_label=ttk.Label(vlc,text="VLC: checking...")
        self.vlc_label.pack(anchor="w")
        self.file_label=ttk.Label(vlc,text="No video loaded",wraplength=650)
        self.file_label.pack(anchor="w",pady=(3,8))

        row=ttk.Frame(vlc); row.pack(fill="x")
        ttk.Button(row,text="Open / Start VLC",command=self.start_vlc).pack(side="left",padx=(0,6))
        ttk.Button(row,text="Choose Video",command=self.choose_video).pack(side="left")
        self.sync_now_btn=ttk.Button(row,text="Sync Now",command=self.sync_now)
        self.sync_now_btn.pack(side="left",padx=6)

        controls=ttk.Frame(vlc); controls.pack(fill="x",pady=(10,0))
        ttk.Button(controls,text="▶  Play",command=self.client.play).pack(side="left",fill="x",expand=True,padx=2)
        ttk.Button(controls,text="Ⅱ  Pause",command=self.client.pause).pack(side="left",fill="x",expand=True,padx=2)
        ttk.Button(controls,text="↻  Restart",command=self.client.restart).pack(side="left",fill="x",expand=True,padx=2)

        seek=ttk.Frame(vlc); seek.pack(fill="x",pady=(10,0))
        ttk.Button(seek,text="−10s",command=lambda:self.nudge(-10)).pack(side="left")
        ttk.Button(seek,text="+10s",command=lambda:self.nudge(10)).pack(side="right")
        self.position_var=tk.DoubleVar(value=0)
        self.scale=ttk.Scale(seek,from_=0,to=100,variable=self.position_var,
                             command=self.slider_move)
        self.scale.pack(side="left",fill="x",expand=True,padx=8)
        self.scale.bind("<ButtonRelease-1>", self.slider_release)
        self.seek_label=ttk.Label(vlc,text="00:00 / 00:00")
        self.seek_label.pack(anchor="w",pady=(3,0))

        status=ttk.LabelFrame(root,text="Synchronization",padding=10)
        status.pack(fill="x",pady=5)
        self.playback_label=ttk.Label(status,text="00:00 / 00:00",font=("TkDefaultFont",18,"bold"))
        self.playback_label.pack(anchor="w")
        self.sync_label=ttk.Label(status,text="Sync: —")
        self.sync_label.pack(anchor="w")
        self.peer_label=ttk.Label(status,text="Peer: disconnected")
        self.peer_label.pack(anchor="w")
        self.clock_label=ttk.Label(status,text="Clock offset: —")
        self.clock_label.pack(anchor="w")
        self.mode_label=ttk.Label(status,text="Both computers can control playback")
        self.mode_label.pack(anchor="w",pady=(4,0))

        logf=ttk.LabelFrame(root,text="Activity",padding=6)
        logf.pack(fill="both",expand=True,pady=5)
        self.log_box=tk.Text(logf,height=7,wrap="word",state="disabled")
        self.log_box.pack(fill="both",expand=True)

    def log(self, text):
        stamp=time.strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {text}")
        self.log_lines=self.log_lines[-200:]
        try:
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0","end")
            self.log_box.insert("1.0","\n".join(self.log_lines))
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except Exception:
            pass

    def toggle_connection(self):
        if self.client.connected or self.client.connecting:
            self.client.disconnect()
            self.connect_btn.config(text="Connect", state="normal")
        else:
            url=self.server_var.get().strip()
            if not url.startswith(("ws://","wss://")):
                messagebox.showerror("Server","Enter a ws:// or wss:// address.")
                return
            self.client.connect(url)
            self.connect_btn.config(text="Connecting…", state="disabled")

    def create_room(self):
        room=self.room_var.get().strip()
        self.client.create_room(room)

    def join_room(self):
        room=self.room_var.get().strip()
        self.client.join_room(room)

    def leave_room(self):
        self.client.leave()

    def start_vlc(self):
        def worker():
            ok = self.client.vlc.start()
            if ok:
                self.client.log("VLC is ready.")
            else:
                self.client.ui_update(message="VLC could not be started. Check Activity log.")
        threading.Thread(target=worker, daemon=True).start()

    def choose_video(self):
        path=filedialog.askopenfilename(
            title="Choose video",
            filetypes=[
                ("Video files","*.mkv *.mp4 *.avi *.mov *.webm *.m4v *.ts"),
                ("All files","*.*")
            ]
        )
        if path:
            self.client.open_video(path)

    def nudge(self, delta):
        st=self.client.vlc.status()
        if st.get("connected"):
            self.client.seek(float(st.get("time",0))+delta)

    def slider_move(self, value):
        self.slider_dragging=True
        st=self.last_status
        if st:
            self.seek_label.config(text=f"{format_time(float(value))} / {format_time(st.get('length',0))}")

    def slider_release(self, _event=None):
        if self.slider_dragging:
            self.slider_dragging=False
            self.client.seek(self.position_var.get())

    def sync_now(self):
        # Treat current local position as an explicit shared seek.
        st=self.client.vlc.status()
        if st.get("connected"):
            self.client.seek(float(st.get("time",0)))
            self.log("Manual sync point sent.")

    def ui_update(self, **kwargs):
        # Called from worker/websocket threads; marshal all Tk changes to main thread.
        if "schedule" in kwargs:
            delay, command_id, m = kwargs["schedule"]
            self.root.after(max(1,int(delay)), lambda cid=command_id,msg=m:self.client.execute_command(cid,msg))
        if "message" in kwargs:
            msg = str(kwargs["message"])
            self.root.after(0, lambda text=msg: messagebox.showinfo("Video Sync", text))

    def loop(self):
        try:
            self.client.tick()
            st=self.client.vlc.status()
            self.last_status=st
            if st.get("connected"):
                self.vlc_label.config(text="VLC: Connected")
                name=Path(st.get("filename","")).name if st.get("filename") else (self.client.shared.get("mediaName") or "No video loaded")
                self.file_label.config(text=name)
                length=float(st.get("length",0) or 0)
                current=float(st.get("time",0) or 0)
                self.playback_label.config(text=f"{format_time(current)} / {format_time(length)}")
                if not self.slider_dragging:
                    self.position_var.set(current)
                    self.seek_label.config(text=f"{format_time(current)} / {format_time(length)}")
            else:
                self.vlc_label.config(text="VLC: Not connected")
            self.session_label.config(
                text=f"Room: {self.client.room or '—'}    Role: {self.client.role or '—'}"
            )
            self.peer_label.config(text=f"Peer: {'connected' if self.client.peer_connected else 'disconnected'}")
            self.clock_label.config(
                text=f"Clock offset: {self.client.clock_offset_ms:+.0f} ms   RTT: {self.client.rtt_ms:.0f} ms"
            )
            sync=self.client.current_sync_ms(st)
            self.sync_label.config(text="Sync: —" if sync is None else f"Sync: {sync:+.0f} ms")
            if self.client.connected:
                self.connect_btn.config(text="Disconnect", state="normal")
            elif self.client.connecting:
                self.connect_btn.config(text="Connecting…", state="disabled")
            else:
                self.connect_btn.config(text="Connect", state="normal")
        except Exception as e:
            self.log(f"UI error: {e}")
        self.root.after(POLL_MS,self.loop)

    def close(self):
        self.client.disconnect()
        self.root.destroy()


def main():
    root=tk.Tk()
    style=ttk.Style(root)
    try: style.theme_use("clam")
    except Exception: pass
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
