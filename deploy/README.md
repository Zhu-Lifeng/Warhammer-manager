# VPS deployment

Auto-deploy on every push to `main` via a GitHub webhook. The core invariant:
**user data (armies, model registry, uploaded images) lives outside the repo
checkout, so `git pull` cannot ever erase it.**

```
/srv/warhammer-manager/           ← git checkout, rewritten by every deploy
└── …repo files…

/var/lib/warhammer-manager/       ← user data, never touched by deploys
├── app.db                        ← SQLite (WAL files live here too)
└── uploads/                      ← model photos
```

The app discovers these via two env vars (`WH40K_DATA_DIR` and
`WH40K_UPLOAD_DIR`), both set in `/etc/warhammer-manager.env`.

## One-time VPS setup (Debian/Ubuntu)

```bash
# 1. System packages
sudo apt update
sudo apt install -y python3-venv python3-pip git nginx

# 2. Service user + data dir
sudo useradd --system --create-home --shell /usr/sbin/nologin wh40k
sudo mkdir -p /var/lib/warhammer-manager/uploads
sudo chown -R wh40k:wh40k /var/lib/warhammer-manager

# 3. Code checkout + virtualenv
sudo mkdir -p /srv/warhammer-manager
sudo chown wh40k:wh40k /srv/warhammer-manager
sudo -u wh40k git clone https://github.com/<you>/Warhammer-manager.git /srv/warhammer-manager
sudo -u wh40k python3 -m venv /srv/warhammer-manager/venv
sudo -u wh40k /srv/warhammer-manager/venv/bin/pip install -r /srv/warhammer-manager/requirements.txt

# 4. Environment file (edit secrets first!)
sudo cp /srv/warhammer-manager/deploy/warhammer-manager.env.example /etc/warhammer-manager.env
sudo chown root:wh40k /etc/warhammer-manager.env
sudo chmod 640 /etc/warhammer-manager.env
sudo $EDITOR /etc/warhammer-manager.env

# 5. systemd units
sudo cp /srv/warhammer-manager/deploy/warhammer-manager.service /etc/systemd/system/
sudo cp /srv/warhammer-manager/deploy/warhammer-webhook.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now warhammer-manager warhammer-webhook

# 6. Sudo rule so the webhook can restart the app (and only that)
sudo install -m 440 -o root -g root \
    /srv/warhammer-manager/deploy/sudoers.d-warhammer /etc/sudoers.d/warhammer

# 7. nginx vhost (edit the server_name first)
sudo cp /srv/warhammer-manager/deploy/nginx.conf.example /etc/nginx/sites-available/warhammer-manager
sudo ln -s /etc/nginx/sites-available/warhammer-manager /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 8. TLS (optional but recommended)
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d wh40k.example.com
```

## GitHub webhook setup

Repo → **Settings → Webhooks → Add webhook**

| Field        | Value                                              |
|--------------|----------------------------------------------------|
| Payload URL  | `https://wh40k.example.com/github-webhook`         |
| Content type | `application/json`                                 |
| Secret       | same value as `WH40K_WEBHOOK_SECRET` in env file   |
| Events       | Just the **push** event                            |

GitHub sends a `ping` first — it should come back 200 with `{"pong": true}`.

## Verifying

```bash
# Live logs while you push to main
sudo journalctl -fu warhammer-webhook -u warhammer-manager

# Health probe (loopback only — adjust nginx to expose if desired)
curl http://127.0.0.1:9001/healthz
```

A successful deploy logs `[deploy] done at …` then the app service comes back
up. User data in `/var/lib/warhammer-manager` is untouched: the deploy script
does `git fetch && git reset --hard origin/<branch>` only inside `APP_DIR`.

## Local development

Nothing changes — when `WH40K_DATA_DIR` is unset the app falls back to the
repo root for `app.db` and `static/uploads/`. Both are in `.gitignore` so they
never get committed.

## Schema migrations

`init_user_db` in `app.py` is idempotent and runs on every connection. Add new
columns with `ALTER TABLE … ADD COLUMN` guarded by `PRAGMA table_info` (see
the existing `model_type_id`/`tier_label` examples). The webhook deploy runs
through `pip install` but does not run a separate migration step — schema
changes apply lazily the first time the new code opens the DB.
