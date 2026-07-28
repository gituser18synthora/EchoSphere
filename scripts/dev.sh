#!/usr/bin/env bash
# EchoSphere — local development environment orchestrator.
#
#   ./scripts/dev.sh [start|stop|restart|status|logs]     (default: start)
#
# Starts the Platform API, voice worker, ingestion worker, MCP server, the
# Vaani telephony gateway and the Vite frontend. Ports are read from .env
# (the single source of truth); every service's own entry point also reads
# .env, so no port is ever passed on a command line here.
#
# PID files:  .devrun/pids/<service>.pid   (one per service, only these are
#             ever signalled — no pkill/killall style process matching)
# Log files:  .devrun/logs/<service>.log

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
RUN_DIR="$ROOT_DIR/.devrun"
PID_DIR="$RUN_DIR/pids"
LOG_DIR="$RUN_DIR/logs"
PY="$ROOT_DIR/env/bin/python"

# ── configuration (only the keys we need; secrets are never read) ─────────
env_lookup() { # env_lookup KEY DEFAULT — last assignment wins; CRLF-safe
    local line
    line="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
    if [ -n "$line" ]; then printf '%s' "${line#*=}" | tr -d '\r"'; else printf '%s' "$2"; fi
}

API_PORT="$(env_lookup API_PORT 9001)"
VOICE_WORKER_PORT="$(env_lookup VOICE_WORKER_PORT 9002)"
MCP_PORT="$(env_lookup MCP_PORT 9003)"
MCP_ENABLED="$(env_lookup MCP_ENABLED true | tr '[:upper:]' '[:lower:]')"
FRONTEND_PORT="$(env_lookup FRONTEND_PORT 5199)"
GATEWAY_PORT="$(env_lookup TELEPHONY_GATEWAY_PORT 9011)"

SERVICES=(api voice-worker ingestion mcp gateway frontend)

service_cmd() {
    case "$1" in
        api)          echo "$PY -m backend.main" ;;
        voice-worker) echo "$PY -m voice_runtime.app" ;;
        ingestion)    echo "$PY -m backend.workers.ingestion" ;;
        mcp)          echo "$PY -m backend.mcp_server.server" ;;
        gateway)      echo "$PY -m voice_runtime.gateway" ;;
        # `node …/vite.js` instead of `npm run dev`: the .bin/vite shim breaks
        # when node_modules is synced from Windows (CRLF + lost exec bit).
        # vite.config.ts itself reads FRONTEND_PORT/API_PORT from .env.
        frontend)     echo "node node_modules/vite/bin/vite.js" ;;
    esac
}

service_port() {
    case "$1" in
        api)          echo "$API_PORT" ;;
        voice-worker) echo "$VOICE_WORKER_PORT" ;;
        ingestion)    echo "" ;;
        mcp)          echo "$MCP_PORT" ;;
        gateway)      echo "$GATEWAY_PORT" ;;
        frontend)     echo "$FRONTEND_PORT" ;;
    esac
}

service_health_url() {
    case "$1" in
        api)          echo "http://127.0.0.1:$API_PORT/api/health" ;;
        voice-worker) echo "http://127.0.0.1:$VOICE_WORKER_PORT/health" ;;
        mcp)          echo "http://127.0.0.1:$MCP_PORT/health" ;;
        gateway)      echo "http://127.0.0.1:$GATEWAY_PORT/health" ;;
        frontend)     echo "http://127.0.0.1:$FRONTEND_PORT/" ;;
        *)            echo "" ;;
    esac
}

service_match() { # unique cmdline token — guards against recycled PIDs
    case "$1" in
        api)          echo "backend.main" ;;
        voice-worker) echo "voice_runtime.app" ;;
        ingestion)    echo "backend.workers.ingestion" ;;
        mcp)          echo "backend.mcp_server.server" ;;
        gateway)      echo "voice_runtime.gateway" ;;
        frontend)     echo "vite" ;;
    esac
}

service_enabled() {
    case "$1" in
        mcp) [ "$MCP_ENABLED" = "true" ] ;;
        *)   return 0 ;;
    esac
}

