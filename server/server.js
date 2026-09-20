const http = require("http");
const WebSocket = require("ws");
const crypto = require("crypto");

const PORT = Number(process.env.PORT || 10000);
const HOST = "0.0.0.0";
const MAX_CLIENTS = 2;
const ROOM_TTL_MS = 6 * 60 * 60 * 1000;

const rooms = new Map();

function now() { return Date.now(); }
function send(ws, obj) {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}
function finiteNumber(v) { return typeof v === "number" && Number.isFinite(v); }
function cleanRoom(s) {
  return typeof s === "string" && /^[A-Za-z0-9_-]{3,32}$/.test(s);
}
function cleanName(s) {
  return typeof s === "string" && s.length >= 1 && s.length <= 200;
}
function broadcast(room, obj) {
  for (const c of room.clients.values()) send(c.ws, obj);
}
function error(ws, code, message) { send(ws, {type:"error", code, message}); }

function getRoom(roomId) {
  return rooms.get(roomId);
}

function removeClient(ws) {
  const roomId = ws.roomId;
  if (!roomId) return;
  const room = rooms.get(roomId);
  if (!room) return;
  room.clients.delete(ws.clientId);
  ws.roomId = null;
  ws.clientId = null;
  broadcast(room, {type:"peer", connected: room.clients.size > 1});
  if (room.clients.size === 0) rooms.delete(roomId);
}

function makeRoom(id) {
  return {
    id,
    createdAt: now(),
    lastActivity: now(),
    seq: 0,
    clients: new Map(),
    state: {
      mediaKey: null,
      mediaName: null,
      position: 0,
      playing: false,
      rate: 1,
      atServerMs: now(),
      seq: 0
    }
  };
}

function applyCommand(room, cmd) {
  room.seq += 1;
  const seq = room.seq;
  const at = now() + 250;
  const base = {
    type: "command",
    seq,
    commandId: cmd.commandId,
    sourceId: cmd.sourceId,
    action: cmd.action,
    atServerMs: at
  };
  if (cmd.mediaKey) {
    room.state.mediaKey = cmd.mediaKey;
    room.state.mediaName = cmd.mediaName || null;
  }
  if (cmd.action === "play") {
    room.state.playing = true;
    room.state.rate = finiteNumber(cmd.rate) && cmd.rate > 0 ? cmd.rate : 1;
    room.state.position = Math.max(0, Number(cmd.position) || 0);
    room.state.atServerMs = at;
    room.state.seq = seq;
    base.position = room.state.position;
    base.playing = room.state.playing;
    base.rate = room.state.rate;
  } else if (cmd.action === "pause") {
    room.state.playing = false;
    room.state.position = Math.max(0, Number(cmd.position) || 0);
    room.state.atServerMs = at;
    room.state.seq = seq;
    base.position = room.state.position;
    base.playing = room.state.playing;
    base.rate = room.state.rate;
  } else if (cmd.action === "seek") {
    room.state.position = Math.max(0, Number(cmd.position) || 0);
    room.state.atServerMs = at;
    room.state.seq = seq;
    base.position = room.state.position;
    base.playing = room.state.playing;
    base.rate = room.state.rate;
  } else if (cmd.action === "restart") {
    room.state.position = 0;
    room.state.playing = true;
    room.state.rate = 1;
    room.state.atServerMs = at;
    room.state.seq = seq;
    base.position = 0;
    base.rate = 1;
  } else if (cmd.action === "rate") {
    room.state.rate = Math.min(4, Math.max(0.1, Number(cmd.rate) || 1));
    room.state.atServerMs = at;
    room.state.seq = seq;
    base.position = room.state.position;
    base.playing = room.state.playing;
    base.rate = room.state.rate;
  }
  room.lastActivity = now();
  broadcast(room, base);
}

const server = http.createServer((req, res) => {
  if (req.url === "/" || req.url === "/health") {
    const body = JSON.stringify({
      status: "ok",
      service: "video-sync-vlc-v3-server",
      rooms: rooms.size,
      time: now()
    });
    res.writeHead(200, {"Content-Type":"application/json", "Cache-Control":"no-store"});
    res.end(body);
    return;
  }
  res.writeHead(404);
  res.end("Not found");
});

