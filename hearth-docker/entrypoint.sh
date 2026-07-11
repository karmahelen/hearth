#!/bin/bash
#
# entrypoint.sh — Hearth container entrypoint (serve-only)
#
# Flow:
#   1. Ensure hearthmonitor is selected in HEARTH_APPS (container can't run
#      without it).
#   2. Run hearth-install.sh in non-interactive mode: fetch-at-runtime clone
#      + rsync into the /data volume, honoring HEARTH_UPDATE / HEARTH_APPS /
#      HEARTH_REF. If the fetch fails but a working install already exists in
#      the volume, degrade gracefully to offline mode.
#   3. Seed the serve-mode password from HEARTH_PASSWORD (write-once: never
#      overwrites an existing password — hearth.json stays owned by the app,
#      so passwords changed via Hearth Monitor survive restarts).
#   4. exec into `uv run hearthmonitor.py --serve` so hearthmonitor receives
#      Docker's SIGTERM directly and hearth.py's handler shuts down cleanly.
#

set -u

export HEARTH_NONINTERACTIVE=1
export HEARTH_INSTALL_DIR="${HEARTH_INSTALL_DIR:-/data}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/data/uv-cache}"

DATA_DIR="$HEARTH_INSTALL_DIR"
HEARTH_DIR="$DATA_DIR/Hearth"
HEARTH_PORT="${HEARTH_PORT:-8000}"

echo "[entrypoint] Hearth container starting"
echo "[entrypoint]   install dir : $HEARTH_DIR"
echo "[entrypoint]   uv cache    : $UV_CACHE_DIR"
echo "[entrypoint]   update mode : ${HEARTH_UPDATE:-always}"
echo "[entrypoint]   ref         : ${HEARTH_REF:-<default branch>}"
echo "[entrypoint]   apps        : ${HEARTH_APPS:-all}"
echo "[entrypoint]   serve port  : $HEARTH_PORT"

mkdir -p "$DATA_DIR"

# --- 1. The container cannot function without hearthmonitor ----------------
# If HEARTH_APPS is a restrictive list (or 'none') and hearthmonitor ships as
# a submodule, make sure it is still selected. If hearthmonitor is part of
# the main repo instead, this is a harmless no-op.
case "${HEARTH_APPS:-all}" in
    all) ;;
    none)
        echo "[entrypoint] HEARTH_APPS=none, but the container requires hearthmonitor — selecting it anyway."
        export HEARTH_APPS="hearthmonitor"
        ;;
    *hearthmonitor*) ;;
    *)
        echo "[entrypoint] Adding hearthmonitor to HEARTH_APPS (the container requires it)."
        export HEARTH_APPS="${HEARTH_APPS},hearthmonitor"
        ;;
esac

# --- 2. Install / update via the standard installer ------------------------
if hearth-install.sh; then
    echo "[entrypoint] Install/update completed."
else
    if [[ -f "$HEARTH_DIR/hearth.py" ]]; then
        echo "[entrypoint] WARNING: fetch/update failed — running the existing install (offline mode)."
    else
        echo "[entrypoint] ERROR: fetch failed and no existing install is present in the volume." >&2
        echo "[entrypoint] Check network access to the repo, then restart the container." >&2
        exit 1
    fi
fi

# --- 3. Password seeding (write-once) ---------------------------------------
CONFIG_FILE="$HEARTH_DIR/hearth.json"

if [[ -n "${HEARTH_PASSWORD:-}" ]]; then
    python3 - "$CONFIG_FILE" <<'PYEOF'
import json, os, sys

path = sys.argv[1]
cfg = {}
if os.path.isfile(path):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        print("[entrypoint] hearth.json exists but is not valid JSON — leaving it untouched; HEARTH_PASSWORD ignored.")
        sys.exit(0)

if cfg.get("password"):
    print("[entrypoint] Password already configured in hearth.json — HEARTH_PASSWORD ignored (write-once).")
else:
    cfg["password"] = os.environ["HEARTH_PASSWORD"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)
    print("[entrypoint] Seeded serve-mode password from HEARTH_PASSWORD.")
PYEOF
else
    if ! python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("password") else 1)' "$CONFIG_FILE" 2>/dev/null; then
        echo "[entrypoint] *********************************************************"
        echo "[entrypoint] *** WARNING: no serve-mode password is configured.    ***"
        echo "[entrypoint] *** Hearth will be reachable on the LAN without login.***"
        echo "[entrypoint] *** Set HEARTH_PASSWORD, or add one in Hearth Monitor.***"
        echo "[entrypoint] *********************************************************"
    fi
fi

# --- 4. Launch --------------------------------------------------------------
MONITOR_SCRIPT="$HEARTH_DIR/hearthmonitor/hearthmonitor.py"
if [[ ! -f "$MONITOR_SCRIPT" ]]; then
    echo "[entrypoint] ERROR: hearthmonitor not found at $MONITOR_SCRIPT." >&2
    echo "[entrypoint] If hearthmonitor is a submodule, make sure HEARTH_APPS includes it (or use 'all')." >&2
    exit 1
fi

cd "$HEARTH_DIR/hearthmonitor"
echo "[entrypoint] Starting Hearth Monitor in serve mode on port $HEARTH_PORT"
exec uv run hearthmonitor.py --serve "$HEARTH_PORT"
