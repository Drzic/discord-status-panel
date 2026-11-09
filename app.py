# Discord Status Panel: web panel (lozinka) za vise tokena - svaki token ima svoj status, custom status i emoji,
# moze se ukljuciti / iskljuciti / restartati / obrisati. Tokeni u SQLite bazi (data.db, samo root).
# Pokretanje: python app.py  (postavke u config.json: password_hash, secret, port)
import base64
import datetime
import json
import os
import random
import sqlite3
import threading
import time
from functools import wraps

import requests
from flask import Flask, jsonify, redirect, request, session
from werkzeug.security import check_password_hash

from worker import Worker, parse_emoji

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
DB = os.path.join(BASE, "data.db")

app = Flask(__name__)
app.secret_key = CFG["secret"]
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", PERMANENT_SESSION_LIFETIME=7 * 86400)

db_lock = threading.Lock()
conn = sqlite3.connect(DB, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'online',
    custom_status TEXT NOT NULL DEFAULT '',
    emoji TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    username TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    avatar TEXT NOT NULL DEFAULT '',
    created INTEGER NOT NULL DEFAULT 0
)""")
conn.commit()
# nove kolone (starija baza ih nema): platforma, aktivnost, rotacija statusa
for col, ddl in (("platform", "TEXT NOT NULL DEFAULT 'desktop'"), ("act_type", "INTEGER NOT NULL DEFAULT -1"),
                 ("act_name", "TEXT NOT NULL DEFAULT ''"), ("rot_texts", "TEXT NOT NULL DEFAULT ''"),
                 ("rot_minutes", "INTEGER NOT NULL DEFAULT 0"), ("rot_status", "INTEGER NOT NULL DEFAULT 0"),
                 ("voice_guild", "TEXT NOT NULL DEFAULT ''"), ("voice_channel", "TEXT NOT NULL DEFAULT ''"),
                 ("voice_name", "TEXT NOT NULL DEFAULT ''"), ("voice_mute", "INTEGER NOT NULL DEFAULT 0"),
                 ("voice_deaf", "INTEGER NOT NULL DEFAULT 0")):
    try:
        conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} {ddl}")
    except sqlite3.OperationalError:
        pass
conn.commit()
# raspored: u odredeno vrijeme (po hrvatskom vremenu) odredenim danima promijeni status / tekst / emoji
conn.execute("""CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    at TEXT NOT NULL,                       -- HH:MM
    days TEXT NOT NULL DEFAULT '0123456',   -- 0 = ponedjeljak ... 6 = nedjelja
    status TEXT NOT NULL DEFAULT '',        -- prazno = ne mijenjaj
    set_text INTEGER NOT NULL DEFAULT 0,
    custom_status TEXT NOT NULL DEFAULT '',
    emoji TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run TEXT NOT NULL DEFAULT ''
)""")
conn.commit()
try:
    os.chmod(DB, 0o600)
except Exception:
    pass

workers = {}          # id -> Worker
STATUSES = ("online", "idle", "dnd", "invisible")
fails = {}            # ip -> (broj, vrijeme) - zastita od pogadjanja lozinke


def q(sql, args=(), one=False):
    with db_lock:
        cur = conn.execute(sql, args)
        conn.commit()
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def start(acc):
    stop(acc["id"])
    w = Worker(acc)
    workers[acc["id"]] = w
    w.start()


def stop(aid):
    w = workers.pop(aid, None)
    if w:
        w.stop()


def check_token(token):
    r = requests.get("https://discord.com/api/v9/users/@me", headers={"Authorization": token}, timeout=10)
    if r.status_code != 200:
        return None
    return r.json()


def _pb(field, payload):
    """protobuf: polje (duljinski zapis) - dovoljno za Discordove postavke statusa"""
    n, out = len(payload), bytearray([(field << 3) | 2])
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + payload


def _proto_status(status, text, emoji):
    # PreloadedUserSettings.status (11) -> StatusSettings { status (1): StringValue, custom_status (2): CustomStatus }
    # CustomStatus { text (1), emoji_id (2, fixed64), emoji_name (3) }
    cs = b""
    if text:
        cs += _pb(1, text.encode())
    name, eid, _ = parse_emoji(emoji)
    if eid:
        cs += bytes([(2 << 3) | 1]) + int(eid).to_bytes(8, "little")
    if name:
        cs += _pb(3, name.encode())
    st = _pb(1, _pb(1, status.encode())) + _pb(2, cs)      # prazan custom_status = obrisan
    return base64.b64encode(_pb(11, st)).decode()


def apply_settings(acc, clear=False):
    """Status i custom status u POSTAVKAMA racuna - inace otvoreni Discord (mobitel / racunalo) vraca svoj status.
    Salje se na oba mjesta: novi format (settings-proto, koristi danasnji Discord) i stari (/settings).
    clear = racun iskljucen / obrisan u panelu: makni custom status, vrati na obican online."""
    status = "online" if clear else (acc.get("status") or "online")
    cs = "" if clear else (acc.get("custom_status") or "")
    emoji = "" if clear else (acc.get("emoji") or "")
    h = {"Authorization": acc["token"], "Content-Type": "application/json",
         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    ok = False
    who = acc.get("username") or acc.get("id")
    try:
        r = requests.patch("https://discord.com/api/v9/users/@me/settings-proto/1",
                           json={"settings": _proto_status(status, cs, emoji)}, headers=h, timeout=10)
        ok = r.status_code in (200, 204)
        print(f"[postavke] {who}: settings-proto -> {r.status_code} {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[postavke] {who}: settings-proto greska {e}", flush=True)
    try:
        ename, eid, _ = parse_emoji(emoji)
        body = {"status": status, "custom_status": {"text": cs or None, "emoji_name": ename or None, "emoji_id": eid}
                if (cs or emoji) else None}
        r = requests.patch("https://discord.com/api/v9/users/@me/settings", json=body, headers=h, timeout=10)
        ok = ok or r.status_code in (200, 204)
        print(f"[postavke] {who}: settings -> {r.status_code} {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[postavke] {who}: settings greska {e}", flush=True)
    return ok


def view(row):
    w = workers.get(row["id"])
    info = w.info() if w else {"state": "stopped", "error": None, "user": None, "since": None}
    t = row["token"]
    return {
        "id": row["id"], "label": row["label"], "status": row["status"], "custom_status": row["custom_status"],
        "emoji": row["emoji"], "enabled": bool(row["enabled"]), "username": row["username"], "user_id": row["user_id"],
        "avatar": row["avatar"], "token": t[:6] + "…" + t[-4:] if len(t) > 12 else "…",
        "state": info["state"] if row["enabled"] else "stopped", "error": info["error"], "since": info["since"],
        "platform": row["platform"], "act_type": row["act_type"], "act_name": row["act_name"],
        "rot_texts": row["rot_texts"], "rot_minutes": row["rot_minutes"], "rot_status": bool(row["rot_status"]),
        "now": {"status": w.acc.get("status"), "custom_status": w.acc.get("custom_status")} if w else None,
        "schedules": [dict(s) for s in q("SELECT * FROM schedules WHERE account_id = ? ORDER BY at", (row["id"],))],
        "voice": {"guild": row["voice_guild"], "channel": row["voice_channel"], "name": row["voice_name"],
                  "mute": bool(row["voice_mute"]), "deaf": bool(row["voice_deaf"])},
    }


def login_required(f):
    @wraps(f)
    def inner(*a, **k):
        if not session.get("ok"):
            return jsonify(ok=False, msg="Nisi prijavljen."), 401
        return f(*a, **k)
    return inner


# ---------- prijava ----------
@app.post("/login")
def login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    n, t = fails.get(ip, (0, 0))
    if n >= 5 and time.time() - t < 300:
        return jsonify(ok=False, msg="Previše pokušaja - pričekaj 5 minuta."), 429
    if check_password_hash(CFG["password_hash"], (request.json or {}).get("password", "")):
        fails.pop(ip, None)
        session.permanent = True
        session["ok"] = True
        return jsonify(ok=True)
    fails[ip] = (n + 1, time.time())
    return jsonify(ok=False, msg="Kriva lozinka."), 401


@app.post("/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


# ---------- racuni ----------
@app.get("/api/accounts")
@login_required
def accounts():
    return jsonify(ok=True, accounts=[view(r) for r in q("SELECT * FROM accounts ORDER BY id")])


@app.post("/api/accounts")
@login_required
def add():
    d = request.json or {}
    token = (d.get("token") or "").strip().strip('"')
    if not token:
        return jsonify(ok=False, msg="Upiši token."), 400
    if q("SELECT id FROM accounts WHERE token = ?", (token,), one=True):
        return jsonify(ok=False, msg="Taj token je već dodan."), 400
    u = check_token(token)
    if not u:
        return jsonify(ok=False, msg="Token nije ispravan (Discord ga je odbio)."), 400
    status = d.get("status") if d.get("status") in STATUSES else "online"
    q("""INSERT INTO accounts (label, token, status, custom_status, emoji, enabled, username, user_id, avatar, created)
         VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
      ((d.get("label") or "")[:40], token, status, (d.get("custom_status") or "")[:128], (d.get("emoji") or "")[:80],
       u.get("global_name") or u.get("username") or "", u.get("id") or "", u.get("avatar") or "", int(time.time())))
    row = q("SELECT * FROM accounts WHERE token = ?", (token,), one=True)
    apply_settings(dict(row))
    start(dict(row))
    return jsonify(ok=True, msg=f"Dodan račun {row['username']}.")


