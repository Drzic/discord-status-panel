# Ulazak na server preko sluzbenog OAuth2 (guilds.join): racun jednom klikne "Authorize" na linku iz panela,
# a onda ga tvoj bot ubaci na tvoj server kao obicnog clana. Bez tokena racuna i bez captche.
# Postavke u config.json: oauth_client_secret (+ po zelji oauth_client_id, bot_token, public_url).
# Bez bot_token panel koristi token NextGaming bota (/opt/nextgaming-bot/config.json), a Client ID dozna od Discorda.
import json
import os
import secrets
import time
from html import escape
from urllib.parse import urlencode

import requests
from flask import jsonify, request

import chat

API = "https://discord.com/api/v10"
states = {}                   # state iz linka -> vrijedi do (link se moze otvoriti s vise racuna)
STATE_TTL = 24 * 3600
NG_BOT_CFG = "/opt/nextgaming-bot/config.json"
_app_id = {}                  # token -> Client ID aplikacije


def register(app, q, login_required, cfg):
    q("""CREATE TABLE IF NOT EXISTS oauth_users (
        user_id TEXT PRIMARY KEY,
        username TEXT NOT NULL DEFAULT '',
        avatar TEXT NOT NULL DEFAULT '',
        access_token TEXT NOT NULL,
        refresh_token TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        created INTEGER NOT NULL DEFAULT 0
    )""")

    def bot_token():
        if cfg.get("bot_token"):
            return cfg["bot_token"]
        try:                                     # token NextGaming bota (isti VPS)
            with open(NG_BOT_CFG, encoding="utf-8") as f:
                return (json.load(f).get("token") or "").strip() or None
        except Exception:
            return None

    def client_id():
        if cfg.get("oauth_client_id"):
            return cfg["oauth_client_id"]
        tok = bot_token()
        if not tok:
            return None
        if tok not in _app_id:
            try:
                r = requests.get(f"{API}/oauth2/applications/@me", headers={"Authorization": "Bot " + tok}, timeout=12)
                if r.status_code == 200:
                    _app_id[tok] = str(r.json().get("id") or "")
            except Exception:
                pass
        return _app_id.get(tok) or None

    def ready():
        return bool(cfg.get("oauth_client_secret") and bot_token() and client_id())

    def redirect_uri():
        base = (cfg.get("public_url") or request.host_url).rstrip("/")
        return base + "/oauth/callback"

    def token_call(data):
        data = {**data, "client_id": client_id(), "client_secret": cfg["oauth_client_secret"]}
        r = requests.post(f"{API}/oauth2/token", data=data, timeout=12)
        return r.json() if r.status_code == 200 else None

    def bot(method, path, **kw):
        return requests.request(method, API + path, headers={"Authorization": "Bot " + (bot_token() or "")}, timeout=12, **kw)

    def access_token(row):
        """Vrati vazeci access token; ako je istekao, obnovi ga refresh tokenom."""
        if row["expires_at"] > time.time() + 60:
            return row["access_token"]
        t = token_call({"grant_type": "refresh_token", "refresh_token": row["refresh_token"]})
        if not t:
            return None
        q("UPDATE oauth_users SET access_token = ?, refresh_token = ?, expires_at = ? WHERE user_id = ?",
          (t["access_token"], t.get("refresh_token") or row["refresh_token"], int(time.time()) + int(t.get("expires_in", 604800)),
           row["user_id"]))
        return t["access_token"]

    # ---------- postavke i link ----------
    @app.get("/api/join/config")
    @login_required
    def join_config():
        if not ready():
            return jsonify(ok=True, ready=False)
        now = time.time()
        for s, exp in list(states.items()):
            if exp < now:
                states.pop(s, None)
        state = secrets.token_urlsafe(16)
        states[state] = now + STATE_TTL
        url = "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": client_id(), "response_type": "code", "redirect_uri": redirect_uri(),
            "scope": "identify guilds.join", "state": state, "prompt": "consent"})
        return jsonify(ok=True, ready=True, url=url, redirect=redirect_uri())

    # ---------- povratak s Discorda nakon "Authorize" ----------
    @app.get("/oauth/callback")
    def oauth_callback():
        def page(title, text):
            return (f'<!DOCTYPE html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                    f'<title>{title}</title><body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0e14;'
                    f'color:#eef1f7;font-family:system-ui,sans-serif;text-align:center;padding:16px"><div><h1>{title}</h1>'
                    f'<p style="color:#8a93a6">{text}</p></div></body>')

        if request.args.get("error"):
            return page("Odustao si", "Autorizacija nije dana - možeš zatvoriti ovu stranicu.")
        state, code = request.args.get("state", ""), request.args.get("code", "")
        if not ready() or states.get(state, 0) < time.time() or not code:
            return page("Link je istekao", "Uzmi novi link u panelu i pokušaj ponovo."), 400
        t = token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()})
        if not t or "guilds.join" not in (t.get("scope") or ""):
            return page("Nije uspjelo", "Discord nije potvrdio autorizaciju - pokušaj ponovo."), 400
        u = requests.get(f"{API}/users/@me", headers={"Authorization": "Bearer " + t["access_token"]}, timeout=12).json()
        name = u.get("global_name") or u.get("username") or ""
        q("""INSERT OR REPLACE INTO oauth_users (user_id, username, avatar, access_token, refresh_token, expires_at, created)
             VALUES (?, ?, ?, ?, ?, ?, ?)""",
          (u["id"], name, u.get("avatar") or "", t["access_token"], t["refresh_token"],
           int(time.time()) + int(t.get("expires_in", 604800)), int(time.time())))
        print(f"[ulazak] {name} ({u['id']}) autoriziran", flush=True)
        return page("✓ Gotovo", f"{escape(name)} je autoriziran. Sada ga možeš ubaciti na server iz panela.")

    # ---------- popis autoriziranih i serveri bota ----------
    @app.get("/api/join/users")
    @login_required
    def join_users():
        panel = {r["user_id"]: r["label"] or r["username"] for r in q("SELECT user_id, label, username FROM accounts")}
        return jsonify(ok=True, users=[{"id": r["user_id"], "name": r["username"], "avatar": r["avatar"],
                                        "panel": panel.get(r["user_id"])}
                                       for r in q("SELECT * FROM oauth_users ORDER BY created DESC")])

    @app.get("/api/join/guilds")
    @login_required
    def join_guilds():
        if not ready():
            return jsonify(ok=False, msg="Upiši Client Secret (bash install.sh oauth)."), 400
        r = bot("GET", "/users/@me/guilds")
        if r.status_code == 401:
            return jsonify(ok=False, msg="Token bota nije ispravan (NextGaming bot ili bot_token)."), 400
        if r.status_code != 200:
            return jsonify(ok=False, msg=f"Discord greška {r.status_code}."), 400
        return jsonify(ok=True, guilds=[{"id": g["id"], "name": g["name"]} for g in r.json()])

    @app.delete("/api/join/users/<uid>")
    @login_required
    def join_forget(uid):
        q("DELETE FROM oauth_users WHERE user_id = ?", (uid,))
        return jsonify(ok=True, msg="Autorizacija obrisana iz panela.")

    # ---------- ubaci na server ----------
    @app.post("/api/join/add")
    @login_required
    def join_add():
        d = request.json or {}
        uid, gid = str(d.get("user_id") or ""), str(d.get("guild_id") or "")
        row = q("SELECT * FROM oauth_users WHERE user_id = ?", (uid,), one=True)
        if not row or not gid.isdigit():
            return jsonify(ok=False, msg="Odaberi server i račun."), 400
        at = access_token(row)
        if not at:
            return jsonify(ok=False, msg="Autorizacija je istekla ili opozvana - račun neka ponovo otvori link."), 400
        r = bot("PUT", f"/guilds/{gid}/members/{uid}", json={"access_token": at})
        if r.status_code == 201:
            for k in [k for k in chat.cache if k[1] == "guilds"]:
                chat.cache.pop(k, None)          # panel odmah vidi novi server u Porukama
            return jsonify(ok=True, msg=f"{row['username']} je ušao na server.")
        if r.status_code == 204:
            return jsonify(ok=True, msg=f"{row['username']} je već na tom serveru.")
        if r.status_code == 403:
            return jsonify(ok=False, msg="Bot nema dozvolu Create Invite na tom serveru (ili je račun bannan)."), 400
        try:
            msg = (r.json() or {}).get("message")
        except Exception:
            msg = None
        return jsonify(ok=False, msg=msg or f"Discord greška {r.status_code}."), 400
