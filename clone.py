# Backup strukture servera (rangovi, kategorije, kanali, dozvole) i uvoz na vlastiti server preko racuna iz panela.
# Ne kopira poruke, opise kanala, ime / ikonu / emojije servera; preskace kanale za tikete.
import json
import re
import threading
import time
import uuid

import requests
from flask import Response, jsonify, request

API = "https://discord.com/api/v9"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TICKET = re.compile(r"(ticket|tiket|tick|closed|zatvoren|transcript|transkript|support-\d|podrska-\d|-\d{3,}$)", re.I)
KEEP_TYPES = (0, 2, 4, 5, 13, 15)      # tekst, glas, kategorija, obavijesti, stage, forum
jobs = {}                              # id uvoza -> {"log": [...], "done": bool}


def register(app, q, login_required):
    # ---------- backup ----------
    @app.get("/api/chat/<int:aid>/guilds/<gid>/backup")
    @login_required
    def guild_backup(aid, gid):
        row = q("SELECT token FROM accounts WHERE id = ?", (aid,), one=True)
        if not row:
            return jsonify(ok=False, msg="Nema tog računa."), 404
        h = {"Authorization": row["token"], "User-Agent": UA}
        rr = requests.get(f"{API}/guilds/{gid}/roles", headers=h, timeout=15)
        rc = requests.get(f"{API}/guilds/{gid}/channels", headers=h, timeout=15)
        if rr.status_code != 200 or rc.status_code != 200:
            return jsonify(ok=False, msg=f"Discord nije dao podatke ({rr.status_code}/{rc.status_code})."), 400
        roles, chans = rr.json(), rc.json()

        out_roles = []
        for r in sorted(roles, key=lambda r: r["position"], reverse=True):
            if r.get("managed") or (r.get("tags") or {}).get("bot_id"):
                continue                      # rang nekog bota - dobije ga bot kad ga dodas
            role = {"key": r["id"], "name": r["name"], "color": r.get("color", 0), "hoist": r.get("hoist", False),
                    "mentionable": r.get("mentionable", False), "permissions": r.get("permissions", "0"),
                    "everyone": r["id"] == gid}
            # gradient / holografska boja imena (Discord "colors": primary, secondary, tertiary)
            c = r.get("colors") or {}
            if c.get("secondary_color") is not None:
                role["colors"] = {"primary_color": c.get("primary_color", role["color"]),
                                  "secondary_color": c.get("secondary_color"),
                                  "tertiary_color": c.get("tertiary_color")}
            out_roles.append(role)
        role_keys = {r["key"] for r in out_roles}

        def overwrites(c):
            return [{"role": o["id"], "allow": o.get("allow", "0"), "deny": o.get("deny", "0")}
                    for o in c.get("permission_overwrites") or [] if o.get("type") == 0 and o["id"] in role_keys]

        cats = {c["id"]: c for c in chans if c.get("type") == 4}
        skip_cats = {cid for cid, c in cats.items() if TICKET.search(c["name"])}
        out_cats = [{"key": c["id"], "name": c["name"], "position": c.get("position", 0), "overwrites": overwrites(c)}
                    for c in sorted(cats.values(), key=lambda c: c.get("position", 0)) if c["id"] not in skip_cats]
        out_ch, skipped = [], 0
        for c in sorted(chans, key=lambda c: c.get("position", 0)):
            t = c.get("type")
            if t == 4 or t not in KEEP_TYPES:
                continue
            if c.get("parent_id") in skip_cats or TICKET.search(c["name"]):
                skipped += 1
                continue
            out_ch.append({"name": c["name"], "type": t, "parent": c.get("parent_id"), "position": c.get("position", 0),
                           "nsfw": c.get("nsfw", False), "slowmode": c.get("rate_limit_per_user", 0),
                           "user_limit": c.get("user_limit", 0), "bitrate": c.get("bitrate", 64000),
                           "overwrites": overwrites(c)})
        data = {"version": 1, "made": int(time.time()), "roles": out_roles, "categories": out_cats, "channels": out_ch,
                "skipped_tickets": skipped}
        return Response(json.dumps(data, ensure_ascii=False, indent=1), mimetype="application/json",
                        headers={"Content-Disposition": f'attachment; filename="server-backup-{gid}.json"'})

    # ---------- uvoz (sluzbeni bot) ----------
    @app.post("/api/clone/import")
    @login_required
    def clone_import():
        d = request.json or {}
        row = q("SELECT token FROM accounts WHERE id = ?", (int(d.get("account_id") or 0),), one=True)
        gid = str(d.get("guild_id") or "").strip()
        data = d.get("data") or {}
        if not row or not gid.isdigit() or not isinstance(data, dict) or data.get("version") != 1:
            return jsonify(ok=False, msg="Odaberi račun, svoj server i ispravnu backup datoteku."), 400
        h = {"Authorization": row["token"], "User-Agent": UA}
        g = requests.get(f"{API}/guilds/{gid}", headers=h, timeout=10)
        if g.status_code != 200:
            return jsonify(ok=False, msg="Račun nije na tom serveru."), 400
        if any(j for j in jobs.values() if not j["done"]):
            return jsonify(ok=False, msg="Jedan uvoz već traje - pričekaj da završi."), 400
        jid = uuid.uuid4().hex[:10]
        only_colors = d.get("mode") == "colors"
        jobs[jid] = {"log": [f"{'Boje rangova' if only_colors else 'Uvoz'} na server: {g.json().get('name')} (polako, ~1 s po stavci)"], "done": False}
        threading.Thread(target=run_colors if only_colors else run_import, args=(jid, h, gid, data), daemon=True).start()
        return jsonify(ok=True, job=jid)

    @app.get("/api/clone/import/<jid>")
    @login_required
    def clone_status(jid):
        j = jobs.get(jid)
        if not j:
            return jsonify(ok=False, msg="Nema tog uvoza."), 404
        return jsonify(ok=True, log=j["log"][-200:], done=j["done"])


