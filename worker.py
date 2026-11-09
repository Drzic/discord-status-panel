# Jedna veza na Discord gateway po tokenu: drzi racun "online" s odabranim statusom i custom statusom.
# Sam se ponovno spaja kad veza padne; neispravan token se ne pokusava beskonacno.
import json
import random
import threading
import time

import websocket

GATEWAY = "wss://gateway.discord.gg/?v=9&encoding=json"
FATAL = {4004: "Neispravan token", 4010: "Neispravan shard", 4011: "Potreban sharding", 4013: "Neispravan intent", 4014: "Nedopusten intent"}


def parse_emoji(s):
    """'😀' -> ('😀', None, False); custom emoji '<:ime:123>' / '<a:ime:123>' -> ('ime', '123', animiran)."""
    s = (s or "").strip()
    if s.startswith("<") and s.endswith(">") and s.count(":") == 2:
        anim, name, eid = s[1:-1].split(":")
        if eid.isdigit():
            return name, eid, anim == "a"
    return s, None, False


class Closed(Exception):
    def __init__(self, code):
        super().__init__(f"veza zatvorena ({code})")
        self.code = code


class Worker(threading.Thread):
    def __init__(self, acc):
        super().__init__(daemon=True)
        self.acc = dict(acc)
        self.stop_evt = threading.Event()
        self.ws = None
        self.lock = threading.Lock()
        self.state = "connecting"      # connecting | online | error | stopped
        self.error = None
        self.user = None
        self.since = None

    # ---------- presence ----------
    def presence(self):
        a = self.acc
        activities = []
        if a.get("custom_status") or a.get("emoji"):
            act = {"type": 4, "name": "Custom Status", "state": a.get("custom_status") or ""}
            if a.get("emoji"):
                name, eid, anim = parse_emoji(a["emoji"])
                act["emoji"] = {"name": name, "id": eid, "animated": anim} if eid else {"name": name}
            activities.append(act)
        # aktivnost: 0 igra, 1 stream, 2 slusa, 3 gleda, 5 natjece se
        t = a.get("act_type")
        if t is not None and int(t) in (0, 1, 2, 3, 5) and a.get("act_name"):
            activities.append({"type": int(t), "name": a["act_name"]})
        return {"status": a.get("status") or "online", "since": 0, "activities": activities, "afk": False}

    # platforma: mobitel (ikona mobitela kod imena), racunalo ili web
    PLATFORMS = {
        "mobile": {"os": "Android", "browser": "Discord Android", "device": "Android"},
        "desktop": {"os": "Windows", "browser": "Discord Client", "device": ""},
        "web": {"os": "Windows", "browser": "Chrome", "device": ""},
    }

    def send(self, payload):
        with self.lock:
            if self.ws:
                self.ws.send(json.dumps(payload))

    def update(self, acc):
        """Nove postavke (status / tekst) - posalji odmah, bez ponovnog spajanja."""
        self.acc.update(acc)
        try:
            self.send({"op": 3, "d": self.presence()})
        except Exception:
            pass

    # ---------- voice: racun "sjedi" u glasovnom kanalu (bez zvuka), s ugasenim mikrofonom / zvucnikom po zelji ----------
    def voice_payload(self, leave=False):
        a = self.acc
        return {"op": 4, "d": {"guild_id": a.get("voice_guild") or None,
                               "channel_id": None if leave else (a.get("voice_channel") or None),
                               "self_mute": bool(a.get("voice_mute")), "self_deaf": bool(a.get("voice_deaf")),
                               "self_video": False}}

    def set_voice(self, guild_id, channel_id, mute, deaf):
        """channel_id prazan = izadi iz voicea na tom serveru."""
        old_guild = self.acc.get("voice_guild")
        if old_guild and old_guild != guild_id and self.acc.get("voice_channel"):
            try:
                self.send(self.voice_payload(leave=True))        # najprije izadi sa starog servera
            except Exception:
                pass
        self.acc.update(voice_guild=guild_id, voice_channel=channel_id, voice_mute=mute, voice_deaf=deaf)
        try:
            self.send(self.voice_payload(leave=not channel_id))
        except Exception:
            pass

    def stop(self):
        self.stop_evt.set()
        with self.lock:
            try:
                if self.ws:
                    self.ws.close()
            except Exception:
                pass
        self.state = "stopped"

    # ---------- veza ----------
    def recv(self, ws):
        """Vraca poruku (dict) ili None ako je isteklo cekanje; baca Closed s kodom kad Discord zatvori vezu."""
        try:
            opcode, data = ws.recv_data()
        except websocket.WebSocketTimeoutException:
            return None
        if opcode == websocket.ABNF.OPCODE_CLOSE:
            code = int.from_bytes(data[:2], "big") if data and len(data) >= 2 else 1000
            raise Closed(code)
        if opcode in (websocket.ABNF.OPCODE_TEXT, websocket.ABNF.OPCODE_BINARY):
            return json.loads(data)
        return None

    def session(self):
        ws = websocket.create_connection(GATEWAY, timeout=30)
        with self.lock:
            self.ws = ws
        hello = None
        while hello is None:
            hello = self.recv(ws)
        interval = hello["d"]["heartbeat_interval"] / 1000.0
        ws.send(json.dumps({"op": 2, "d": {
            "token": self.acc["token"],
            "properties": self.PLATFORMS.get(self.acc.get("platform") or "desktop", self.PLATFORMS["desktop"]),
            "presence": self.presence(),
        }}))
        seq = None
        next_hb = time.time() + interval * random.random()
        ws.settimeout(1)
        while not self.stop_evt.is_set():
            if time.time() >= next_hb:
                ws.send(json.dumps({"op": 1, "d": seq}))
                next_hb = time.time() + interval
            msg = self.recv(ws)
            if not msg:
                continue
            if msg.get("s") is not None:
                seq = msg["s"]
            op = msg.get("op")
            if op == 0 and msg.get("t") == "READY":
                u = msg["d"]["user"]
                self.user = {"id": u.get("id"), "name": u.get("global_name") or u.get("username"),
                             "username": u.get("username"), "avatar": u.get("avatar")}
                self.state, self.error, self.since = "online", None, time.time()
                print(f"[veza] {self.user['username']}: spojen · status {self.acc.get('status')} · platforma {self.acc.get('platform') or 'desktop'}", flush=True)
                # Discord zna zanemariti status iz identify - posalji ga jos jednom nakon READY
                ws.send(json.dumps({"op": 3, "d": self.presence()}))
                if self.acc.get("voice_guild") and self.acc.get("voice_channel"):
                    ws.send(json.dumps(self.voice_payload()))           # nakon (ponovnog) spajanja vrati se u voice
            elif op == 7:                       # Discord trazi ponovno spajanje
                raise Closed(4000)
            elif op == 9:                       # neispravna sesija
                raise Closed(4009)

    def run(self):
        delay = 5
        while not self.stop_evt.is_set():
            self.state = "connecting"
            try:
                self.session()
            except Closed as e:
                print(f"[veza] racun {self.acc.get('id')}: Discord zatvorio vezu, kod {e.code}", flush=True)
                if e.code in FATAL:
                    self.state, self.error = "error", FATAL[e.code]
                    self.close_ws()
                    return
                self.error = str(e)
            except Exception as e:
                self.error = str(e)
            self.close_ws()
            if self.stop_evt.is_set():
                break
            if self.state == "online":
                delay = 5
            self.state = "connecting"
            self.stop_evt.wait(delay)
            delay = min(delay * 2, 120)
        self.state = "stopped"

    def close_ws(self):
        with self.lock:
            try:
                if self.ws:
                    self.ws.close()
            except Exception:
                pass
            self.ws = None

    def info(self):
        return {"state": self.state, "error": self.error, "user": self.user, "since": self.since}
