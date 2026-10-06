#!/usr/bin/env bash
# Install, enable, start, and verify the Google Tasks MCP server and its Secure MCP Tunnel as
# systemd user services. Safe to run repeatedly: it only rewrites what changed and restarts the
# services so they pick up new code.
#
#   scripts/setup-services.sh [--stop-manual] [--no-start]
#
#   --stop-manual  stop google-tasks-mcp / tunnel-client processes started by hand (in a terminal)
#                  that hold port 8765 or the tunnel profile, so the services can take over.
#   --no-start     install and enable only.
#
# The tunnel key comes from ~/.config/google-tasks-mcp/tunnel.env. When that file has no key yet,
# the script takes CONTROL_PLANE_API_KEY from the environment or prompts for it (input hidden).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/google-tasks-mcp"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PROFILE="${GTASKS_TUNNEL_PROFILE:-google-tasks-test}"
TUNNEL_CLIENT="${GTASKS_TUNNEL_CLIENT:-/usr/local/bin/tunnel-client}"
PORT="${GTASKS_PORT:-8765}"
PYTHON="${PYTHON:-python3}"
MCP_UNIT=google-tasks-mcp.service
TUNNEL_UNIT=google-tasks-tunnel.service
STOP_MANUAL=0
START=1

for arg in "$@"; do
  case "$arg" in
    --stop-manual) STOP_MANUAL=1 ;;
    --no-start) START=0 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
ok() { printf '    ok: %s\n' "$*"; }
warn() { printf '    warning: %s\n' "$*" >&2; }
die() { printf '    error: %s\n' "$*" >&2; exit 1; }

step "Checking prerequisites"
# These paths are substituted into unit files; keep them free of characters systemd or sed would mangle.
for path in "$REPO" "$CONFIG_DIR" "$TUNNEL_CLIENT"; do
  [[ "$path" =~ ^[A-Za-z0-9._/+-]+$ ]] || die "unsupported characters in path: $path"
done
[[ "$PROFILE" =~ ^[A-Za-z0-9._-]+$ ]] || die "unsupported tunnel profile name: $PROFILE"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python 3.11+ is required ($PYTHON)"
ok "$("$PYTHON" --version)"
systemctl --user show-environment >/dev/null 2>&1 || die "systemctl --user is not available (no user session bus)"
ok "systemd user manager reachable"
[[ -x "$TUNNEL_CLIENT" ]] || die "$TUNNEL_CLIENT not found or not executable"
ok "$("$TUNNEL_CLIENT" --version | cut -d+ -f1)"
"$TUNNEL_CLIENT" profiles list 2>/dev/null | awk '{print $1}' | grep -qx "$PROFILE" \
  || die "tunnel-client profile '$PROFILE' not found (this script never creates profiles)"
ok "tunnel profile $PROFILE"

step "Python environment ($REPO/.venv)"
if [[ ! -x "$REPO/.venv/bin/python" ]]; then
  "$PYTHON" -m venv "$REPO/.venv"
  ok "created virtual environment"
fi
"$REPO/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -e "$REPO"
ok "installed google-tasks-mcp $("$REPO/.venv/bin/google-tasks-mcp" --version | awk '{print $2}')"

step "Configuration ($CONFIG_DIR)"
install -d -m 700 "$CONFIG_DIR"
# Create the state directory before systemd sees StateDirectory=; otherwise systemd links it to the
# config directory (a compatibility shim for pre-254 layouts) and credentials would land there.
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/google-tasks-mcp"
if [[ -L "$STATE_DIR" && "$(readlink -f "$STATE_DIR")" == "$(readlink -f "$CONFIG_DIR")" ]]; then
  rm "$STATE_DIR"
fi
install -d -m 700 "$STATE_DIR"
if [[ ! -f "$CONFIG_DIR/server.env" ]]; then
  install -m 600 "$REPO/deploy/server.env.example" "$CONFIG_DIR/server.env"
  ok "created server.env from deploy/server.env.example"
