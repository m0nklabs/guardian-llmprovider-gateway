#!/usr/bin/env bash
# Guardian LLM Provider Gateway — product installer (idempotent).
#
# Usage (from a clone of this repository):
#   scripts/install.sh                          # venv + config + TLS + validation
#   scripts/install.sh --with-systemd           # + render + install the systemd unit
#   scripts/install.sh --with-nginx             # + render + install the nginx configs
#   scripts/install.sh --print-only             # ONLY render deploy files (no venv/config/TLS/system changes)
#
# Common flags:
#   --dir PATH            install root (default: the repository you run this from)
#   --python BIN          interpreter to build the venv with (default: python3.14 → python3)
#   --user NAME           systemd User= for the rendered unit (default: current user)
#   --tls-cert FILE       reuse an existing certificate instead of generating one
#   --tls-key FILE        reuse an existing private key instead of generating one
#   --tls-dir PATH        where the TLS pair lives (default: ~/.config/guardian-llmprovider-gateway/tls)
#   --skip-venv           do not create/upgrade the venv
#   --print-only          never touch system dirs; render deploy files to ./deploy-rendered
#
# The installer is idempotent: re-running upgrades the venv and re-renders the
# deploy files, and never overwrites an existing .env, key file or TLS pair.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
PYTHON_BIN=""
WITH_SYSTEMD=0
WITH_NGINX=0
PRINT_ONLY=0
SKIP_VENV=0
TLS_DIR="${HOME}/.config/guardian-llmprovider-gateway/tls"
TLS_CERT=""
TLS_KEY=""
LAN_IP=""   # empty = auto-detect (default-route source); --lan-ip overrides

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) REPO="$(cd "$2" && pwd)"; shift 2 ;;
        --python) PYTHON_BIN="$2"; shift 2 ;;
        --user) RUN_USER="$2"; shift 2 ;;
        --tls-cert) TLS_CERT="$2"; shift 2 ;;
        --tls-key) TLS_KEY="$2"; shift 2 ;;
        --tls-dir) TLS_DIR="$2"; shift 2 ;;
        --lan-ip) LAN_IP="$2"; shift 2 ;;
        --with-systemd) WITH_SYSTEMD=1; shift ;;
        --with-nginx) WITH_NGINX=1; shift ;;
        --skip-venv) SKIP_VENV=1; shift ;;
        --print-only) PRINT_ONLY=1; shift ;;
        *) echo "Unknown flag: $1" >&2; exit 2 ;;
    esac
done

if [ -n "$TLS_CERT" ] && [ -z "$TLS_KEY" ]; then echo "--tls-cert needs --tls-key" >&2; exit 2; fi
if [ -n "$TLS_KEY" ] && [ -z "$TLS_CERT" ]; then echo "--tls-key needs --tls-cert" >&2; exit 2; fi

log() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*"; }

RENDER_DIR="$REPO/deploy-rendered"
mkdir -p "$RENDER_DIR"

# ── Preflight ────────────────────────────────────────────────────────────
log "Preflight (repo: $REPO)"
if [ -z "$PYTHON_BIN" ]; then
    if command -v python3.14 >/dev/null 2>&1; then PYTHON_BIN=python3.14
    else PYTHON_BIN=python3; fi
fi
PY_MAJOR=$("$PYTHON_BIN" -c 'import sys; print(sys.version_info[0])')
PY_MINOR=$("$PYTHON_BIN" -c 'import sys; print(sys.version_info[1])')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 12 ]; }; then
    echo "Python >= 3.12 required (found $PY_MAJOR.$PY_MINOR via $PYTHON_BIN)" >&2; exit 1
fi
if [ "$PY_MAJOR" -ne 3 ] || [ "$PY_MINOR" -ne 14 ]; then
    warn "the dependency pins are tested on Python 3.14; found $PY_MAJOR.$PY_MINOR — continuing"
