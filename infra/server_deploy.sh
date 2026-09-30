#!/usr/bin/env bash
# Runs on the server as root, via deploy.sh. Idempotent.
set -euo pipefail
: "${APP:?}" "${DOMAIN:?}" "${PORT:?}" "${VAULT_REPO:?}" "${WIKICORE_REPO:?}" "${WIKICORE_REF:=main}"
HOME_DIR="/opt/$APP"

id -u "$APP" >/dev/null 2>&1 || useradd --system --home-dir "$HOME_DIR" --create-home --shell /usr/sbin/nologin "$APP"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

runuser -u "$APP" -- bash -euo pipefail -c "
  cd '$HOME_DIR'
  [ -d code/.git ] || git clone -q '$WIKICORE_REPO' code
  git -C code fetch -q origin && git -C code reset -q --hard 'origin/$WIKICORE_REF'
  (cd code && /usr/local/bin/uv sync -q --no-dev)
  mkdir -p .ssh state && chmod 700 .ssh state
  [ -f .ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -f .ssh/id_ed25519 -C '$APP'
  ssh-keyscan -T 15 github.com >> .ssh/known_hosts 2>/dev/null; sort -u -o .ssh/known_hosts .ssh/known_hosts
  if [ ! -d vault/.git ]; then
    if ! git clone -q '$VAULT_REPO' vault; then
      echo '== VAULT CLONE FAILED. Add this key as a deploy key WITH WRITE ACCESS on the vault repo, then re-run:'
      cat .ssh/id_ed25519.pub
      exit 1
    fi
  fi
  [ -f .env ] || printf '%s\n' \
    'WIKICORE_VAULT=$HOME_DIR/vault' 'WIKICORE_STATE=$HOME_DIR/state' 'WIKICORE_PUBLIC_URL=https://$DOMAIN' \
    'WIKICORE_PORT=$PORT' 'WIKICORE_ALLOWED_HOSTS=$DOMAIN,127.0.0.1,localhost' '# WIKICORE_GITHUB_TOKEN=' > .env
  chmod 600 .env
"

# ExecStart runs exactly one process (`wikicore serve` uses a single uvicorn worker). Do not add
# workers or a second instance: OAuth pending requests and authorization codes are held in memory per process.
cat > "/etc/systemd/system/$APP.service" <<UNIT
[Unit]
Description=wikicore ($APP)
After=network-online.target

[Service]
User=$APP
WorkingDirectory=$HOME_DIR
EnvironmentFile=$HOME_DIR/.env
ExecStart=$HOME_DIR/code/.venv/bin/wikicore serve
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# 26m: the app accepts uploads up to 25 MB (+1 MB slack) and rejects larger bodies itself.
cat > "/etc/nginx/sites-available/$APP" <<NGINX
server {
  listen 80;
  server_name $DOMAIN;
  client_max_body_size 26m;
  location / {
    proxy_pass http://127.0.0.1:$PORT;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 3600s;
  }
}
NGINX
ln -sf "/etc/nginx/sites-available/$APP" "/etc/nginx/sites-enabled/$APP"
nginx -t && systemctl reload nginx

systemctl daemon-reload
systemctl enable -q "$APP"
if runuser -u "$APP" -- bash -c "set -a; source $HOME_DIR/.env; $HOME_DIR/code/.venv/bin/wikicore token list" | grep -q .; then
  systemctl restart "$APP"
else
  echo "== No client keys yet. Create one, then start the service:"
  echo "   runuser -u $APP -- bash -c 'set -a; source $HOME_DIR/.env; $HOME_DIR/code/.venv/bin/wikicore token add owner'"
  echo "   systemctl start $APP"
fi

# The vhost above is rewritten on every deploy, which drops the TLS lines certbot added, so always
# (re)install: with an existing certificate this only re-applies it (no new issuance).
if [ -n "${CERTBOT_EMAIL:-}" ]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect --keep-until-expiring \
    || echo "== certbot failed (DNS not pointing here yet?). Re-run deploy once it does."
fi
echo "== $APP deployed: https://$DOMAIN"