@app.put("/api/accounts/<int:aid>")
@login_required
def edit(aid):
    d = request.json or {}
    row = q("SELECT * FROM accounts WHERE id = ?", (aid,), one=True)
    if not row:
        return jsonify(ok=False, msg="Nema tog računa."), 404
    g = lambda k, cut=None: (d.get(k) if d.get(k) is not None else row[k]) if cut is None else str(d.get(k) if d.get(k) is not None else row[k])[:cut]
    status = d.get("status") if d.get("status") in STATUSES else row["status"]
    label, cs, emoji = g("label", 40), g("custom_status", 128), g("emoji", 80)
    platform = d.get("platform") if d.get("platform") in ("desktop", "mobile", "web") else row["platform"]
    act_type = int(d.get("act_type")) if str(d.get("act_type", "")).lstrip("-").isdigit() else row["act_type"]
    act_name = g("act_name", 64)
    rot_texts = "\n".join(t.strip()[:128] for t in str(g("rot_texts")).splitlines() if t.strip())[:3000]
    rot_minutes = max(0, min(1440, int(d.get("rot_minutes") if str(d.get("rot_minutes", "")).isdigit() else row["rot_minutes"])))
    rot_status = 1 if d.get("rot_status") in (True, 1, "1", "true") else (0 if "rot_status" in d else row["rot_status"])
    q("""UPDATE accounts SET label = ?, status = ?, custom_status = ?, emoji = ?, platform = ?, act_type = ?, act_name = ?,
         rot_texts = ?, rot_minutes = ?, rot_status = ? WHERE id = ?""",
      (label, status, cs, emoji, platform, act_type, act_name, rot_texts, rot_minutes, rot_status, aid))
    new = dict(q("SELECT * FROM accounts WHERE id = ?", (aid,), one=True))
    w = workers.get(aid)
    if w and platform != row["platform"]:
        start(new)                               # platforma se mijenja samo novom prijavom na gateway
    elif w:
        w.update(new)
    ok = apply_settings(new) if row["enabled"] else True
    rot_next.pop(aid, None)
    return jsonify(ok=True, msg="Spremljeno." if ok else "Spremljeno u panelu, ali Discord nije prihvatio postavke (provjeri token).")


