"""Dark Colony X - WebSocket Relay Server

Minimal relay server for multiplayer: room management, message relay, heartbeat,
and WebRTC signaling relay.
Binary WebSocket protocol — no JSON overhead.

Usage:
    python tools/relay_server.py [--port PORT] [--verbose]
    python tools/relay_server.py [--port 443] [--cert CERT] [--key KEY] [--verbose]

Protocol:
    Room management (0xA1-0xA8):
        0xA1  C->S  CREATE_ROOM    [name_len:1][name:N][max_players:1][pwd_len:1][pwd:M]
        0xA2  S->C  ROOM_CREATED   [room_code:6]
        0xA3  C->S  JOIN_ROOM      [room_code:6][pwd_len:1][pwd:M]
        0xA4  S->C  JOIN_RESPONSE  [player_id:1][player_count:1]
        0xA5  S->C  PLAYER_JOINED  [player_id:1]
        0xA6  S->C  PLAYER_LEFT    [player_id:1]
        0xA7  C->S  LIST_ROOMS     (no payload)
        0xA8  S->C  ROOM_LIST      [count:1][entries...] (entry: [code:6][name_len:1][name:N][cur:1][max:1][has_pwd:1])
        0xA9  C->S  LEAVE_ROOM     (no payload) — leave room, keep WebSocket alive
        0xAA  C->S  LOCK_ROOM      (no payload) — host marks room as in-game (hidden from list, no new joins)

    Game data relay (0x10-0x12):
        0x10  C->S  SEND_TO        [to_id:1][data:N]
        0x11  C->S  BROADCAST      [data:N]
        0x12  S->C  GAME_DATA      [from_id:1][data:N]

    WebRTC signaling relay (0xB1-0xB7):
        0xB1  C->S  SDP_OFFER      [to_id:1][sdp_len:2 LE][sdp:N]
        0xB2  C->S  SDP_ANSWER     [to_id:1][sdp_len:2 LE][sdp:N]
        0xB3  C->S  ICE_CANDIDATE  [to_id:1][cand_len:2 LE][candidate:N]
        0xB4  S->C  SDP_OFFER      [from_id:1][sdp_len:2 LE][sdp:N]
        0xB5  S->C  SDP_ANSWER     [from_id:1][sdp_len:2 LE][sdp:N]
        0xB6  S->C  ICE_CANDIDATE  [from_id:1][cand_len:2 LE][candidate:N]
        0xB7  C->S  P2P_FAILED     [to_id:1]  (forwarded to target as [from_id:1])

    Connection management (0xF0-0xFF):
        0xF0  both  PING
        0xF1  both  PONG
        0xF8  C->S  HELLO          [proto_version:1][capabilities:1]
        0xF9  S->C  HELLO_ACK      [negotiated_v:1][server_caps:1][result:1]
        0xFE  S->C  SERVER_ERROR   [reason:1]  (0=full, 1=not_found, 2=timeout, 3=bad_password, 4=rate_limited, 5=ip_blocked)
        0xFF  both  DISCONNECT     [reason:1]  (0=normal, 1=kicked, 2=timeout)
"""

import asyncio
import json
import logging
import secrets
import ssl
import string
import sys
import time
from http import HTTPStatus

try:
    import websockets
    from websockets.asyncio.server import serve
except ImportError:
    print("ERROR: 'websockets' package required. Install with: pip install websockets")
    sys.exit(1)

logger = logging.getLogger("relay")

# Protocol message types
MSG_SEND_TO       = 0x10
MSG_BROADCAST     = 0x11
MSG_GAME_DATA     = 0x12
MSG_CREATE_ROOM   = 0xA1
MSG_ROOM_CREATED  = 0xA2
MSG_JOIN_ROOM     = 0xA3
MSG_JOIN_RESPONSE = 0xA4
MSG_PLAYER_JOINED = 0xA5
MSG_PLAYER_LEFT   = 0xA6
MSG_LIST_ROOMS    = 0xA7
MSG_ROOM_LIST     = 0xA8
MSG_LEAVE_ROOM    = 0xA9
MSG_LOCK_ROOM     = 0xAA
MSG_PING          = 0xF0
MSG_PONG          = 0xF1
MSG_HELLO         = 0xF8
MSG_HELLO_ACK     = 0xF9
MSG_SERVER_ERROR  = 0xFE
MSG_DISCONNECT    = 0xFF

