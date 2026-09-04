#!/usr/bin/env bash
# Crack Design MCP — server and tunnel as separate lifetimes.
#
# The tunnel URL is the thing clients are configured with, and a quick tunnel
# gets a fresh random hostname every time cloudflared restarts. Restarting the
# server to pick up a code change must therefore leave the tunnel alone, so the
# two run as independent processes and only `down` stops the tunnel.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
STATE="${CRACK_STATE:-$HOME/.crack-emu}/run"
PROJECT="${CRACK_PROJECT:-${CRACK_WORKSPACE:-$HOME/crack}/마왕성주식회사/build}"
STORE="${CRACK_STORE:-${CRACK_STATE:-$HOME/.crack-emu}/sessions}"
PORT="${CRACK_MCP_PORT:-8787}"
TOKEN="${CRACK_MCP_AUTH_TOKEN:-}"
mkdir -p "$STATE"

SRV_PID="$STATE/server.pid"; SRV_LOG="$STATE/server.log"
TUN_PID="$STATE/tunnel.pid"; TUN_LOG="$STATE/tunnel.log"; URL_FILE="$STATE/url"

alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

start_server() {
  alive "$SRV_PID" && { echo "server already up (pid $(cat "$SRV_PID"))"; return; }
  local auth=()
  [[ -n "$TOKEN" ]] && auth+=(--auth-token "$TOKEN")
  PYTHONPATH="$SCRIPT_DIR" nohup python3 -m crack_design \
    --project "$PROJECT" --store "$STORE" \
    http --host 127.0.0.1 --port "$PORT" ${auth[@]+"${auth[@]}"} \
    --cors-origin "${CRACK_MCP_CORS_ORIGIN:-*}" >"$SRV_LOG" 2>&1 &
  echo $! > "$SRV_PID"
  for _ in {1..60}; do
    curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && { echo "server up on :$PORT"; return; }
    sleep 0.25
  done
  echo "server failed to answer /health. log: $SRV_LOG" >&2; exit 1
}

stop_server() { alive "$SRV_PID" && kill "$(cat "$SRV_PID")" 2>/dev/null || true; rm -f "$SRV_PID"; }

start_tunnel() {
  alive "$TUN_PID" && { echo "tunnel already up: $(cat "$URL_FILE")"; return; }
  command -v cloudflared >/dev/null || { echo "cloudflared missing: brew install cloudflared" >&2; exit 2; }
  : > "$TUN_LOG"
  nohup cloudflared tunnel --url "http://127.0.0.1:${PORT}" >"$TUN_LOG" 2>&1 &
  echo $! > "$TUN_PID"
  for _ in {1..80}; do
    local u; u="$(sed -nE 's|.*(https://[^ ]+\.trycloudflare\.com).*|\1|p' "$TUN_LOG" | head -1)"
    [[ -n "$u" ]] && { echo "$u" > "$URL_FILE"; echo "tunnel up: $u"; return; }
    sleep 0.5
  done
  echo "tunnel failed. log: $TUN_LOG" >&2; exit 1
}

stop_tunnel() { alive "$TUN_PID" && kill "$(cat "$TUN_PID")" 2>/dev/null || true; rm -f "$TUN_PID" "$URL_FILE"; }

banner() {
  local url="(none)"; [[ -f "$URL_FILE" ]] && url="$(cat "$URL_FILE")"
  cat <<BANNER
============================================================
  Crack Design MCP
============================================================
  Project : $PROJECT
  Web UI  : http://127.0.0.1:${PORT}/
  MCP URL : ${url}/mcp
============================================================
BANNER
}

case "${1:-up}" in
  up)      start_server; start_tunnel; banner ;;
  restart) stop_server; start_server; banner   # tunnel and URL untouched
           ;;
  status)  alive "$SRV_PID" && echo "server: up" || echo "server: down"
           alive "$TUN_PID" && echo "tunnel: up $(cat "$URL_FILE" 2>/dev/null)" || echo "tunnel: down"
           curl -fsS -m 5 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 \
             && echo "health: ok" || echo "health: FAIL" ;;
  down)    stop_server; stop_tunnel; echo "stopped" ;;
  url)     cat "$URL_FILE" 2>/dev/null || { echo "no tunnel" >&2; exit 1; } ;;
  *) echo "usage: $0 {up|restart|status|down|url}" >&2; exit 64 ;;
esac
