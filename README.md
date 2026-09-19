# Video Sync — VLC Edition v1.1 (fixed)

This version fixes the `Invalid message` problem seen when the VLC agent sent
non-standard JSON numeric values such as `NaN` or `Infinity`.

## What changed

- VLC numeric status values are sanitized before sending.
- Python JSON serialization rejects NaN/Infinity instead of sending invalid JSON.
- Server validates playback numbers before relaying them.
- Server returns specific error codes instead of a generic invalid-message error.
- The agent logs server protocol errors instead of interrupting playback with a popup.
- VLC's HTTP control interface remains bound to `127.0.0.1`.

## Render

Use your deployed VLC server URL in the application, for example:

```text
wss://video-sync-vlc.onrender.com
```

The Render service should use:

```text
Root Directory: server
Build Command: npm ci
Start Command: npm start
Health Check: /health
```

Keep the `package-lock.json` already generated in your GitHub `server/`
directory when updating the deployed service.

## Windows

Replace the old `agent` directory with this one and run:

```text
run_windows.bat
```

## Linux

```bash
chmod +x run_linux.sh
./run_linux.sh
```