# WebRTC signaling relay (C->S)
MSG_SDP_OFFER_C2S     = 0xB1
MSG_SDP_ANSWER_C2S    = 0xB2
MSG_ICE_CANDIDATE_C2S = 0xB3
# WebRTC signaling relay (S->C)
MSG_SDP_OFFER_S2C     = 0xB4
MSG_SDP_ANSWER_S2C    = 0xB5
MSG_ICE_CANDIDATE_S2C = 0xB6
# P2P failure notification (C->S, forwarded to target)
MSG_P2P_FAILED        = 0xB7

# C2S -> S2C message code mapping for signaling relay
_SIGNALING_C2S_TO_S2C = {
    MSG_SDP_OFFER_C2S:     MSG_SDP_OFFER_S2C,
    MSG_SDP_ANSWER_C2S:    MSG_SDP_ANSWER_S2C,
    MSG_ICE_CANDIDATE_C2S: MSG_ICE_CANDIDATE_S2C,
}

KNOWN_MSG_TYPES = {
    MSG_SEND_TO, MSG_BROADCAST, MSG_CREATE_ROOM, MSG_JOIN_ROOM,
    MSG_LIST_ROOMS, MSG_LEAVE_ROOM, MSG_LOCK_ROOM, MSG_PING, MSG_PONG, MSG_HELLO, MSG_DISCONNECT,
    MSG_SDP_OFFER_C2S, MSG_SDP_ANSWER_C2S, MSG_ICE_CANDIDATE_C2S,
    MSG_P2P_FAILED,
}

# Protocol version negotiation
SERVER_PROTO_VERSION = 2   # v2: WebRTC signaling support
SERVER_MIN_VERSION   = 1   # minimum client version accepted
SERVER_CAPABILITIES  = 0x03  # 0x01 = password, 0x02 = webrtc signaling

# Error reasons
ERR_FULL         = 0
ERR_NOT_FOUND    = 1
ERR_TIMEOUT      = 2
ERR_BAD_PASSWORD = 3
ERR_RATE_LIMITED = 4
ERR_IP_BLOCKED   = 5

# Disconnect reasons
DC_NORMAL  = 0
DC_KICKED  = 1
DC_TIMEOUT = 2

# Server limits
HEARTBEAT_TIMEOUT = 45.0
ROOM_CODE_LENGTH = 6
ROOM_CODE_CHARS = string.ascii_uppercase + string.digits
MAX_ROOMS = 1000
MAX_ROOM_NAME_LEN = 64
MAX_GAME_DATA_SIZE = 2048
RATE_LIMIT_WINDOW = 1.0   # seconds
RATE_LIMIT_MAX = 500       # messages per window (8-player HOST sends ~210/sec)
MAX_CONNECTIONS_PER_IP = 4  # same IP max simultaneous connections (covers 8-player LAN party)
MAX_JOIN_FAILURES = 10      # join failures before temporary block
JOIN_FAILURE_WINDOW = 60.0  # seconds
ROOM_IDLE_TIMEOUT = 1800.0  # 30 minutes
MAX_SIGNALING_SIZE = 16384  # 16KB max SDP/ICE candidate size


class Player:
    __slots__ = ("ws", "player_id", "room_code", "last_activity",
                 "msg_count", "msg_window_start",
                 "proto_version", "capabilities")

    def __init__(self, ws):
        self.ws = ws
        self.player_id = 0
        self.room_code = None
        self.last_activity = time.monotonic()
        self.msg_count = 0
        self.msg_window_start = time.monotonic()
        self.proto_version = 1     # default: v1 (no HELLO = legacy client)
        self.capabilities = 0x00   # default: no capabilities