fi
# Prefer the source address of the default route: on multi-interface hosts
# (docker0, virbr0, VPN tunnels) the first `hostname -I` entry is frequently
# NOT the LAN interface, and this value feeds the certificate SAN and the
# nginx dashboard perimeter. --lan-ip overrides detection entirely.
if [ -z "$LAN_IP" ]; then
    # || true guards each probe: under set -euo pipefail a failed command
    # substitution in an assignment aborts the script before the fallbacks run.
    LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}' || true)"
    [ -z "$LAN_IP" ] && LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    LAN_IP="${LAN_IP:-127.0.0.1}"
fi
# An existing TLS pair IS the deployment's identity: when the detected LAN IP
# does not match a certificate already in $TLS_DIR but exactly one other
# guardian-*.crt exists, adopt that IP instead (multi-interface hosts detect
# bridge/VPN addresses; the review caught this on the production host).
_existing_certs="$(ls "$TLS_DIR"/guardian-*.crt 2>/dev/null || true)"
if [ -n "$_existing_certs" ] && [ ! -f "$TLS_DIR/guardian-${LAN_IP}.crt" ] && [ "$(echo "$_existing_certs" | wc -l)" -eq 1 ]; then
    _cert_ip="$(basename "$_existing_certs" | sed 's/^guardian-//; s/\.crt$//')"
    if [[ "$_cert_ip" =~ ^[A-Za-z0-9._-]+$ ]]; then
        warn "detected LAN IP ${LAN_IP} has no certificate, but ${_cert_ip} does — using the existing cert identity (override with --lan-ip)"
        LAN_IP="$_cert_ip"
    else
        warn "existing certificate filename does not parse as an IP/hostname — ignoring it"
    fi
fi
TLS_CERTFILE="$TLS_DIR/guardian-${LAN_IP}.crt"
TLS_KEYFILE="$TLS_DIR/guardian-${LAN_IP}.key"
LAN_SUBNET="$(awk -F. -v ip="$LAN_IP" 'BEGIN{split(ip,a,"."); printf "%s.%s.%s.0/24", a[1], a[2], a[3]}')"
echo "python: $PYTHON_BIN ($PY_MAJOR.$PY_MINOR) | lan ip: $LAN_IP | user: $RUN_USER"

# ── Render deploy files ──────────────────────────────────────────────────
log "Rendering deploy files → $RENDER_DIR"
RENDER_UI_PORT="${GUARDIAN_UI_PORT:-11437}"
render() { # render SRC DST
    sed -e "s|@INSTALL_DIR@|$REPO|g" \
        -e "s|@RUN_USER@|$RUN_USER|g" \
        -e "s|@TLS_CERTFILE@|$TLS_CERTFILE|g" \
        -e "s|@TLS_KEYFILE@|$TLS_KEYFILE|g" \
        -e "s|@TLS_TRUSTED_CERT@|$TLS_CERTFILE|g" \
        -e "s|@LAN_IP@|$LAN_IP|g" \
        -e "s|@LAN_SUBNET@|$LAN_SUBNET|g" \
        -e "s|@UI_PORT@|$RENDER_UI_PORT|g" \
        "$1" > "$2"
}
render "$REPO/deploy/systemd/guardian-llmprovider-gateway.service" \
       "$RENDER_DIR/guardian-llmprovider-gateway.service"
mkdir -p "$RENDER_DIR/guardian-llmprovider-gateway.service.d"
render "$REPO/deploy/systemd/guardian-llmprovider-gateway.service.d/20-tls.conf" \
       "$RENDER_DIR/guardian-llmprovider-gateway.service.d/20-tls.conf"
render "$REPO/deploy/nginx/guardian-llmprovider-gateway-protocol-mux.conf" \
       "$RENDER_DIR/guardian-llmprovider-gateway-protocol-mux.conf"
render "$REPO/deploy/nginx/guardian-llmprovider-gateway-loopback-http.conf" \
       "$RENDER_DIR/guardian-llmprovider-gateway-loopback-http.conf"
render "$REPO/deploy/nginx/guardian-llmprovider-gateway-dashboard.conf" \
       "$RENDER_DIR/guardian-llmprovider-gateway-dashboard.conf"
