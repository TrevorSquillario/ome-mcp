#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# ome_mcp_v5 service control script
# Usage: ./service_control.sh {start|stop|restart|status|logs|build|rebuild}
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="ome_mcp_v5"
CONTAINER_NAME="ome_mcp_v5"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
ENV_FILE="${SCRIPT_DIR}/.env"
SYSTEMD_UNIT="${SERVICE_NAME}.service"

RED="\033[0;31m"; GREEN="\033[0;32m"; YELLOW="\033[1;33m"; CYAN="\033[0;36m"; NC="\033[0m"
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

require_env() {
    if [[ ! -f "${ENV_FILE}" ]]; then
        warn ".env not found – copying from .env.example"
        cp "${SCRIPT_DIR}/.env.example" "${ENV_FILE}"
        error "Please edit ${ENV_FILE} with your credentials and re-run."
    fi
    # shellcheck source=/dev/null
    set -a; source "${ENV_FILE}"; set +a
}

cmd_build() {
    info "Building Docker image for ${SERVICE_NAME}..."
    docker compose -f "${COMPOSE_FILE}" build
    ok "Build complete."
}

cmd_rebuild() {
    info "Rebuilding Docker image (no cache)..."
    docker compose -f "${COMPOSE_FILE}" build --no-cache
    ok "Rebuild complete."
}

cmd_start() {
    require_env
    info "Starting ${SERVICE_NAME}..."
    docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up -d --remove-orphans
    sleep 2
    cmd_status
}

cmd_stop() {
    info "Stopping ${SERVICE_NAME}..."
    docker compose -f "${COMPOSE_FILE}" down
    ok "Service stopped."
}

cmd_restart() {
    cmd_stop
    cmd_start
}

cmd_status() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  OME MCP v5 Status${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"

    RUNNING=$(docker ps --filter "name=${CONTAINER_NAME}" --format "{{.Status}}" 2>/dev/null || true)
    if [[ -n "${RUNNING}" ]]; then
        ok "Container: ${RUNNING}"
        PORT=$(docker inspect "${CONTAINER_NAME}" --format '{{range $p, $conf := .NetworkSettings.Ports}}{{$p}} -> {{(index $conf 0).HostPort}} {{end}}' 2>/dev/null || echo "unknown")
        info "Ports   : ${PORT}"
        info "Endpoint: http://$(hostname -I | awk '{print $1}'):${MCP_PORT:-8000}/mcp"
    else
        warn "Container is NOT running."
    fi

    # Systemd status (if managed by systemd)
    if systemctl is-active --quiet "${SYSTEMD_UNIT}" 2>/dev/null; then
        ok "systemd unit '${SYSTEMD_UNIT}' is active."
    fi

    echo ""
}

cmd_logs() {
    LINES="${2:-100}"
    info "Showing last ${LINES} log lines (Ctrl-C to exit)..."
    docker compose -f "${COMPOSE_FILE}" logs --tail="${LINES}" -f
}

cmd_install_systemd() {
    UNIT_FILE="${SCRIPT_DIR}/${SYSTEMD_UNIT}"
    if [[ ! -f "${UNIT_FILE}" ]]; then
        error "Systemd unit file not found: ${UNIT_FILE}"
    fi
    info "Installing systemd service..."
    # Update WorkingDirectory in unit file to actual path
    sed "s|/opt/ome_mcp_v5|${SCRIPT_DIR}|g" "${UNIT_FILE}" \
        | sudo tee "/etc/systemd/system/${SYSTEMD_UNIT}" > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable "${SYSTEMD_UNIT}"
    ok "Service installed. Run: sudo systemctl start ${SYSTEMD_UNIT}"
}

cmd_uninstall_systemd() {
    sudo systemctl stop "${SYSTEMD_UNIT}" 2>/dev/null || true
    sudo systemctl disable "${SYSTEMD_UNIT}" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/${SYSTEMD_UNIT}"
    sudo systemctl daemon-reload
    ok "Systemd service removed."
}

case "${1:-help}" in
    start)             cmd_start ;;
    stop)              cmd_stop ;;
    restart)           cmd_restart ;;
    status)            require_env; cmd_status ;;
    logs)              cmd_logs "$@" ;;
    build)             cmd_build ;;
    rebuild)           cmd_rebuild ;;
    install-systemd)   cmd_install_systemd ;;
    uninstall-systemd) cmd_uninstall_systemd ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs [N]|build|rebuild|install-systemd|uninstall-systemd}"
        echo ""
        echo "  start             – Start the MCP server container"
        echo "  stop              – Stop the MCP server container"
        echo "  restart           – Stop then start"
        echo "  status            – Show container and endpoint status"
        echo "  logs [N]          – Tail last N lines of container logs (default 100)"
        echo "  build             – Build (or rebuild cached) Docker image"
        echo "  rebuild           – Force rebuild with --no-cache"
        echo "  install-systemd   – Install as systemd service (auto-start on boot)"
        echo "  uninstall-systemd – Remove systemd service"
        exit 1
        ;;
esac