const wss = new WebSocket.Server({server});
wss.on("connection", (ws) => {
  ws.clientId = crypto.randomUUID();
  ws.isAlive = true;

  ws.on("pong", () => { ws.isAlive = true; });

  ws.on("message", (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); }
    catch { return error(ws, "INVALID_JSON", "Invalid JSON"); }

    if (!msg || typeof msg.type !== "string") return error(ws, "INVALID_MESSAGE", "Invalid message");

    if (msg.type === "clock_ping") {
      send(ws, {type:"clock_pong", pingId:msg.pingId, serverMs:now()});
      return;
    }

    if (msg.type === "hello") {
      send(ws, {type:"hello", ok:true, serverMs:now()});
      return;
    }

    if (msg.type === "create") {
      if (!cleanRoom(msg.room)) return error(ws, "INVALID_ROOM", "Invalid room");
      if (rooms.has(msg.room)) return error(ws, "ROOM_EXISTS", "Room already exists");
      const room = makeRoom(msg.room);
      rooms.set(msg.room, room);
      room.clients.set(ws.clientId, {ws, name:msg.name || "Computer", role:"master"});
      ws.roomId = msg.room;
      send(ws, {type:"created", room:msg.room, role:"master", clientId:ws.clientId, serverMs:now()});
      send(ws, {type:"state", ...room.state, serverMs:now()});
      return;
    }

    if (msg.type === "join") {
      if (!cleanRoom(msg.room)) return error(ws, "INVALID_ROOM", "Invalid room");
      const room = getRoom(msg.room);
      if (!room) return error(ws, "ROOM_NOT_FOUND", "Room not found");
      if (room.clients.size >= MAX_CLIENTS) return error(ws, "ROOM_FULL", "Room is full");
      const role = room.clients.size === 0 ? "master" : "client";
      room.clients.set(ws.clientId, {ws, name:msg.name || "Computer", role});
      ws.roomId = msg.room;
      room.lastActivity = now();
      send(ws, {type:"joined", room:msg.room, role, clientId:ws.clientId, serverMs:now()});
      send(ws, {type:"state", ...room.state, serverMs:now()});
      broadcast(room, {type:"peer", connected: room.clients.size > 1});
      return;
    }

    if (msg.type === "leave") {
      removeClient(ws);
      return;
    }

    if (msg.type === "command") {
      if (!ws.roomId) return error(ws, "NOT_IN_ROOM", "Join a room first");
      const room = getRoom(ws.roomId);
      if (!room) return error(ws, "ROOM_NOT_FOUND", "Room not found");
      const allowed = new Set(["play","pause","seek","restart","rate"]);
      if (!allowed.has(msg.action)) return error(ws, "BAD_ACTION", "Unsupported action");
      if (msg.mediaKey !== undefined && !cleanName(msg.mediaKey)) return error(ws, "BAD_MEDIA", "Invalid media key");
      if (msg.position !== undefined && !finiteNumber(msg.position)) return error(ws, "BAD_POSITION", "Invalid position");
      if (msg.rate !== undefined && (!finiteNumber(msg.rate) || msg.rate <= 0)) return error(ws, "BAD_RATE", "Invalid rate");
      applyCommand(room, {
        commandId: typeof msg.commandId === "string" ? msg.commandId.slice(0,80) : crypto.randomUUID(),
        sourceId: ws.clientId,
        action: msg.action,
        position: msg.position,
        rate: msg.rate,
        mediaKey: msg.mediaKey,
        mediaName: msg.mediaName
      });
      return;
    }

    if (msg.type === "media") {
      if (!ws.roomId) return;
      const room = getRoom(ws.roomId);
      if (!room) return;
      if (!cleanName(msg.mediaKey)) return error(ws, "BAD_MEDIA", "Invalid media key");
      room.state.mediaKey = msg.mediaKey;
      room.state.mediaName = msg.mediaName || msg.mediaKey;
      room.lastActivity = now();
      broadcast(room, {type:"media", mediaKey:room.state.mediaKey, mediaName:room.state.mediaName});
      return;
    }

    error(ws, "UNKNOWN_MESSAGE", "Unknown message type");
  });

  ws.on("close", () => removeClient(ws));
});

setInterval(() => {
  for (const [id, room] of rooms) {
    if (now() - room.lastActivity > ROOM_TTL_MS || room.clients.size === 0) {
      for (const c of room.clients.values()) c.ws.close();
      rooms.delete(id);
    }
  }
}, 60_000);

setInterval(() => {
  for (const room of rooms.values()) {
    for (const c of room.clients.values()) {
      if (!c.ws.isAlive) { c.ws.terminate(); continue; }
      c.ws.isAlive = false;
      c.ws.ping();
    }
  }
}, 30_000);

server.listen(PORT, HOST, () => {
  console.log(`Video Sync VLC v3.4 server listening on ${HOST}:${PORT}`);
});