# novi token za postojeci racun (npr. nakon odjave iz Discorda stari token prestane vrijediti)
@app.put("/api/accounts/<int:aid>/token")
@login_required
def new_token(aid):
    row = q("SELECT * FROM accounts WHERE id = ?", (aid,), one=True)
    if not row:
        return jsonify(ok=False, msg="Nema tog računa."), 404
    token = ((request.json or {}).get("token") or "").strip().strip('"')
    u = token and check_token(token)
    if not u:
        return jsonify(ok=False, msg="Novi token nije ispravan (Discord ga je odbio)."), 400
    if row["user_id"] and u.get("id") != row["user_id"]:
        return jsonify(ok=False, msg="Taj token pripada drugom računu - dodaj ga kao novi račun."), 400
    q("UPDATE accounts SET token = ?, username = ?, avatar = ?, enabled = 1 WHERE id = ?",
      (token, u.get("global_name") or u.get("username") or "", u.get("avatar") or "", aid))
    row = q("SELECT * FROM accounts WHERE id = ?", (aid,), one=True)
    apply_settings(dict(row))
    start(dict(row))
    return jsonify(ok=True, msg="Token zamijenjen - račun se spaja.")


@app.post("/api/accounts/<int:aid>/<action>")
@login_required
def control(aid, action):
    row = q("SELECT * FROM accounts WHERE id = ?", (aid,), one=True)
    if not row:
        return jsonify(ok=False, msg="Nema tog računa."), 404
    if action == "enable":
        q("UPDATE accounts SET enabled = 1 WHERE id = ?", (aid,))
        apply_settings(dict(row))
        start(dict(row))
    elif action == "disable":
        q("UPDATE accounts SET enabled = 0 WHERE id = ?", (aid,))
        stop(aid)
        apply_settings(dict(row), clear=True)    # makni custom status, racun je opet obican online
    elif action == "restart":
        if row["enabled"]:
            start(dict(row))
    else:
        return jsonify(ok=False), 400
    return jsonify(ok=True)


