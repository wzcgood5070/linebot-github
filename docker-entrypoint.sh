#!/usr/bin/env bash
set -Eeuo pipefail

app=(python -m uvicorn app.main:app --host 0.0.0.0 --port 8000)

if [[ -z "${CLOUDFLARE_TUNNEL_TOKEN:-}" || "${CLOUDFLARE_TUNNEL_TOKEN}" == "replace_me" ]]; then
  echo "CLOUDFLARE_TUNNEL_TOKEN is not configured; starting the bot without a public tunnel."
  exec "${app[@]}"
fi

cleanup() {
  kill "${app_pid:-}" "${tunnel_pid:-}" 2>/dev/null || true
  wait "${app_pid:-}" "${tunnel_pid:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"${app[@]}" &
app_pid=$!
cloudflared tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN" &
tunnel_pid=$!

# The bot and tunnel are one service: if either stops, exit so Docker restarts both.
wait -n "$app_pid" "$tunnel_pid"