_unresolved="$(grep -h '@[A-Z_]*@' -r "$RENDER_DIR" || true)"
_grep_rc=$?
if [ "$_grep_rc" -ge 2 ]; then
    echo "grep failed while checking rendered files (rc=$_grep_rc)" >&2; exit 1
fi
if [ -n "$_unresolved" ]; then
    echo "unresolved placeholders in rendered files — aborting" >&2; exit 1
fi
if [ "$PRINT_ONLY" -eq 1 ]; then
    log "Print-only: rendered to $RENDER_DIR — no venv, config, TLS or system changes performed"
    exit 0
fi

# ── venv ─────────────────────────────────────────────────────────────────
if [ "$SKIP_VENV" -eq 0 ]; then
    log "Virtualenv (venv/ from requirements.txt)"
    if [ -L "$REPO/venv" ]; then
        warn "venv is a symlink ($("readlink" "$REPO/venv")) — replacing it with a real venv"
        rm "$REPO/venv"
    fi
    if [ ! -d "$REPO/venv" ]; then
        "$PYTHON_BIN" -m venv "$REPO/venv"
    fi
    "$REPO/venv/bin/pip" install -q --upgrade pip
    "$REPO/venv/bin/pip" install -q -r "$REPO/requirements.txt"
    echo "venv OK: $("$REPO/venv/bin/python" --version)"
else
    log "Skipping venv (--skip-venv)"
fi
VENV_PY="$REPO/venv/bin/python3.14"
[ -x "$VENV_PY" ] || VENV_PY="$REPO/venv/bin/python"

# ── Config bootstrap (.env + operator key) ───────────────────────────────
if [ "$SKIP_VENV" -eq 1 ] && [ ! -x "$REPO/venv/bin/python" ]; then
    echo "--skip-venv requires an existing venv (none at $REPO/venv) — aborting before partial bootstrap" >&2
    exit 1
fi
log "Config bootstrap"
if [ ! -f "$REPO/.env" ]; then
    # umask 077: the operator fills provider API keys into this file; it must
    # never be world-readable, even before the app hardens it on first start.
    ( umask 077; cp "$REPO/.env.example" "$REPO/.env" )
    warn ".env created from template (0600) — fill in the provider API keys before starting"
else
    echo ".env present — leaving it untouched"
fi
if [ ! -f "$REPO/config/guardian.keys.yaml" ]; then
    echo "minting the first operator API key..."
    (cd "$REPO" && "$VENV_PY" scripts/generate_key.py operator)
    warn "store the printed key now — it is only shown once"
else
    echo "config/guardian.keys.yaml present — leaving it untouched"
fi

# ── TLS pair ─────────────────────────────────────────────────────────────
log "TLS pair ($TLS_DIR)"
# umask 077 during material creation: openssl/cp honor the ambient umask and
# would briefly expose the private key world-readable before the chmod below.
if [ -n "$TLS_CERT" ]; then
    mkdir -p "$TLS_DIR"
    ( umask 077; cp "$TLS_CERT" "$TLS_CERTFILE"; cp "$TLS_KEY" "$TLS_KEYFILE" )
    chmod 600 "$TLS_KEYFILE"
    echo "reused provided certificate: $TLS_CERTFILE"
elif [ -f "$TLS_CERTFILE" ] && [ -f "$TLS_KEYFILE" ]; then
    echo "existing TLS pair found — leaving it untouched"