def api_caller(h):
    def call(method, path, body=None):
        for _ in range(6):
            r = requests.request(method, API + path, headers={**h, "Content-Type": "application/json"},
                                 data=json.dumps(body) if body is not None else None, timeout=20)
            if r.status_code == 429:
                time.sleep(float((r.json() or {}).get("retry_after", 2)) + 0.3)
                continue
            if r.status_code == 401:
                raise RuntimeError("token računa više ne vrijedi")
            if r.status_code >= 400:
                try:
                    return None, (r.json() or {}).get("message") or str(r.status_code)
                except Exception:
                    return None, str(r.status_code)
            time.sleep(1.0)
            return (r.json() if r.content else {}), None
        return None, "previše čekanja (rate limit)"
    return call


def run_colors(jid, h, gid, data):
    """Samo boje: rangovi iz backupa -> postojeci rangovi ISTOG IMENA na serveru dobiju boju (i gradient). Nista se ne stvara."""
    job = jobs[jid]
    log = job["log"].append
    call = api_caller(h)
    try:
        mine, err = call("GET", f"/guilds/{gid}/roles")
        if err:
            raise RuntimeError(err)
        by_name = {}
        for r in mine:
            by_name.setdefault(r["name"].strip().lower(), []).append(r)
        done = missing = 0
        for r in data.get("roles") or []:
            if r.get("everyone") or (not r.get("color") and not r.get("colors")):
                continue                                   # bez boje - nema sto prenijeti
            targets = by_name.get(r["name"].strip().lower())
            if not targets:
                missing += 1
                continue
            for t in targets:
                if t.get("managed"):
                    continue
                body, note = {"color": r.get("color", 0)}, ""
                if r.get("colors"):
                    body["colors"] = r["colors"]
                res, err = call("PATCH", f"/guilds/{gid}/roles/{t['id']}", body)
                if err and r.get("colors"):
                    body.pop("colors", None)
                    res, err = call("PATCH", f"/guilds/{gid}/roles/{t['id']}", body)
                    note = " (gradient preskočen - treba Boost razina 2)"
                elif r.get("colors"):
                    note = " (gradient)"
                if err:
                    log(f"{t['name']} - greška: {err}")
                else:
                    done += 1
                    log(f"{t['name']} ✓{note}")
        log(f"Gotovo. Obojano rangova: {done}. Iz backupa bez ranga istog imena kod tebe: {missing}.")
    except Exception as e:
        log(f"Prekinuto: {e}")
    job["done"] = True


