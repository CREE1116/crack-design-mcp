#!/usr/bin/env bash
set -euo pipefail

# One-command local Crack Design MCP + Web UI + temporary HTTPS tunnel for ChatGPT / Claude.

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
PROJECT="${CRACK_PROJECT:-${CRACK_WORKSPACE:-$HOME/crack}/마왕성주식회사/build}"
STORE="${CRACK_STORE:-${CRACK_STATE:-$HOME/.crack-emu}/sessions}"
PORT="${CRACK_MCP_PORT:-8787}"
TOKEN="${CRACK_MCP_AUTH_TOKEN:-}"
TMP_DIR="$(mktemp -d -t crack-design-mcp)"
SERVER_LOG="$TMP_DIR/server.log"
TUNNEL_LOG="$TMP_DIR/tunnel.log"

cleanup() {
  trap - EXIT INT TERM
  [[ -n "${TUNNEL_PID:-}" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
  [[ -n "${SERVER_PID:-}" ]] && kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

command -v cloudflared >/dev/null || {
  echo "cloudflared missing. Install: brew install cloudflared" >&2
  exit 2
}

AUTH_ARGS=""
if [[ -n "$TOKEN" ]]; then
  AUTH_ARGS="--auth-token $TOKEN"
fi

PYTHONPATH="$SCRIPT_DIR" \
  python3 -m crack_design --project "$PROJECT" --store "$STORE" \
  http --host 127.0.0.1 --port "$PORT" $AUTH_ARGS \
  --cors-origin "${CRACK_MCP_CORS_ORIGIN:-*}" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

for _ in {1..50}; do
  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done
curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null || {
  echo "MCP server failed. Log: $SERVER_LOG" >&2
  exit 1
}

cloudflared tunnel --url "http://127.0.0.1:${PORT}" >"$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

PUBLIC_URL=""
for _ in {1..60}; do
  PUBLIC_URL="$(sed -nE 's/.*(https:\/\/[^ ]+\.trycloudflare\.com).*/\1/p' "$TUNNEL_LOG" | head -1)"
  [[ -n "$PUBLIC_URL" ]] && break
  sleep 0.5
done
if [[ -z "$PUBLIC_URL" ]]; then
  echo "Tunnel failed. Log: $TUNNEL_LOG" >&2
  exit 1
fi

MCP_URL="${PUBLIC_URL}/mcp"

cat <<BANNER
============================================================
  Crack Design MCP & Emulator Live
============================================================
  Project : $PROJECT
  Web UI  : http://127.0.0.1:${PORT}/
  Remote  : $PUBLIC_URL/
  MCP URL : $MCP_URL
============================================================
BANNER

wait "$SERVER_PID"