else
    mkdir -p "$TLS_DIR"
    ( umask 077; openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$TLS_KEYFILE" -out "$TLS_CERTFILE" \
        -subj "/CN=guardian" \
        -addext "subjectAltName=DNS:localhost,DNS:$(hostname),IP:127.0.0.1,IP:${LAN_IP}" \
        >/dev/null 2>&1 )
    chmod 600 "$TLS_KEYFILE"
    echo "generated self-signed pair: $TLS_CERTFILE"
    warn "LAN clients must trust this certificate before connecting without a custom CA setting"
fi

install_systemd() {
    if sudo -n true 2>/dev/null; then
        sudo install -m 644 "$RENDER_DIR/guardian-llmprovider-gateway.service" \
            /etc/systemd/system/guardian-llmprovider-gateway.service
        sudo mkdir -p /etc/systemd/system/guardian-llmprovider-gateway.service.d
        sudo install -m 644 "$RENDER_DIR/guardian-llmprovider-gateway.service.d/20-tls.conf" \
            /etc/systemd/system/guardian-llmprovider-gateway.service.d/20-tls.conf
        sudo systemctl daemon-reload
        echo "systemd unit installed (enable with: sudo systemctl enable guardian-llmprovider-gateway.service)"
    else
        warn "no passwordless sudo — copy the rendered files from $RENDER_DIR to /etc/systemd/system/ manually"
    fi
}
install_nginx() {
    if sudo -n true 2>/dev/null; then
        sudo install -m 644 "$RENDER_DIR/guardian-llmprovider-gateway-loopback-http.conf" \
            /etc/nginx/conf.d/guardian-llmprovider-gateway-loopback-http.conf
        sudo install -m 644 "$RENDER_DIR/guardian-llmprovider-gateway-dashboard.conf" \
            /etc/nginx/conf.d/guardian-llmprovider-gateway-dashboard.conf
        sudo mkdir -p /etc/nginx/stream-conf.d
        sudo install -m 644 "$RENDER_DIR/guardian-llmprovider-gateway-protocol-mux.conf" \
            /etc/nginx/stream-conf.d/guardian-llmprovider-gateway-protocol-mux.conf
        if sudo nginx -t; then
            echo "nginx configs installed (reload with: sudo systemctl reload nginx)"
        else
            warn "nginx -t FAILED after installing the configs — fix or remove them before reloading nginx"
            exit 1
        fi
    else
        warn "no passwordless sudo — copy the rendered configs from $RENDER_DIR manually"
        warn "the protocol mux is a stream{} block: it belongs in /etc/nginx/stream-conf.d/ (loaded from a top-level stream { include ...; })"
    fi
}
if [ "$WITH_SYSTEMD" -eq 1 ]; then install_systemd; fi
if [ "$WITH_NGINX" -eq 1 ]; then install_nginx; fi

# ── Validation ───────────────────────────────────────────────────────────
log "Validation"
if [ "$SKIP_VENV" -eq 1 ] && [ ! -x "$VENV_PY" ]; then
    warn "--skip-venv with no existing venv — skipping import validation"
    VENV_PY=""
fi
[ -n "$VENV_PY" ] && (cd "$REPO" && "$VENV_PY" -c "import app.main; print('app imports OK')")
if [ -n "$VENV_PY" ]; then
    (cd "$REPO" && "$VENV_PY" -c "import yaml, pathlib; [yaml.safe_load(p.read_text()) for p in pathlib.Path('config').rglob('*.yaml')]; print('config parses OK')") \
        || warn "config parse check failed — fill in .env before starting"
fi
if ss -tln 2>/dev/null | grep -q ':11434 '; then
    warn "port 11434 already in use — is another Guardian instance running?"
fi

# ── Next steps ───────────────────────────────────────────────────────────
log "Done — next steps"
cat <<EOF
  1. Verify the detected LAN IP ($LAN_IP — override with --lan-ip when this host has
     bridges/tunnels/VPN interfaces): it feeds the cert SAN and the nginx allowlist.
  2. Fill in $REPO/.env (provider API keys, CARETAKER_KEY if you run the caretaker daemon).
  3. Trust $TLS_CERTFILE on every LAN client that should connect without a custom CA.
  4. Start Guardian:
       systemd : sudo systemctl enable --now guardian-llmprovider-gateway.service
       manual  : cd $REPO && venv/bin/python -m app.main
  5. Smoke test: curl -k https://127.0.0.1:11434/v1/models -H "Authorization: Bearer <key>"
  6. Optional: install and start the caretaker daemon (separate repo:
     caretaker-llamacpp, deploy/systemd/) for remote-first backend lifecycle.
EOF