@app.delete("/api/accounts/<int:aid>")
@login_required
def delete(aid):
    row = q("SELECT * FROM accounts WHERE id = ?", (aid,), one=True)
    stop(aid)
    if row:
        apply_settings(dict(row), clear=True)
    q("DELETE FROM accounts WHERE id = ?", (aid,))
    q("DELETE FROM schedules WHERE account_id = ?", (aid,))
    return jsonify(ok=True, msg="Račun obrisan.")


# ---------- voice ----------
@app.post("/api/accounts/<int:aid>/voice")
@login_required
def voice(aid):
    """{guild_id, channel_id, name, mute, deaf} - channel_id prazan = izadi. Bez guild_id samo mijenja mute / deaf."""
    row = q("SELECT * FROM accounts WHERE id = ?", (aid,), one=True)
    if not row:
        return jsonify(ok=False, msg="Nema tog računa."), 404
    d = request.json or {}
    gid = str(d.get("guild_id") if d.get("guild_id") is not None else row["voice_guild"])
    cid = str(d.get("channel_id") if d.get("channel_id") is not None else row["voice_channel"])
    name = str(d.get("name") if d.get("name") is not None else row["voice_name"])[:120]
    mute = 1 if (d.get("mute") if "mute" in d else row["voice_mute"]) else 0
    deaf = 1 if (d.get("deaf") if "deaf" in d else row["voice_deaf"]) else 0
    if cid and not (gid.isdigit() and cid.isdigit()):
        return jsonify(ok=False, msg="Odaberi server i glasovni kanal."), 400
    w = workers.get(aid)
    if cid and (not w or w.state != "online"):
        return jsonify(ok=False, msg="Račun nije spojen - uključi ga pa probaj opet."), 400
    if w:
        w.set_voice(gid, cid, mute, deaf)
    if not cid:
        name = ""
    q("UPDATE accounts SET voice_guild = ?, voice_channel = ?, voice_name = ?, voice_mute = ?, voice_deaf = ? WHERE id = ?",
      (gid if cid else "", cid, name, mute, deaf, aid))
    return jsonify(ok=True, msg=("U voiceu: " + name) if cid else "Izašao iz voicea.")


# ---------- raspored ----------
@app.post("/api/accounts/<int:aid>/schedules")
@login_required
def schedule_add(aid):
    if not q("SELECT id FROM accounts WHERE id = ?", (aid,), one=True):
        return jsonify(ok=False, msg="Nema tog računa."), 404
    d = request.json or {}
    at = str(d.get("at") or "")
    if not (len(at) == 5 and at[2] == ":" and at[:2].isdigit() and at[3:].isdigit() and int(at[:2]) < 24 and int(at[3:]) < 60):
        return jsonify(ok=False, msg="Upiši vrijeme (npr. 08:30)."), 400
    days = "".join(sorted({c for c in str(d.get("days") or "") if c in "0123456"}))
    if not days:
        return jsonify(ok=False, msg="Odaberi barem jedan dan."), 400
    status = d.get("status") if d.get("status") in STATUSES else ""
    set_text = 1 if d.get("set_text") else 0
    if not status and not set_text:
        return jsonify(ok=False, msg="Pravilo ništa ne mijenja - odaberi status ili tekst."), 400
    q("""INSERT INTO schedules (account_id, at, days, status, set_text, custom_status, emoji) VALUES (?, ?, ?, ?, ?, ?, ?)""",
      (aid, at, days, status, set_text, str(d.get("custom_status") or "")[:128], str(d.get("emoji") or "")[:80]))
    return jsonify(ok=True, msg="Pravilo dodano.")