def run_import(jid, h, gid, data):
    job = jobs[jid]
    log = job["log"].append

    def call(method, path, body=None):
        for _ in range(6):
            r = requests.request(method, API + path, headers={**h, "Content-Type": "application/json"},
                                 data=json.dumps(body) if body is not None else None, timeout=20)
            if r.status_code == 429:
                time.sleep(float((r.json() or {}).get("retry_after", 2)) + 0.3)
                continue
            if r.status_code == 401:
                raise RuntimeError("token računa više ne vrijedi")
            if r.status_code >= 400:
                try:
                    return None, (r.json() or {}).get("message") or str(r.status_code)
                except Exception:
                    return None, str(r.status_code)
            time.sleep(1.0)                    # racun, ne bot - polako, da ne izgleda kao automatika
            return (r.json() if r.content else {}), None
        return None, "previše čekanja (rate limit)"

    try:
        role_map = {}
        roles = data.get("roles") or []
        # @everyone: samo dozvole
        for r in roles:
            if r.get("everyone"):
                _, err = call("PATCH", f"/guilds/{gid}/roles/{gid}", {"permissions": r["permissions"]})
                role_map[r["key"]] = gid
                log("@everyone dozvole" + (f" - greška: {err}" if err else " ✓"))
        # rangovi od najviseg prema najnizem - svaki novi ide ispod prethodnog
        for r in roles:
            if r.get("everyone"):
                continue
            body = {"name": r["name"], "color": r.get("color", 0), "hoist": r.get("hoist", False),
                    "mentionable": r.get("mentionable", False), "permissions": r.get("permissions", "0")}
            note = ""
            if r.get("colors"):
                body["colors"] = r["colors"]
            res, err = call("POST", f"/guilds/{gid}/roles", body)
            if err and r.get("colors"):
                # gradient imena traze Boost razinu 2 - bez nje rang dobije obicnu boju
                body.pop("colors", None)
                res, err = call("POST", f"/guilds/{gid}/roles", body)
                note = " (gradient preskočen - treba Boost razina 2)"
            elif r.get("colors"):
                note = " (gradient)"
            if err:
                log(f"Rang {r['name']} - greška: {err}")
                continue
            role_map[r["key"]] = res["id"]
            log(f"Rang {r['name']} ✓{note}")

        def ow(lst):
            return [{"id": role_map[o["role"]], "type": 0, "allow": o["allow"], "deny": o["deny"]}
                    for o in lst or [] if o["role"] in role_map]

        cat_map = {}
        for c in data.get("categories") or []:
            res, err = call("POST", f"/guilds/{gid}/channels", {"name": c["name"], "type": 4, "position": c.get("position", 0),
                                                                 "permission_overwrites": ow(c.get("overwrites"))})
            if err:
                log(f"Kategorija {c['name']} - greška: {err}")
                continue
            cat_map[c["key"]] = res["id"]
            log(f"Kategorija {c['name']} ✓")

        for c in data.get("channels") or []:
            t = c["type"]
            body = {"name": c["name"], "type": t, "position": c.get("position", 0), "permission_overwrites": ow(c.get("overwrites"))}
            if c.get("parent") in cat_map:
                body["parent_id"] = cat_map[c["parent"]]
            if t in (0, 5, 15):
                body["nsfw"] = bool(c.get("nsfw"))
                if t != 5:
                    body["rate_limit_per_user"] = int(c.get("slowmode") or 0)
            if t in (2, 13):
                body["bitrate"] = min(int(c.get("bitrate") or 64000), 96000)    # vise od 96 kbps samo uz boost
                body["user_limit"] = int(c.get("user_limit") or 0)
            res, err = call("POST", f"/guilds/{gid}/channels", body)
            if err and t in (5, 13, 15):
                # obavijesti / stage / forum traze Community server - napravi obican kanal
                body["type"] = 2 if t == 13 else 0
                res, err = call("POST", f"/guilds/{gid}/channels", body)
                if not err:
                    log(f"#{c['name']} ✓ (kao običan kanal - uključi Community za pravi tip)")
                    continue
            log(f"{'🔊' if t in (2, 13) else '#'}{c['name']}" + (f" - greška: {err}" if err else " ✓"))
        log(f"Gotovo. Rangova: {len(role_map)}, kategorija: {len(cat_map)}, kanala: {len(data.get('channels') or [])}.")
    except Exception as e:
        log(f"Prekinuto: {e}")
    job["done"] = True
