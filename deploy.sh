#!/usr/bin/env bash
# Deploy wikicore to one server. Instance settings live in .deploy.env (never committed).
set -euo pipefail
cd "$(dirname "$0")"
[ -f .deploy.env ] || { echo "missing .deploy.env (see .deploy.env.example)"; exit 1; }
set -a; source .deploy.env; set +a
git push -q origin HEAD
ssh "$DEPLOY_HOST" "APP='$APP' DOMAIN='$DOMAIN' PORT='$PORT' VAULT_REPO='$VAULT_REPO' CERTBOT_EMAIL='${CERTBOT_EMAIL:-}' \
  WIKICORE_REPO='$WIKICORE_REPO' WIKICORE_REF='${WIKICORE_REF:-main}' bash -s" < infra/server_deploy.sh