@app.post("/api/schedules/<int:sid>/toggle")
@login_required
def schedule_toggle(sid):
    q("UPDATE schedules SET enabled = 1 - enabled WHERE id = ?", (sid,))
    return jsonify(ok=True)


@app.delete("/api/schedules/<int:sid>")
@login_required
def schedule_delete(sid):
    q("DELETE FROM schedules WHERE id = ?", (sid,))
    return jsonify(ok=True, msg="Pravilo obrisano.")


def run_schedule(s):
    """Primijeni pravilo: spremi u racun (ostaje i nakon restarta) i posalji Discordu."""
    row = q("SELECT * FROM accounts WHERE id = ?", (s["account_id"],), one=True)
    if not row:
        return
    acc = dict(row)
    if s["status"]:
        acc["status"] = s["status"]
    if s["set_text"]:
        acc["custom_status"], acc["emoji"] = s["custom_status"], s["emoji"]
    q("UPDATE accounts SET status = ?, custom_status = ?, emoji = ? WHERE id = ?",
      (acc["status"], acc["custom_status"], acc["emoji"], acc["id"]))
    print(f"[raspored] {acc['username']}: {s['at']} -> {acc['status']} · {acc['custom_status'] or '-'}", flush=True)
    if not acc["enabled"]:
        return
    w = workers.get(acc["id"])
    if w:
        w.update(acc)
    apply_settings(acc)
    rot_next.pop(acc["id"], None)


def schedule_loop():
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Zagreb")
    except Exception:
        tz = None
    while True:
        time.sleep(15)
        try:
            now = datetime.datetime.now(tz)
            hm, day, today = now.strftime("%H:%M"), str(now.weekday()), now.strftime("%Y-%m-%d")
            for s in q("SELECT * FROM schedules WHERE enabled = 1 AND at = ? AND last_run != ?", (hm, today)):
                if day in s["days"]:
                    q("UPDATE schedules SET last_run = ? WHERE id = ?", (today, s["id"]))
                    run_schedule(dict(s))
        except Exception as e:
            print("raspored:", e, flush=True)


# ---------- rotacija: svakih rot_minutes nasumicni tekst s popisa (i po zelji nasumicni status) ----------
rot_next = {}   # id -> kad je iduca izmjena
ROT_STATUSES = ("online", "idle", "dnd")


def rotation_loop():
    while True:
        time.sleep(10)
        now = time.time()
        try:
            for r in q("SELECT * FROM accounts WHERE enabled = 1 AND rot_minutes > 0"):
                texts = [t for t in (r["rot_texts"] or "").splitlines() if t.strip()]
                if not texts and not r["rot_status"]:
                    continue
                if now < rot_next.get(r["id"], 0):
                    continue
                rot_next[r["id"]] = now + r["rot_minutes"] * 60
                w = workers.get(r["id"])
                if not w:
                    continue
                cur = dict(r)
                if texts:
                    cur["custom_status"] = random.choice(texts)
                if r["rot_status"]:
                    cur["status"] = random.choice(ROT_STATUSES)
                w.update({"status": cur["status"], "custom_status": cur["custom_status"]})
                apply_settings(cur)
        except Exception as e:
            print("rotacija:", e)


import chat
import clone
import join
chat.register(app, q, login_required)
clone.register(app, q, login_required)
join.register(app, q, login_required, CFG)


# ---------- stranica ----------
@app.get("/")
def index():
    return open(os.path.join(BASE, "index.html"), encoding="utf-8").read()


if __name__ == "__main__":
    for r in q("SELECT * FROM accounts WHERE enabled = 1"):
        start(dict(r))
        time.sleep(1.5)       # ne spajaj sve racune u istoj sekundi
    threading.Thread(target=rotation_loop, daemon=True).start()
    threading.Thread(target=schedule_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(CFG.get("port", 8090)), threaded=True)
