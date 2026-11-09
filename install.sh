#!/bin/bash
# Status Panel (vise Discord tokena, web panel) - instalacija na Linux VPS (Ubuntu / Debian), kao root:
#   cd /root/panel && bash install.sh
# Pita lozinku panela i port, instalira sve i pokrene kao servis 24/7. Panel: http://IP-VPS-a:PORT
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DIR=/opt/discord-panel

echo "== Paketi =="
apt-get update -y
apt-get install -y python3 python3-venv python3-pip

echo "== Datoteke =="
mkdir -p "$DIR"
cp "$SRC/app.py" "$SRC/worker.py" "$SRC/chat.py" "$SRC/clone.py" "$SRC/join.py" "$SRC/index.html" "$SRC/requirements.txt" "$DIR/"

echo "== Python okruzenje =="
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --upgrade pip
"$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"

if [ ! -f "$DIR/config.json" ]; then
    echo "== Postavke panela =="
    read -s -p "Lozinka panela (ne prikazuje se): " PASS; echo
    read -p "Port panela [8090]: " PORT
    PORT=${PORT:-8090}
    PASS="$PASS" PORT="$PORT" "$DIR/venv/bin/python" - <<'EOF'
import json, os, secrets
from werkzeug.security import generate_password_hash
json.dump({"password_hash": generate_password_hash(os.environ["PASS"]), "secret": secrets.token_hex(32), "port": int(os.environ["PORT"])},
          open("/opt/discord-panel/config.json", "w"))
EOF
    chmod 600 "$DIR/config.json"
else
    PORT=$(python3 -c "import json;print(json.load(open('$DIR/config.json')).get('port',8090))")
    echo "== Postojece postavke zadrzane (config.json) =="
fi

# ---------- Ulazak na server (Discord OAuth2 guilds.join) ----------
# Client Secret aplikacije: iz varijable OAUTH_SECRET (OAUTH_SECRET=... bash install.sh) ili rucno "bash install.sh oauth"; bot token i Client ID panel uzima od
# NextGaming bota (/opt/nextgaming-bot/config.json). Rucno mijenjanje:  bash install.sh oauth
OAUTH_SECRET="${OAUTH_SECRET:-}"   # NE upisuj tajnu u kod - postavi je pri pokretanju
IP=$(curl -s -4 ifconfig.me || hostname -I | awk '{print $1}')
OSEC="$OAUTH_SECRET" OURL="http://$IP:$PORT" "$DIR/venv/bin/python" - <<'EOF'
import json, os
p = "/opt/discord-panel/config.json"
c = json.load(open(p))
if not c.get("oauth_client_secret") and os.environ.get("OSEC"):
    c["oauth_client_secret"] = os.environ["OSEC"]
if not c.get("public_url"):
    c["public_url"] = os.environ["OURL"]
json.dump(c, open(p, "w"))
EOF
chmod 600 "$DIR/config.json"
echo "== OAuth: U Developer Portalu -> OAuth2 -> Redirects dodaj TOCNO: http://$IP:$PORT/oauth/callback =="

if [ "$1" = "oauth" ]; then
    IP=$(curl -s -4 ifconfig.me || hostname -I | awk '{print $1}')
    echo
    echo "== Ulazak na server (OAuth) - Discord Developer Portal -> tvoja aplikacija =="
    echo "   OAuth2 -> Redirects dodaj TOCNO:  http://$IP:$PORT/oauth/callback"
    echo "   Bot mora biti na serveru i imati dozvolu 'Create Instant Invite'."
    echo "   (prazno + Enter = preskoci, ostaje kako je)"
    read -p "Client ID (OAuth2 -> Client ID): " OID
    read -s -p "Client Secret (OAuth2 -> Reset Secret, ne prikazuje se): " OSEC; echo
    read -s -p "Bot token (Bot -> Reset Token, ne prikazuje se): " OBOT; echo
    read -p "Javni URL panela [http://$IP:$PORT]: " OURL
    OURL=${OURL:-http://$IP:$PORT}
    OID="$OID" OSEC="$OSEC" OBOT="$OBOT" OURL="$OURL" "$DIR/venv/bin/python" - <<'EOF'
import json, os
p = "/opt/discord-panel/config.json"
c = json.load(open(p))
for k, e in (("oauth_client_id", "OID"), ("oauth_client_secret", "OSEC"), ("bot_token", "OBOT"), ("public_url", "OURL")):
    v = os.environ.get(e, "").strip()
    if v:
        c[k] = v.rstrip("/") if k == "public_url" else v
json.dump(c, open(p, "w"))
print("OAuth postavke spremljene." if all(c.get(k) for k in ("oauth_client_id", "oauth_client_secret", "bot_token")) else "OAuth jos nije potpun - ulazak na server ce cekati (bash install.sh oauth).")
EOF
    chmod 600 "$DIR/config.json"
fi

# stara skripta s jednim tokenom (ako postoji) - ugasi da isti racun ne bude spojen dvaput
if systemctl list-unit-files | grep -q '^discord-status.service'; then
    systemctl disable --now discord-status || true
    echo "Stara skripta (discord-status) je ugasena - racune dodaj u panel."
fi

echo "== Servis =="
cat > /etc/systemd/system/discord-panel.service <<EOF
[Unit]
Description=Discord Status Panel
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable discord-panel
systemctl restart discord-panel

# vatrozid: otvori port panela ako je ufw ukljucen
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow "$PORT"/tcp
fi

sleep 3
systemctl --no-pager status discord-panel | head -n 12
IP=$(curl -s -4 ifconfig.me || hostname -I | awk '{print $1}')
echo
echo "Gotovo. Panel:  http://$IP:$PORT"
echo "Zapisi uzivo:   journalctl -u discord-panel -f"
echo "Nova lozinka:   rm $DIR/config.json && bash install.sh"
echo "OAuth (ulazak): bash install.sh oauth"