fi
chmod 600 "$CONFIG_DIR/server.env"
for f in "$CONFIG_DIR/client_secret.json" "$CONFIG_DIR/tunnel.env"; do
  [[ -f "$f" ]] && chmod 600 "$f"
done
tunnel_key=1
if grep -qs '^CONTROL_PLANE_API_KEY=.\+' "$CONFIG_DIR/tunnel.env"; then
  ok "tunnel.env has CONTROL_PLANE_API_KEY"
else
  key="${CONTROL_PLANE_API_KEY:-}"
  if [[ -z "$key" && -t 0 ]]; then
    read -r -s -p "    Tunnel runtime API key (CONTROL_PLANE_API_KEY; input hidden, Enter to skip): " key
    echo
  fi
  if [[ -n "$key" ]]; then
    (umask 077; printf '# Secure MCP Tunnel runtime key for google-tasks-tunnel.service.\nCONTROL_PLANE_API_KEY=%s\n' \
      "$key" > "$CONFIG_DIR/tunnel.env")
    ok "wrote tunnel.env (chmod 600)"
  else
    tunnel_key=0
    warn "no tunnel key: the tunnel service will not be enabled. Re-run with CONTROL_PLANE_API_KEY exported"
    warn "(or interactively), or add CONTROL_PLANE_API_KEY=... to $CONFIG_DIR/tunnel.env (chmod 600)."
  fi
  unset key
fi
if [[ -f "$CONFIG_DIR/client_secret.json" ]] || grep -qs '^GTASKS_GOOGLE_CLIENT_ID=.\+' "$CONFIG_DIR/server.env"; then
  ok "Google OAuth client configured"
else
  warn "no Google OAuth client yet: save it as $CONFIG_DIR/client_secret.json (README: 'Google Cloud setup')"
fi

step "systemd units ($UNIT_DIR)"
install -d "$UNIT_DIR"
changed=0
render() {  # template destination
  local tmp
  tmp="$(mktemp)"
  sed -e "s|@REPO@|$REPO|g" -e "s|@CONFIG_DIR@|$CONFIG_DIR|g" -e "s|@TUNNEL_CLIENT@|$TUNNEL_CLIENT|g" \
    -e "s|@PROFILE@|$PROFILE|g" "$1" > "$tmp"
  if ! cmp -s "$tmp" "$2"; then
    install -m 644 "$tmp" "$2"
    changed=1
    ok "installed $(basename "$2")"
  else
    ok "$(basename "$2") unchanged"
  fi
  rm -f "$tmp"
}
render "$REPO/deploy/systemd/$MCP_UNIT" "$UNIT_DIR/$MCP_UNIT"
render "$REPO/deploy/systemd/$TUNNEL_UNIT" "$UNIT_DIR/$TUNNEL_UNIT"
if (( changed )); then
  systemctl --user daemon-reload
  ok "reloaded systemd"
fi
systemctl --user enable --quiet "$MCP_UNIT"
ok "enabled $MCP_UNIT"
if (( tunnel_key )); then
  systemctl --user enable --quiet "$TUNNEL_UNIT"
  ok "enabled $TUNNEL_UNIT"
else
  systemctl --user disable --quiet "$TUNNEL_UNIT" 2>/dev/null || true
  warn "$TUNNEL_UNIT installed but not enabled (no tunnel key)"
fi

