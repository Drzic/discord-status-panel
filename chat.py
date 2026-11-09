# Poruke iz panela: racun iz panela cita i pise u kanale servera i privatne poruke (kao obican Discord klijent).
# Sve rucno iz panela - nema automatskog ni masovnog slanja.
import random
import time
from urllib.parse import quote

import requests
from flask import jsonify, request

API = "https://discord.com/api/v9"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TEXT_TYPES = (0, 5, 15)        # tekst, obavijesti, forum (forum se samo prikaze)
VIEW, ADMIN, CONNECT = 1 << 10, 1 << 3, 1 << 20
cache = {}                    # (racun, kljuc) -> (vrijeme, podaci) - serveri i kanali se ne mijenjaju svake sekunde


def avatar_url(u):
    if u.get("avatar"):
        return f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png?size=64"
    return f"https://cdn.discordapp.com/embed/avatars/{(int(u.get('id') or 0) >> 22) % 6}.png"


def register(app, q, login_required):
    def account(aid):
        row = q("SELECT * FROM accounts WHERE id = ?", (aid,), one=True)
        return dict(row) if row else None

    def call(acc, method, path, **kw):
        h = {"Authorization": acc["token"], "User-Agent": UA}
        if "json" in kw:
            h["Content-Type"] = "application/json"
        r = requests.request(method, API + path, headers=h, timeout=12, **kw)
        if r.status_code == 429:
            wait = (r.json() or {}).get("retry_after", 1) if r.headers.get("content-type", "").startswith("application/json") else 1
            return None, f"Discord usporava - pričekaj {round(float(wait), 1)} s."
        if r.status_code == 401:
            return None, "Token ne vrijedi - zamijeni ga (Novi token)."
        if r.status_code == 403:
            return None, "Nemaš pristup tom kanalu ili radnji."
        if r.status_code >= 400:
            try:
                j = r.json() or {}
            except Exception:
                return None, f"Greška {r.status_code}"
            if j.get("captcha_key"):
                return None, "Discord traži captchu - ovu promjenu napravi u Discord aplikaciji."
            errs = j.get("errors") or {}
            for field in errs.values():                     # npr. {"username": {"_errors": [{"message": "..."}]}}
                for e in (field or {}).get("_errors") or []:
                    return None, e.get("message") or j.get("message")
            return None, j.get("message") or f"Greška {r.status_code}"
        if r.status_code == 204 or not r.content:
            return {}, None
        return r.json(), None

    def cached(acc, key, path, ttl=60):
        k = (acc["id"], key)
        hit = cache.get(k)
        if hit and time.time() - hit[0] < ttl:
            return hit[1], None
        data, err = call(acc, "GET", path)
        if data is not None:
            cache[k] = (time.time(), data)
        return data, err

    def need(aid):
        acc = account(aid)
        if not acc:
            return None, (jsonify(ok=False, msg="Nema tog računa."), 404)
        return acc, None

    def fail(msg):
        return jsonify(ok=False, msg=msg), 400

    def simple(m, me):
        a = m.get("author") or {}
        content = m.get("content") or ""
        for u in m.get("mentions") or []:
            name = "@" + (u.get("global_name") or u.get("username") or "?")
            content = content.replace(f"<@{u['id']}>", name).replace(f"<@!{u['id']}>", name)
        ref = m.get("referenced_message") or None
        return {
            "id": m["id"], "content": content, "ts": m.get("timestamp"), "edited": bool(m.get("edited_timestamp")),
            "type": m.get("type", 0), "mine": a.get("id") == me,
            "author": {"id": a.get("id"), "name": a.get("global_name") or a.get("username") or "?", "avatar": avatar_url(a), "bot": bool(a.get("bot"))},
            "attachments": [{"url": f.get("url"), "name": f.get("filename"), "image": (f.get("content_type") or "").startswith("image/")}
                            for f in m.get("attachments") or []],
            "embeds": [{"title": e.get("title") or "", "description": e.get("description") or "", "image": (e.get("image") or e.get("thumbnail") or {}).get("url")}
                       for e in m.get("embeds") or [] if e.get("title") or e.get("description") or e.get("image") or e.get("thumbnail")],
            "reactions": [{"emoji": (r.get("emoji") or {}).get("name") or "?", "id": (r.get("emoji") or {}).get("id"),
                           "count": r.get("count", 0), "me": bool(r.get("me"))} for r in m.get("reactions") or []],
            "reply": {"name": ((ref.get("author") or {}).get("global_name") or (ref.get("author") or {}).get("username") or "?"),
                      "content": (ref.get("content") or "")[:120]} if ref else None,
        }

    def viewer(acc, gid):
        """Vraca funkciju kanal -> vidi li ga racun (VIEW_CHANNEL po rangovima i dozvolama kanala, kao Discord)."""
        me = acc.get("user_id")
        guild, _ = cached(acc, "g:" + gid, f"/guilds/{gid}", 300)
        roles, _ = cached(acc, "r:" + gid, f"/guilds/{gid}/roles", 120)
        member, _ = cached(acc, "m:" + gid, f"/users/@me/guilds/{gid}/member", 120)
        if member is None and me:
            member, _ = cached(acc, "m:" + gid, f"/guilds/{gid}/members/{me}", 120)
        if guild is None or roles is None or member is None:
            return lambda c, bit=VIEW: True    # ne znamo dozvole - ne skrivaj nista
        if guild.get("owner_id") == me:
            return lambda c, bit=VIEW: True
        mine = set(member.get("roles") or [])
        base = 0
        for r in roles:
            if r["id"] == gid or r["id"] in mine:
                base |= int(r.get("permissions") or 0)
        if base & ADMIN:
            return lambda c, bit=VIEW: True

        def can(c, bit=VIEW):
            p = base
            ows = {o["id"]: o for o in c.get("permission_overwrites") or []}
            o = ows.get(gid)                                        # @everyone
            if o:
                p = (p & ~int(o["deny"])) | int(o["allow"])
            allow = deny = 0
            for rid in mine:                                        # svi rangovi zajedno
                o = ows.get(rid)
                if o and o.get("type") == 0:
                    allow |= int(o["allow"])
                    deny |= int(o["deny"])
            p = (p & ~deny) | allow
            o = ows.get(me)                                         # dozvola samo za tog clana
            if o and o.get("type") == 1:
                p = (p & ~int(o["deny"])) | int(o["allow"])
            return bool(p & VIEW) and bool(p & bit)
        return can

    # ---------- serveri, kanali, privatne poruke ----------
    @app.get("/api/chat/<int:aid>/guilds")
    @login_required
    def chat_guilds(aid):
        acc, e = need(aid)
        if e:
            return e
        data, err = cached(acc, "guilds", "/users/@me/guilds", 120)
        if err:
            return fail(err)
        return jsonify(ok=True, guilds=[{
            "id": g["id"], "name": g["name"],
            "icon": f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png?size=64" if g.get("icon") else None,
        } for g in data])

    @app.get("/api/chat/<int:aid>/guilds/<gid>/channels")
    @login_required
    def chat_channels(aid, gid):
        acc, e = need(aid)
        if e:
            return e
        data, err = cached(acc, "ch:" + gid, f"/guilds/{gid}/channels", 60)
        if err:
            return fail(err)
        can_view = viewer(acc, gid)
        cats = {c["id"]: c for c in data if c.get("type") == 4}
        out = [{"id": c["id"], "name": c["name"], "type": c["type"], "cat": (cats.get(c.get("parent_id")) or {}).get("name", ""),
                "catpos": (cats.get(c.get("parent_id")) or {}).get("position", -1), "pos": c.get("position", 0),
                "view": can_view(c)}
               for c in data if c.get("type") in TEXT_TYPES]
        out.sort(key=lambda c: (c["catpos"], c["pos"]))
        return jsonify(ok=True, channels=out)

    @app.get("/api/chat/<int:aid>/guilds/<gid>/voice")
    @login_required
    def chat_voice(aid, gid):
        acc, e = need(aid)
        if e:
            return e
        data, err = cached(acc, "ch:" + gid, f"/guilds/{gid}/channels", 60)
        if err:
            return fail(err)
        can = viewer(acc, gid)
        cats = {c["id"]: c for c in data if c.get("type") == 4}
        out = [{"id": c["id"], "name": c["name"], "stage": c["type"] == 13, "limit": c.get("user_limit") or 0,
                "cat": (cats.get(c.get("parent_id")) or {}).get("name", ""),
                "catpos": (cats.get(c.get("parent_id")) or {}).get("position", -1), "pos": c.get("position", 0),
                "can": can(c, CONNECT)}
               for c in data if c.get("type") in (2, 13) and can(c)]
        out.sort(key=lambda c: (c["catpos"], c["pos"]))
        return jsonify(ok=True, channels=out)

    @app.get("/api/chat/<int:aid>/guilds/<gid>/emojis")
    @login_required
    def chat_emojis(aid, gid):
        acc, e = need(aid)
        if e:
            return e
        data, err = cached(acc, "em:" + gid, f"/guilds/{gid}/emojis", 600)
        if err:
            return fail(err)
        return jsonify(ok=True, emojis=[{"id": x["id"], "name": x["name"], "animated": bool(x.get("animated"))}
                                        for x in data if x.get("available", True)])

    @app.get("/api/chat/<int:aid>/dms")
    @login_required
    def chat_dms(aid):
        acc, e = need(aid)
        if e:
            return e
        data, err = cached(acc, "dms", "/users/@me/channels", 30)
        if err:
            return fail(err)
        out = []
        for c in data:
            users = c.get("recipients") or []
            name = c.get("name") or ", ".join(u.get("global_name") or u.get("username") or "?" for u in users)
            out.append({"id": c["id"], "name": name or "Privatna poruka", "avatar": avatar_url(users[0]) if users else None,
                        "last": c.get("last_message_id") or "0"})
        out.sort(key=lambda c: int(c["last"]), reverse=True)
        return jsonify(ok=True, dms=out[:60])

    # ---------- ime racuna ----------
    @app.get("/api/chat/<int:aid>/me")
    @login_required
    def chat_me(aid):
        acc, e = need(aid)
        if e:
            return e
        data, err = call(acc, "GET", "/users/@me")
        if err:
            return fail(err)
        return jsonify(ok=True, username=data.get("username"), global_name=data.get("global_name") or "")

    def remember_name(acc, u):
        q("UPDATE accounts SET username = ? WHERE id = ?", (u.get("global_name") or u.get("username") or "", acc["id"]))

    @app.post("/api/chat/<int:aid>/name")
    @login_required
    def chat_name(aid):
        """Prikazno ime (global_name) - prazno = koristi se korisnicko ime."""
        acc, e = need(aid)
        if e:
            return e
        name = ((request.json or {}).get("global_name") or "").strip()[:32]
        data, err = call(acc, "PATCH", "/users/@me", json={"global_name": name or None})
        if err:
            return fail(err)
        remember_name(acc, data)
        return jsonify(ok=True, msg="Prikazno ime promijenjeno.")

    @app.post("/api/chat/<int:aid>/username")
    @login_required
    def chat_username(aid):
        """Korisnicko ime (@ime) - Discord trazi lozinku racuna; lozinka se ne sprema."""
        acc, e = need(aid)
        if e:
            return e
        d = request.json or {}
        username = (d.get("username") or "").strip().lower()[:32]
        password = d.get("password") or ""
        if len(username) < 2 or not password:
            return fail("Upiši novo korisničko ime i lozinku računa.")
        data, err = call(acc, "PATCH", "/users/@me", json={"username": username, "password": password})
        if err:
            return fail(err)
        if data.get("token"):                              # Discord ponekad vrati novi token - spremi ga
            q("UPDATE accounts SET token = ? WHERE id = ?", (data["token"], acc["id"]))
        remember_name(acc, data)
        return jsonify(ok=True, msg=f"Korisničko ime je sada @{data.get('username', username)}.")

    @app.post("/api/chat/<int:aid>/guilds/<gid>/nick")
    @login_required
    def chat_nick(aid, gid):
        acc, e = need(aid)
        if e:
            return e
        nick = ((request.json or {}).get("nick") or "").strip()[:32]
        data, err = call(acc, "PATCH", f"/guilds/{gid}/members/@me", json={"nick": nick or None})
        if err:
            return fail(err)
        return jsonify(ok=True, msg="Nadimak na serveru promijenjen." if nick else "Nadimak maknut.")

    # ---------- poruke ----------
    @app.get("/api/chat/<int:aid>/channels/<cid>/messages")
    @login_required
    def chat_messages(aid, cid):
        acc, e = need(aid)
        if e:
            return e
        data, err = call(acc, "GET", f"/channels/{cid}/messages", params={"limit": 50})
        if err:
            return fail(err)
        me = acc.get("user_id")
        return jsonify(ok=True, messages=[simple(m, me) for m in reversed(data)])

    @app.post("/api/chat/<int:aid>/channels/<cid>/messages")
    @login_required
    def chat_send(aid, cid):
        acc, e = need(aid)
        if e:
            return e
        d = request.json or {}
        content = (d.get("content") or "").strip()
        if not content:
            return fail("Poruka je prazna.")
        body = {"content": content[:2000], "nonce": str(random.randint(10 ** 17, 10 ** 18)), "tts": False}
        if d.get("reply_to"):
            body["message_reference"] = {"channel_id": cid, "message_id": str(d["reply_to"])}
        data, err = call(acc, "POST", f"/channels/{cid}/messages", json=body)
        if err:
            return fail(err)
        return jsonify(ok=True, message=simple(data, acc.get("user_id")))

    @app.patch("/api/chat/<int:aid>/channels/<cid>/messages/<mid>")
    @login_required
    def chat_edit(aid, cid, mid):
        acc, e = need(aid)
        if e:
            return e
        content = ((request.json or {}).get("content") or "").strip()
        if not content:
            return fail("Poruka je prazna.")
        data, err = call(acc, "PATCH", f"/channels/{cid}/messages/{mid}", json={"content": content[:2000]})
        return fail(err) if err else jsonify(ok=True)

    @app.delete("/api/chat/<int:aid>/channels/<cid>/messages/<mid>")
    @login_required
    def chat_delete(aid, cid, mid):
        acc, e = need(aid)
        if e:
            return e
        data, err = call(acc, "DELETE", f"/channels/{cid}/messages/{mid}")
        return fail(err) if err else jsonify(ok=True)

    @app.post("/api/chat/<int:aid>/channels/<cid>/messages/<mid>/react")
    @login_required
    def chat_react(aid, cid, mid):
        acc, e = need(aid)
        if e:
            return e
        d = request.json or {}
        emoji = (d.get("emoji") or "").strip()
        if not emoji:
            return fail("Nema emojija.")
        # custom emoji servera: ime:id
        key = quote(f"{emoji}:{d['id']}" if d.get("id") else emoji, safe=":")
        method = "DELETE" if d.get("remove") else "PUT"
        data, err = call(acc, method, f"/channels/{cid}/messages/{mid}/reactions/{key}/@me")
        return fail(err) if err else jsonify(ok=True)

    @app.post("/api/chat/<int:aid>/channels/<cid>/typing")
    @login_required
    def chat_typing(aid, cid):
        acc, e = need(aid)
        if e:
            return e
        call(acc, "POST", f"/channels/{cid}/typing")
        return jsonify(ok=True)
