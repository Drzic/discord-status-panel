# Discord Status Panel

Web panel za upravljanje više Discord računa s jednog mjesta (Python / Flask), radi 24/7 kao servis na Linux VPS-u.

**Mogućnosti**
- custom status i platforma po računu, rotacija statusa
- chat (poruke) kroz panel
- backup i uvoz strukture servera (kanali, uloge)
- ulazak računa na server preko Discord OAuth2 (`guilds.join`)

## Instalacija (Ubuntu / Debian, kao root)
```bash
cd /root/panel && bash install.sh
```
Skripta pita lozinku panela i port (zadano 8090), instalira sve i pokrene servis `discord-panel`.
Panel: `http://IP-VPS-a:PORT`

**OAuth (ulazak na server):** `bash install.sh oauth` (pita Client ID, Client Secret, bot token),
ili Client Secret proslijedi pri instalaciji: `OAUTH_SECRET=... bash install.sh`.
U Developer Portalu -> OAuth2 -> Redirects dodaj `http://IP:PORT/oauth/callback`.

**Zapisi:** `journalctl -u discord-panel -f`

> `config.json` (lozinka, tokeni) se ne sprema u repo.