# ── helpers ────────────────────────────────────────────────────────────────
info() { printf '%s\n' "$*"; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

pid_file() { echo "$PID_DIR/$1.pid"; }
log_file() { echo "$LOG_DIR/$1.log"; }

pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

pid_is_service() { # pid_is_service PID SERVICE — cmdline sanity check
    tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null | grep -qF "$(service_match "$2")"
}

read_pid() { cat "$(pid_file "$1")" 2>/dev/null | tr -dc '0-9'; }

port_in_use() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | grep -qE "[:.]$1\$"
    else # fallback: try to connect
        (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
        return 1
    fi
}

service_running() { # RUNNING = live pid from OUR pid file with matching cmdline
    local pid; pid="$(read_pid "$1")"
    pid_alive "$pid" && pid_is_service "$pid" "$1"
}

require_tools() {
    local missing=0
    [ -x "$PY" ] || { err "env/bin/python not found — create the venv first (see README Setup)"; missing=1; }
    [ -x "$ROOT_DIR/env/bin/uvicorn" ] || { err "env/bin/uvicorn not found — pip install -r requirements.txt"; missing=1; }
    command -v node >/dev/null 2>&1 || { err "node is not on PATH"; missing=1; }
    command -v npm  >/dev/null 2>&1 || { err "npm is not on PATH"; missing=1; }
    command -v curl >/dev/null 2>&1 || { err "curl is not on PATH (needed for readiness checks)"; missing=1; }
    [ -f "$ENV_FILE" ] || { err ".env not found at $ENV_FILE"; missing=1; }
    [ -f "$ROOT_DIR/node_modules/vite/bin/vite.js" ] || { err "node_modules missing — run npm install"; missing=1; }
    [ "$missing" -eq 0 ] || exit 1
}

wait_http() { # wait_http URL TIMEOUT_SECONDS
    local url="$1" deadline=$(( $(date +%s) + $2 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        curl -sf -o /dev/null --max-time 3 "$url" && return 0
        sleep 1
    done
    return 1
}

stop_one() { # graceful TERM → KILL, pid-file-scoped only
    local svc="$1" pid; pid="$(read_pid "$svc")"
    if [ -z "$pid" ]; then
        info "  $svc: no pid file — nothing to stop"
        return 0
    fi
    if ! pid_alive "$pid"; then
        info "  $svc: stale pid $pid removed"
        rm -f "$(pid_file "$svc")"
        return 0
    fi
    if ! pid_is_service "$pid" "$svc"; then
        err "  $svc: pid $pid is a different process now — refusing to signal it (removing stale pid file)"
        rm -f "$(pid_file "$svc")"
        return 0
    fi
    kill -TERM "$pid" 2>/dev/null
    local waited=0
    while pid_alive "$pid" && [ "$waited" -lt 10 ]; do sleep 1; waited=$((waited + 1)); done
    if pid_alive "$pid"; then
        kill -KILL "$pid" 2>/dev/null
        info "  $svc: pid $pid force-killed"
    else
        info "  $svc: pid $pid stopped"
    fi
    rm -f "$(pid_file "$svc")"
}

STARTED=()

rollback() {
    [ "${#STARTED[@]}" -gt 0 ] || return 0
    err "rolling back already-started services: ${STARTED[*]}"
    local i
    for (( i=${#STARTED[@]}-1; i>=0; i-- )); do stop_one "${STARTED[$i]}"; done
}

# ── commands ───────────────────────────────────────────────────────────────
cmd_start() {
    require_tools
    mkdir -p "$PID_DIR" "$LOG_DIR"
    cd "$ROOT_DIR"

    local svc pid port cmd log
    for svc in "${SERVICES[@]}"; do
        service_enabled "$svc" || { info "  $svc: disabled (.env) — skipped"; continue; }
        if service_running "$svc"; then
            info "  $svc: already running (pid $(read_pid "$svc")) — not starting a duplicate"
            continue
        fi
        rm -f "$(pid_file "$svc")" # dead or foreign pid → clear
        port="$(service_port "$svc")"
        if [ -n "$port" ] && port_in_use "$port"; then
            err "$svc: port $port is already in use by a process this script did not start"
            rollback
            exit 1
        fi
        cmd="$(service_cmd "$svc")"
        log="$(log_file "$svc")"
        # PYTHONUNBUFFERED=1 → immediate log lines; nohup → survives terminal
        # loss; the process stays our child so `wait` below keeps us attached.
        nohup env PYTHONUNBUFFERED=1 $cmd >>"$log" 2>&1 &
        pid=$!
        echo "$pid" >"$(pid_file "$svc")"
        STARTED+=("$svc")
        sleep 1
        if ! pid_alive "$pid"; then
            err "$svc failed to start — last log lines ($log):"
            tail -n 15 "$log" >&2 || true
            rm -f "$(pid_file "$svc")"
            rollback
            exit 1
        fi
        info "  $svc: started (pid $pid${port:+, port $port}) → $log"
    done

    info ""
    info "Readiness checks:"
    local url ok=1
    for svc in "${SERVICES[@]}"; do
        service_enabled "$svc" || continue
        url="$(service_health_url "$svc")"
        if [ -z "$url" ]; then # ingestion worker: no HTTP surface
            if service_running "$svc"; then info "  $svc: RUNNING (no HTTP endpoint)"; else info "  $svc: NOT RUNNING"; ok=0; fi
            continue
        fi
        if wait_http "$url" 60; then
            info "  $svc: READY  $url"
        else
            err "  $svc: NOT READY after 60s ($url) — check $(log_file "$svc")"
            ok=0
        fi
    done

    info ""
    printf '  %-14s %-8s %-7s %s\n' "SERVICE" "PID" "PORT" "LOG"
    for svc in "${SERVICES[@]}"; do
        service_enabled "$svc" || continue
        printf '  %-14s %-8s %-7s %s\n' "$svc" "$(read_pid "$svc")" \
            "$(service_port "$svc")" "$(log_file "$svc")"
    done
    info ""
    [ "$ok" -eq 1 ] && info "All services are up." || err "some services are not ready — see logs above"
    info "Press Ctrl+C to stop everything (or run: $0 stop from another shell)."

    trap 'info ""; info "Shutting down…"; cmd_stop; exit 0' INT TERM
    wait   # stay attached to the children; returns when they are all gone
    info "All services have exited."
}

cmd_stop() {
    trap - INT TERM
    local i
    # reverse order: frontend first, platform API last
    for (( i=${#SERVICES[@]}-1; i>=0; i-- )); do stop_one "${SERVICES[$i]}"; done
}

cmd_status() {
    local svc pid port state detail
    printf '%-14s %-14s %-8s %-7s %s\n' "SERVICE" "STATE" "PID" "PORT" "DETAIL"
    for svc in "${SERVICES[@]}"; do
        pid="$(read_pid "$svc")"
        port="$(service_port "$svc")"
        state="STOPPED"; detail=""
        if [ -n "$pid" ] && pid_alive "$pid" && pid_is_service "$pid" "$svc"; then
            state="RUNNING"
        elif [ -n "$pid" ] && pid_alive "$pid"; then
            state="STALE PID"; detail="pid $pid belongs to another process"
        elif [ -n "$pid" ]; then
            state="STALE PID"; detail="pid $pid is not running"
        fi
        if [ "$state" != "RUNNING" ] && [ -n "$port" ] && port_in_use "$port"; then
            state="PORT OCCUPIED"; detail="port $port in use by a foreign process"
        fi
        if ! service_enabled "$svc"; then detail="disabled via .env"; fi
        printf '%-14s %-14s %-8s %-7s %s\n' "$svc" "$state" "${pid:--}" "${port:--}" "$detail"
    done
}

cmd_logs() {
    local svc log tails=()
    trap 'kill "${tails[@]}" 2>/dev/null; exit 0' INT TERM
    for svc in "${SERVICES[@]}"; do
        log="$(log_file "$svc")"
        [ -f "$log" ] || continue
        tail -n 10 -F "$log" 2>/dev/null | sed -u "s/^/[$svc] /" &
        tails+=("$!")
    done
    [ "${#tails[@]}" -gt 0 ] || { err "no log files yet under $LOG_DIR"; exit 1; }
    wait
}

case "${1:-start}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_stop; cmd_start ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    *)       err "usage: $0 [start|stop|restart|status|logs]"; exit 2 ;;
esac
