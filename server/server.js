const http = require("http");
const WebSocket = require("ws");

const PORT = Number(process.env.PORT || 8080);
const MAX_CLIENTS_PER_ROOM = 2;
const ROOM_TTL_MS = 6 * 60 * 60 * 1000;

const httpServer = http.createServer((req, res) => {
    if (req.url === "/" || req.url === "/health") {
        res.writeHead(200, {
            "Content-Type": "application/json",
            "Cache-Control": "no-store"
        });
        res.end(JSON.stringify({
            status: "ok",
            service: "video-sync-vlc-server",
            time: Date.now()
        }));
        return;
    }
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "Not found" }));
});

const wss = new WebSocket.Server({ server: httpServer });
const rooms = new Map();

function send(ws, message) {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
}

function broadcast(room, sender, message) {
    const entry = rooms.get(room);
    if (!entry) return;
    for (const client of entry.clients) {
        if (client !== sender && client.readyState === WebSocket.OPEN) send(client, message);
    }
}

function touchRoom(room) {
    const entry = rooms.get(room);
    if (entry) entry.lastActivity = Date.now();
}

function removeFromRoom(ws, notifyPeer = true) {
    if (!ws.room) return;
    const room = ws.room;
    const entry = rooms.get(room);
    if (!entry) {
        ws.room = null;
        ws.role = null;
        return;
    }
    entry.clients.delete(ws);
    touchRoom(room);
    if (notifyPeer) broadcast(room, ws, { type: "peer-left" });
    if (entry.clients.size === 0) {
        rooms.delete(room);
        console.log(`Room deleted: ${room}`);
    }
    ws.room = null;
    ws.role = null;
}

function validRoom(room) {
    return /^[A-Z0-9]{4,12}$/.test(room);
}

function finiteNumber(value, fallback = null) {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
}

wss.on("connection", (ws) => {
    console.log("Computer connected");
    ws.room = null;
    ws.role = null;
    ws.isAlive = true;

    ws.on("pong", () => { ws.isAlive = true; });

    send(ws, { type: "connected", serverTime: Date.now() });

    ws.on("message", (raw) => {
        let message;
        try {
            message = JSON.parse(raw.toString());
        } catch (error) {
            console.error("Invalid JSON from client:", error.message);
            send(ws, {
                type: "error",
                code: "INVALID_JSON",
                message: "Client sent invalid JSON."
            });
            return;
        }

        if (!message || typeof message !== "object" || Array.isArray(message)) {
            send(ws, {
                type: "error",
                code: "INVALID_MESSAGE",
                message: "Message must be a JSON object."
            });
            return;
        }

        const type = String(message.type || "");

        if (type === "clock-ping") {
            send(ws, {
                type: "clock-pong",
                clientTime: finiteNumber(message.clientTime, Date.now()),
                serverTime: Date.now()
            });
            return;
        }

        if (type === "create-room") {
            const room = String(message.room || "").trim().toUpperCase();
            if (!validRoom(room)) {
                send(ws, { type: "error", code: "INVALID_ROOM", message: "Room code must be 4-12 letters/numbers." });
                return;
            }
            if (ws.room) removeFromRoom(ws);
            if (rooms.has(room)) {
                send(ws, { type: "error", code: "ROOM_EXISTS", message: "Room already exists." });
                return;
            }
            const entry = { clients: new Set(), lastActivity: Date.now() };
            rooms.set(room, entry);
            ws.room = room;
            ws.role = "master";
            entry.clients.add(ws);
            send(ws, { type: "room-created", room, role: "master" });
            console.log(`Room created: ${room}`);
            return;
        }

        if (type === "join-room") {
            const room = String(message.room || "").trim().toUpperCase();
            const entry = rooms.get(room);
            if (!entry) {
                send(ws, { type: "error", code: "ROOM_NOT_FOUND", message: "Room does not exist." });
                return;
            }
            if (entry.clients.size >= MAX_CLIENTS_PER_ROOM) {
                send(ws, { type: "error", code: "ROOM_FULL", message: "Room is full." });
                return;
            }
            if (ws.room) removeFromRoom(ws);
            ws.room = room;
            ws.role = "client";
            entry.clients.add(ws);
            touchRoom(room);
            send(ws, { type: "room-joined", room, role: "client" });
            broadcast(room, ws, { type: "peer-joined" });
            console.log(`Computer joined room: ${room}`);
            return;
        }

        if (type === "leave-room") {
            removeFromRoom(ws, true);
            send(ws, { type: "session-left" });
            return;
        }

        if (type === "sync") {
            if (!ws.room) return;
            const entry = rooms.get(ws.room);
            if (!entry) return;

            const time = finiteNumber(message.time);
            const playbackRate = finiteNumber(message.playbackRate, 1);
            if (time === null || time < 0 || playbackRate === null) {
                send(ws, {
                    type: "error",
                    code: "INVALID_SYNC",
                    message: "Invalid playback state received."
                });
                return;
            }

            touchRoom(ws.room);
            broadcast(ws.room, ws, {
                type: "sync",
                action: String(message.action || "state"),
                time,
                playing: Boolean(message.playing),
                playbackRate: Math.max(0.05, Math.min(8, playbackRate)),
                sentAt: Date.now(),
                videoKey: String(message.videoKey || ""),
                sourceRole: ws.role
            });
            return;
        }

        send(ws, {
            type: "error",
            code: "UNKNOWN_MESSAGE",
            message: `Unknown message type: ${type || "(missing type)"}`
        });
    });

    ws.on("close", () => {
        console.log("Computer disconnected");
        removeFromRoom(ws, true);
    });

    ws.on("error", (error) => console.error("WebSocket error:", error.message));
});

const heartbeat = setInterval(() => {
    for (const ws of wss.clients) {
        if (ws.isAlive === false) {
            ws.terminate();
            continue;
        }
        ws.isAlive = false;
        ws.ping();
    }

    const now = Date.now();
    for (const [room, entry] of rooms.entries()) {
        if (entry.clients.size === 0 || now - entry.lastActivity > ROOM_TTL_MS) {
            for (const client of entry.clients) {
                try { client.close(1000, "Room expired"); } catch (_) {}
            }
            rooms.delete(room);
            console.log(`Expired room: ${room}`);
        }
    }
}, 30000);

wss.on("close", () => clearInterval(heartbeat));

httpServer.listen(PORT, "0.0.0.0", () => {
    console.log(`VLC Sync server listening on 0.0.0.0:${PORT}`);
});

function shutdown() {
    console.log("Shutting down...");
    for (const ws of wss.clients) ws.close(1001, "Server shutting down");
    wss.close(() => httpServer.close(() => process.exit(0)));
}

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