class Room:
    __slots__ = ("code", "name", "max_players", "players", "host_ws", "last_activity", "password", "in_game")

    def __init__(self, code, name, max_players, host_ws, password=""):
        self.code = code
        self.name = name
        self.max_players = max_players
        self.players = {}   # player_id -> Player
        self.host_ws = host_ws
        self.last_activity = time.monotonic()
        self.password = password  # empty string = public room
        self.in_game = False      # set True by LOCK_ROOM when game starts


class RelayServer:
    def __init__(self, verbose=False):
        self.rooms = {}            # room_code -> Room
        self.players = {}          # websocket -> Player
        self.verbose = verbose
        self.ip_connections = {}   # ip -> count
        self.join_failures = {}    # ip -> (count, first_failure_time)
        self._start_time = time.monotonic()

    def _generate_room_code(self):
        """Generate a unique 6-character room code using CSPRNG."""
        for _ in range(1000):
            code = "".join(secrets.choice(ROOM_CODE_CHARS) for _ in range(ROOM_CODE_LENGTH))
            if code not in self.rooms:
                return code
        return None

    def _next_player_id(self, room):
        """Find next available player ID (1-8) in room."""
        used = set(room.players.keys())
        for pid in range(1, room.max_players + 1):
            if pid not in used:
                return pid
        return None

    async def _send(self, ws, data):
        """Send binary data to a websocket, with error logging."""
        try:
            await ws.send(data)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Send failed: connection already closed")
        except Exception as e:
            logger.warning(f"Send failed: {e}")

    def _log_msg(self, direction, ws, msg_type, payload_len):
        """Log message if verbose mode enabled."""
        if not self.verbose:
            return
        player = self.players.get(ws)
        pid = player.player_id if player else "?"
        room = player.room_code if player else "?"
        type_names = {
            0x10: "SEND_TO", 0x11: "BROADCAST", 0x12: "GAME_DATA",
            0xA1: "CREATE_ROOM", 0xA2: "ROOM_CREATED", 0xA3: "JOIN_ROOM",
            0xA4: "JOIN_RESPONSE", 0xA5: "PLAYER_JOINED", 0xA6: "PLAYER_LEFT",
            0xA7: "LIST_ROOMS", 0xA8: "ROOM_LIST", 0xAA: "LOCK_ROOM",
            0xB1: "SDP_OFFER", 0xB2: "SDP_ANSWER", 0xB3: "ICE_CANDIDATE",
            0xB4: "SDP_OFFER>", 0xB5: "SDP_ANSWER>", 0xB6: "ICE_CANDIDATE>",
            0xB7: "P2P_FAILED",
            0xF0: "PING", 0xF1: "PONG", 0xFE: "SERVER_ERROR", 0xFF: "DISCONNECT",
        }
        name = type_names.get(msg_type, f"0x{msg_type:02X}")
        logger.info(f"  {direction} P{pid} R={room} {name} ({payload_len}B)")

    def _check_rate_limit(self, player):
        """Returns True if player exceeds rate limit."""
        now = time.monotonic()
        if now - player.msg_window_start > RATE_LIMIT_WINDOW:
            player.msg_count = 0
            player.msg_window_start = now
        player.msg_count += 1
        return player.msg_count > RATE_LIMIT_MAX

    async def handle_message(self, ws, data):
        """Process a single binary message from a client."""
        if len(data) < 1:
            return

        player = self.players.get(ws)
        if not player:
            return

        player.last_activity = time.monotonic()

        # Rate limiting
        if self._check_rate_limit(player):
            logger.warning(f"Rate limit exceeded: {ws.remote_address}")
            await self._send(ws, bytes([MSG_DISCONNECT, DC_TIMEOUT]))
            await self._remove_player(ws, DC_TIMEOUT)
            try:
                await ws.close()
            except Exception:
                pass
            return

        msg_type = data[0]
        payload = data[1:]

        self._log_msg("<<", ws, msg_type, len(payload))

        if msg_type == MSG_PING:
            await self._send(ws, bytes([MSG_PONG]))

        elif msg_type == MSG_PONG:
            pass  # activity timestamp already updated

        elif msg_type == MSG_CREATE_ROOM:
            await self._handle_create_room(ws, payload)

        elif msg_type == MSG_JOIN_ROOM:
            await self._handle_join_room(ws, payload)

        elif msg_type == MSG_LIST_ROOMS:
            await self._handle_list_rooms(ws)

        elif msg_type == MSG_BROADCAST:
            await self._handle_broadcast(ws, payload)

        elif msg_type == MSG_SEND_TO:
            await self._handle_send_to(ws, payload)

        elif msg_type == MSG_HELLO:
            await self._handle_hello(ws, payload)

        elif msg_type in _SIGNALING_C2S_TO_S2C:
            await self._handle_signaling_forward(ws, msg_type, payload)

        elif msg_type == MSG_P2P_FAILED:
            await self._handle_p2p_failed(ws, payload)

        elif msg_type == MSG_LEAVE_ROOM:
            await self._handle_leave_room(ws)

        elif msg_type == MSG_LOCK_ROOM:
            await self._handle_lock_room(ws)

        elif msg_type == MSG_DISCONNECT:
            reason = payload[0] if len(payload) > 0 else DC_NORMAL
            await self._handle_disconnect(ws, reason)

        else:
            logger.debug(f"Unknown message type 0x{msg_type:02X} from {ws.remote_address}")

    async def _handle_hello(self, ws, payload):
        """Handle HELLO (0xF8) — negotiate protocol version and capabilities."""
        player = self.players.get(ws)
        if not player or len(payload) < 2:
            return
        client_version = payload[0]
        client_caps = payload[1]
        negotiated_version = min(client_version, SERVER_PROTO_VERSION)
        negotiated_caps = client_caps & SERVER_CAPABILITIES
        result = 1 if client_version < SERVER_MIN_VERSION else 0
        player.proto_version = negotiated_version
        player.capabilities = negotiated_caps
        await self._send(ws, bytes([MSG_HELLO_ACK, negotiated_version, negotiated_caps, result]))
        logger.info(f"HELLO from {ws.remote_address}: v{client_version} caps=0x{client_caps:02X} "
                     f"-> negotiated v{negotiated_version} caps=0x{negotiated_caps:02X} result={result}")

    async def _leave_current_room(self, ws):
        """Remove player from their current room (if any) before joining another."""
        player = self.players.get(ws)
        if not player or not player.room_code:
            return
        room = self.rooms.get(player.room_code)
        if room:
            room.players.pop(player.player_id, None)
            notify = bytes([MSG_PLAYER_LEFT, player.player_id])
            for pid, other in list(room.players.items()):
                await self._send(other.ws, notify)
            if not room.players:
                del self.rooms[player.room_code]
                logger.info(f"Room {player.room_code} closed (empty)")
        player.room_code = None
        player.player_id = 0

    async def _handle_create_room(self, ws, payload):
        """Handle room creation request."""
        if len(payload) < 2:
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_NOT_FOUND]))
            return

        name_len = payload[0]
        if name_len < 1 or name_len > MAX_ROOM_NAME_LEN:
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_NOT_FOUND]))
            return

        if len(payload) < 1 + name_len + 1:
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_NOT_FOUND]))
            return

        # Server room limit
        if len(self.rooms) >= MAX_ROOMS:
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_FULL]))
            return

        # Leave previous room if any
        await self._leave_current_room(ws)

        try:
            name = payload[1:1 + name_len].decode("utf-8", "strict")
        except UnicodeDecodeError:
            name = payload[1:1 + name_len].decode("utf-8", "replace")
        # Sanitize for logging
        name = name.replace("\n", " ").replace("\r", " ")[:MAX_ROOM_NAME_LEN]

        max_players = payload[1 + name_len]
        if max_players < 2:
            max_players = 2
        if max_players > 8:
            max_players = 8

        # Parse optional password: [pwd_len:1][pwd:M] after max_players
        pwd = ""
        pw_offset = 1 + name_len + 1
        if pw_offset < len(payload):
            pwd_len = payload[pw_offset]
            if pwd_len > 0 and pw_offset + 1 + pwd_len <= len(payload):
                pwd = payload[pw_offset + 1 : pw_offset + 1 + pwd_len].decode("utf-8", "replace")[:31]

        code = self._generate_room_code()
        if not code:
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_FULL]))
            return

        room = Room(code, name, max_players, ws, password=pwd)
        self.rooms[code] = room

        player = self.players.get(ws)
        if player:
            player.player_id = 1
            player.room_code = code
            room.players[1] = player

        # Send room code back
        response = bytes([MSG_ROOM_CREATED]) + code.encode("ascii")
        await self._send(ws, response)
        self._log_msg(">>", ws, MSG_ROOM_CREATED, len(code))
        logger.info(f"Room created: {code} '{name}' (max {max_players}) [{len(self.rooms)}/{MAX_ROOMS}]")

    async def _handle_join_room(self, ws, payload):
        """Handle room join request."""
        if len(payload) < ROOM_CODE_LENGTH:
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_NOT_FOUND]))
            return

        # Validate room code format (alphanumeric only)
        raw = payload[:ROOM_CODE_LENGTH]
        if not all(0x30 <= b <= 0x39 or 0x41 <= b <= 0x5A for b in raw):
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_NOT_FOUND]))
            return
        code = raw.decode("ascii")

        # Brute-force protection: check join failure count for this IP
        remote = ws.remote_address
        ip = remote[0] if remote else "unknown"
        now = time.monotonic()
        fail_count, fail_start = self.join_failures.get(ip, (0, 0.0))
        if now - fail_start > JOIN_FAILURE_WINDOW:
            fail_count, fail_start = 0, now
        if fail_count >= MAX_JOIN_FAILURES:
            logger.warning(f"Join brute-force blocked: {ip} ({fail_count} failures)")
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_RATE_LIMITED]))
            return

        # Leave previous room if any
        await self._leave_current_room(ws)

        room = self.rooms.get(code)
        if not room or room.in_game:
            self.join_failures[ip] = (fail_count + 1, fail_start or now)
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_NOT_FOUND]))
            return

        # Password verification: [room_code:6][pwd_len:1][pwd:M]
        if room.password:
            pwd = ""
            pwd_offset = ROOM_CODE_LENGTH
            if pwd_offset < len(payload):
                pwd_len = payload[pwd_offset]
                if pwd_len > 0 and pwd_offset + 1 + pwd_len <= len(payload):
                    pwd = payload[pwd_offset + 1 : pwd_offset + 1 + pwd_len].decode("utf-8", "replace")
            if pwd != room.password:
                self.join_failures[ip] = (fail_count + 1, fail_start or now)
                await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_BAD_PASSWORD]))
                return

        if len(room.players) >= room.max_players:
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_FULL]))
            return

        pid = self._next_player_id(room)
        if pid is None:
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_FULL]))
            return

        # Re-check room still exists (race condition guard)
        if code not in self.rooms:
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_NOT_FOUND]))
            return

        player = self.players.get(ws)
        if player:
            player.player_id = pid
            player.room_code = code
            room.players[pid] = player

        # Join success — clear failure counter and update room activity
        self.join_failures.pop(ip, None)
        room.last_activity = time.monotonic()

        # Send join response to joiner
        response = bytes([MSG_JOIN_RESPONSE, pid, len(room.players)])
        await self._send(ws, response)
        self._log_msg(">>", ws, MSG_JOIN_RESPONSE, 2)

        # Notify existing players
        notify = bytes([MSG_PLAYER_JOINED, pid])
        for other_pid, other_player in room.players.items():
            if other_pid != pid:
                await self._send(other_player.ws, notify)
                self._log_msg(">>", other_player.ws, MSG_PLAYER_JOINED, 1)

        logger.info(f"Player {pid} joined room {code} ({len(room.players)}/{room.max_players})")

    async def _handle_list_rooms(self, ws):
        """Handle room list request. Response capped at 64KB."""
        MAX_LIST_SIZE = 65536
        entries = []
        total_size = 2  # msg_type + count byte
        for code, room in self.rooms.items():
            if room.in_game:
                continue  # hide rooms where game has started
            name_bytes = room.name.encode("utf-8")[:255]
            entry = code.encode("ascii") + bytes([len(name_bytes)]) + name_bytes
            entry += bytes([len(room.players), room.max_players, 1 if room.password else 0])
            if total_size + len(entry) > MAX_LIST_SIZE:
                break
            entries.append(entry)
            total_size += len(entry)
            if len(entries) >= 255:  # count field is 1 byte
                break

        response = bytes([MSG_ROOM_LIST, len(entries)])
        for entry in entries:
            response += entry
        await self._send(ws, response)
        self._log_msg(">>", ws, MSG_ROOM_LIST, len(response) - 1)

    async def _handle_broadcast(self, ws, payload):
        """Relay broadcast message to all players in room (sender excluded).

        Matches original DirectPlay DPID_ALLPLAYERS behavior:
        Wine DP_QueuePlayerMessage skips excludeId == sender.
        HOST never sends BROADCAST (writes to local mailbox).
        CLIENT sends BROADCAST; relay delivers to HOST only.
        """
        player = self.players.get(ws)
        if not player or not player.room_code:
            return

        room = self.rooms.get(player.room_code)
        if not room:
            return

        # Reject oversized payloads
        if len(payload) > MAX_GAME_DATA_SIZE:
            logger.warning(f"Broadcast oversized ({len(payload)}B), dropping")
            return

        room.last_activity = time.monotonic()

        # Wrap: [0x12][from_id][original_payload]
        relay_msg = bytes([MSG_GAME_DATA, player.player_id]) + payload

        for pid, other in room.players.items():
            if pid == player.player_id:
                continue  # Exclude sender (RE DirectPlay DPID_ALLPLAYERS)
            await self._send(other.ws, relay_msg)
            self._log_msg(">>", other.ws, MSG_GAME_DATA, len(relay_msg) - 1)

    async def _handle_send_to(self, ws, payload):
        """Relay targeted message to a specific player."""
        if len(payload) < 2:
            return

        player = self.players.get(ws)
        if not player or not player.room_code:
            return

        room = self.rooms.get(player.room_code)
        if not room:
            return

        to_id = payload[0]
        if to_id < 1 or to_id > room.max_players:
            return

        data = payload[1:]
        if len(data) > MAX_GAME_DATA_SIZE:
            logger.warning(f"SendTo oversized ({len(data)}B), dropping")
            return

        room.last_activity = time.monotonic()

        target = room.players.get(to_id)
        if target and target.ws != ws:  # Don't send to self
            relay_msg = bytes([MSG_GAME_DATA, player.player_id]) + data
            await self._send(target.ws, relay_msg)
            self._log_msg(">>", target.ws, MSG_GAME_DATA, len(relay_msg) - 1)

    async def _handle_signaling_forward(self, ws, msg_type, payload):
        """Forward WebRTC signaling message (SDP offer/answer, ICE candidate) to target peer.

        C->S: [to_id:1][len:2 LE][data:N]
        S->C: [from_id:1][len:2 LE][data:N]  (msg_type remapped C2S -> S2C)
        """
        if len(payload) < 3:  # to_id(1) + len(2) minimum
            return

        player = self.players.get(ws)
        if not player or not player.room_code:
            return

        room = self.rooms.get(player.room_code)
        if not room:
            return

        to_id = payload[0]
        # Validate payload size field
        data_len = int.from_bytes(payload[1:3], "little")
        if len(payload) < 3 + data_len:
            logger.warning(f"Signaling message truncated: declared {data_len}B, got {len(payload) - 3}B")
            return

        if data_len > MAX_SIGNALING_SIZE:
            logger.warning(f"Signaling message oversized ({data_len}B), dropping")
            return

        target = room.players.get(to_id)
        if not target or target.ws == ws:
            return

        s2c_type = _SIGNALING_C2S_TO_S2C[msg_type]
        # Replace to_id with from_id, keep rest of payload intact
        forward_msg = bytes([s2c_type, player.player_id]) + payload[1:]
        await self._send(target.ws, forward_msg)
        self._log_msg(">>", target.ws, s2c_type, len(forward_msg) - 1)

    async def _handle_p2p_failed(self, ws, payload):
        """Forward P2P failure notification to target peer.

        C->S: [to_id:1]
        S->C: [from_id:1]  (same msg_type 0xB7)
        """
        if len(payload) < 1:
            return

        player = self.players.get(ws)
        if not player or not player.room_code:
            return

        room = self.rooms.get(player.room_code)
        if not room:
            return

        to_id = payload[0]
        target = room.players.get(to_id)
        if not target or target.ws == ws:
            return

        forward_msg = bytes([MSG_P2P_FAILED, player.player_id])
        await self._send(target.ws, forward_msg)
        self._log_msg(">>", target.ws, MSG_P2P_FAILED, 1)
        logger.info(f"P2P failed: P{player.player_id} -> P{to_id} in room {player.room_code}")

    async def _handle_leave_room(self, ws):
        """Handle LEAVE_ROOM — remove player from room but keep WebSocket alive."""
        player = self.players.get(ws)
        if not player or not player.room_code:
            return

        room = self.rooms.get(player.room_code)
        if room:
            is_host = (room.host_ws == ws)
            room.players.pop(player.player_id, None)
            logger.info(f"Player {player.player_id} left room {player.room_code} (leave, host={is_host})")

            if is_host:
                # Host left — evict all remaining players and close room
                notify = bytes([MSG_PLAYER_LEFT, player.player_id])
                for pid, other in list(room.players.items()):
                    await self._send(other.ws, notify)
                    # Reset each remaining player's room state
                    other.room_code = None
                    other.player_id = 0
                room.players.clear()
                self.rooms.pop(player.room_code, None)
                logger.info(f"Room {player.room_code} closed (host left)")
            else:
                # Notify remaining players
                notify = bytes([MSG_PLAYER_LEFT, player.player_id])
                for pid, other in list(room.players.items()):
                    await self._send(other.ws, notify)

                # Remove empty rooms
                if not room.players:
                    self.rooms.pop(player.room_code, None)
                    logger.info(f"Room {player.room_code} closed (empty)")

        # Reset player state so they can create/join another room
        player.room_code = None
        player.player_id = 0

    async def _handle_lock_room(self, ws):
        """Handle LOCK_ROOM — host marks room as in-game, hidden from room list."""
        player = self.players.get(ws)
        if not player or not player.room_code:
            return
        room = self.rooms.get(player.room_code)
        if not room:
            return
        if room.host_ws != ws:
            return  # only host can lock
        room.in_game = True
        logger.info(f"Room {player.room_code} locked (game started)")

    async def _handle_disconnect(self, ws, reason):
        """Handle explicit disconnect."""
        await self._remove_player(ws, reason)

    async def _remove_player(self, ws, reason=DC_NORMAL):
        """Remove a player from their room and notify others."""
        player = self.players.pop(ws, None)
        if not player:
            return

        if player.room_code:
            room = self.rooms.get(player.room_code)
            if room:
                room.players.pop(player.player_id, None)
                logger.info(f"Player {player.player_id} left room {player.room_code} (reason={reason})")

                # Notify remaining players (copy list to avoid mutation during iteration)
                notify = bytes([MSG_PLAYER_LEFT, player.player_id])
                for pid, other in list(room.players.items()):
                    await self._send(other.ws, notify)

                # Remove empty rooms
                if not room.players:
                    self.rooms.pop(player.room_code, None)
                    logger.info(f"Room {player.room_code} closed (empty)")

    def _process_request(self, connection, request):
        """Intercept HTTP GET /status before WebSocket handshake."""
        if request.path == "/status":
            features = ["password", "webrtc_signaling"]
            if self._ssl_enabled:
                features.insert(0, "wss")
            status_data = {
                "version": "1.0",
                "uptime_seconds": int(time.monotonic() - self._start_time),
                "rooms": len(self.rooms),
                "players": len(self.players),
                "max_rooms": MAX_ROOMS,
                "features": features,
            }
            body = json.dumps(status_data)
            response = connection.protocol.reject(HTTPStatus.OK, body)
            response.headers["Content-Type"] = "application/json; charset=utf-8"
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response
        return None  # proceed with normal WebSocket handshake

    async def handle_connection(self, ws):
        """Handle a new WebSocket connection."""
        remote = ws.remote_address
        ip = remote[0] if remote else "unknown"

        # IP-based connection limit
        current = self.ip_connections.get(ip, 0)
        if current >= MAX_CONNECTIONS_PER_IP:
            logger.warning(f"IP connection limit reached: {ip} ({current}/{MAX_CONNECTIONS_PER_IP})")
            await self._send(ws, bytes([MSG_SERVER_ERROR, ERR_IP_BLOCKED]))
            await ws.close()
            return

        self.ip_connections[ip] = current + 1
        player = Player(ws)
        self.players[ws] = player
        logger.info(f"Client connected: {remote}")

        try:
            async for message in ws:
                if isinstance(message, bytes):
                    await self.handle_message(ws, message)
                # Ignore text messages
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._remove_player(ws)
            self.ip_connections[ip] = max(0, self.ip_connections.get(ip, 1) - 1)
            logger.info(f"Client disconnected: {remote}")

    async def heartbeat_checker(self):
        """Periodically check for timed-out connections and idle rooms."""
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()

            # Player heartbeat timeout
            timed_out = []
            for ws, player in list(self.players.items()):
                if now - player.last_activity > HEARTBEAT_TIMEOUT:
                    timed_out.append(ws)

            for ws in timed_out:
                logger.info("Heartbeat timeout")
                try:
                    await self._send(ws, bytes([MSG_DISCONNECT, DC_TIMEOUT]))
                except Exception:
                    pass
                await self._remove_player(ws, DC_TIMEOUT)
                try:
                    await ws.close()
                except Exception:
                    pass

            # Idle room cleanup (30 min no activity)
            idle_rooms = [code for code, room in self.rooms.items()
                          if now - room.last_activity > ROOM_IDLE_TIMEOUT]
            for code in idle_rooms:
                room = self.rooms.pop(code, None)
                if room:
                    for pid, player in list(room.players.items()):
                        try:
                            await self._send(player.ws, bytes([MSG_DISCONNECT, DC_TIMEOUT]))
                        except Exception:
                            pass
                    logger.info(f"Room {code} closed (idle {ROOM_IDLE_TIMEOUT/60:.0f}min timeout)")

            # Clean up stale join failure entries
            stale_ips = [ip for ip, (count, start) in self.join_failures.items()
                         if now - start > JOIN_FAILURE_WINDOW]
            for ip in stale_ips:
                del self.join_failures[ip]

    async def run(self, host="0.0.0.0", port=8766, cert=None, key=None):
        """Start the relay server."""
        ssl_context = None
        if cert and key:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(cert, key)
            logger.info(f"TLS enabled (cert={cert})")
        self._ssl_enabled = ssl_context is not None

        scheme = "wss" if ssl_context else "ws"
        logger.info(f"Relay server starting on {scheme}://{host}:{port}")

        async with serve(
            self.handle_connection,
            host,
            port,
            max_size=2**16,  # 64KB max message
            ssl=ssl_context,
            process_request=self._process_request,
        ) as server:
            logger.info(f"Relay server started on {scheme}://:{port}")
            # Run heartbeat checker alongside server
            heartbeat_task = asyncio.create_task(self.heartbeat_checker())
            try:
                await asyncio.Future()  # run forever
            finally:
                heartbeat_task.cancel()


def main():
    port = 8766
    verbose = False
    cert = None
    key = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--cert" and i + 1 < len(args):
            cert = args[i + 1]
            i += 2
        elif args[i] == "--key" and i + 1 < len(args):
            key = args[i + 1]
            i += 2
        elif args[i] == "--verbose":
            verbose = True
            i += 1
        else:
            i += 1

    if bool(cert) != bool(key):
        print("ERROR: --cert and --key must be provided together")
        sys.exit(1)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    server = RelayServer(verbose=verbose)
    asyncio.run(server.run(port=port, cert=cert, key=key))


if __name__ == "__main__":
    main()