step "Start at boot without a login (lingering)"
if [[ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" == "yes" ]]; then
  ok "lingering already enabled for $USER"
elif loginctl enable-linger "$USER" 2>/dev/null; then
  ok "enabled lingering for $USER"
else
  warn "could not enable lingering; run: sudo loginctl enable-linger $USER"
fi

(( START )) || { echo; echo "Installed and enabled (not started)."; exit 0; }

step "Checking for manually started copies"
manual=()
mcp_pid="$(systemctl --user show -p MainPID --value "$MCP_UNIT" 2>/dev/null || echo 0)"
uses_profile() {  # pid: does its command line contain exactly "--profile $PROFILE" / "--profile=$PROFILE"?
  tr '\0' '\n' < "/proc/$1/cmdline" 2>/dev/null | awk -v p="$PROFILE" '
    prev == "--profile" && $0 == p { found = 1 } $0 == "--profile=" p { found = 1 } { prev = $0 }
    END { exit !found }'
}
while read -r pid; do
  [[ -n "$pid" && "$pid" != "$mcp_pid" ]] || continue
  grep -q "$MCP_UNIT" "/proc/$pid/cgroup" 2>/dev/null && continue
  if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'google-tasks-mcp'; then
    manual+=("$pid")
  else
    die "port $PORT is used by another program (pid $pid); free it or set GTASKS_PORT"
  fi
done < <(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u)
for pid in $( (( tunnel_key )) && pgrep -x tunnel-client || true); do
  uses_profile "$pid" || continue
  grep -q "$TUNNEL_UNIT" "/proc/$pid/cgroup" 2>/dev/null || manual+=("$pid")
done
if (( ${#manual[@]} )); then
  for pid in "${manual[@]}"; do
    printf '    found: pid %s: %s\n' "$pid" "$(tr '\0' ' ' < "/proc/$pid/cmdline" | cut -c1-100)"
  done
  (( STOP_MANUAL )) || die "stop these first (Ctrl+C in their terminals), or re-run with --stop-manual"
  kill "${manual[@]}" 2>/dev/null || true
  for _ in $(seq 1 20); do
    alive=0
    for pid in "${manual[@]}"; do kill -0 "$pid" 2>/dev/null && alive=1; done
    (( alive )) || break
    sleep 0.5
  done
  ok "stopped manually started processes"
else
  ok "none"
fi

step "Starting services"
systemctl --user restart "$MCP_UNIT"
ok "$MCP_UNIT: $(systemctl --user is-active "$MCP_UNIT")"
if (( tunnel_key )); then
  systemctl --user restart "$TUNNEL_UNIT"
  ok "$TUNNEL_UNIT: $(systemctl --user is-active "$TUNNEL_UNIT")"
fi

step "Verifying"
failed=0
"$REPO/.venv/bin/google-tasks-mcp" wait-ready --timeout 30 >/dev/null && ok "MCP server ready at http://127.0.0.1:$PORT/mcp" \
  || { warn "MCP server not ready; see: journalctl --user -u $MCP_UNIT"; failed=1; }
profile_file="${XDG_CONFIG_HOME:-$HOME/.config}/tunnel-client/$PROFILE.yaml"
health="$(grep -oE 'listen_addr:[[:space:]]*"?[0-9.]+:[0-9]+' "$profile_file" 2>/dev/null | grep -oE '[0-9.]+:[0-9]+' || true)"
health="${health:-127.0.0.1:8080}"
tunnel_ready=0
(( tunnel_key )) || { warn "tunnel not started (no key)"; failed=1; }
for _ in $( (( tunnel_key )) && seq 1 30); do
  if [[ "$(curl -fsS --max-time 2 "http://$health/readyz" 2>/dev/null)" == "ready" ]]; then tunnel_ready=1; break; fi
  sleep 1
done
if (( tunnel_ready )); then ok "tunnel-client ready (http://$health/readyz)"; elif (( tunnel_key )); then
  warn "tunnel-client not ready yet; see: journalctl --user -u $TUNNEL_UNIT"; failed=1
fi
units=("$MCP_UNIT")
(( tunnel_key )) && units+=("$TUNNEL_UNIT")
for unit in "${units[@]}"; do
  [[ "$(systemctl --user is-active "$unit")" == "active" ]] || { warn "$unit is not active"; failed=1; }
done

echo
"$REPO/.venv/bin/google-tasks-mcp" doctor || true
echo
if (( failed )); then
  echo "Services installed, but verification found problems (see above)."
  exit 1
fi
echo "Services installed, running, and verified. Check any WARN/FAIL lines from doctor above."
