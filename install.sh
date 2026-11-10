#!/bin/sh
# Install or update the Huawei R4850G2 charger driver on a Venus OS device.
#
#   sh install.sh              install, then start the service
#   sh install.sh --dry-run    print the plan, change nothing
#
# Assumes the repository lives at INSTALL_DIR (git clone, like dbus-mpp-p18).
# Nothing is written outside /data and /service, so a firmware update keeps the
# checkout and the state but restores the stock service tree: re-run this
# script after an update.
set -eu

INSTALL_DIR="${INSTALL_DIR:-/data/etc/dbus-r4850G2-charger}"
STATE_DIR="${STATE_DIR:-/data/etc/dbus-r4850-state}"
SERVICE_NAME="r4850-charger"
# /service is a tmpfs, bind-mounted at boot over a ramdisk copy of
# /opt/victronenergy/service (see /etc/init.d/overlays.sh). Writing only to
# /service works until the next reboot, then the service vanishes silently.
# The canonical copy therefore lives in /opt and is replayed into /service the
# same way the boot path does it: delete, cp -a, start.
SERVICE_DIR="/opt/victronenergy/service/$SERVICE_NAME"
LIVE_SERVICE_DIR="/service/$SERVICE_NAME"
LOGDIR="/var/log/dbus-r4850"
CAN_IFACE="${R4850_CAN_IFACE:-can1}"
DEVICE_INSTANCE="${R4850_DEVICE_INSTANCE:-10}"
RUN_INSTANCE="$DEVICE_INSTANCE"
TEST_FLAG=""
DRY_RUN=0

TEST_MODE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --test) TEST_MODE=1; SERVICE_NAME="r4850-charger-test"; SERVICE_DIR="/opt/victronenergy/service/$SERVICE_NAME"; LIVE_SERVICE_DIR="/service/$SERVICE_NAME" ;;
    esac
done

say() { printf '%s\n' "$*"; }
run() {
    if [ "$DRY_RUN" = 1 ]; then
        say "  [dry-run] $*"
    else
        say "  $*"
        sh -c "$*"
    fi
}

say "== Vérifications"
[ "$(id -u)" = 0 ] || { echo "à lancer en root" >&2; exit 1; }
[ -f /etc/venus/swu-name ] || echo "attention: /etc/venus absent, êtes-vous sur Venus OS ?"
[ -d "$INSTALL_DIR/.git" ] || { echo "dépôt absent de $INSTALL_DIR" >&2; exit 1; }
for mod in dbus gi; do
    python3 -c "import $mod" 2>/dev/null && say "  python3 -m $mod : ok" \
        || { echo "  python3 -m $mod : MANQUANT" >&2; exit 1; }
done
if [ -d "$INSTALL_DIR/velib_python/vedbus" ]; then
    say "  velib_python : présent"
else
    say "  velib_python : submodule à récupérer"
    run "cd '$INSTALL_DIR' && git submodule update --init --depth 1 velib_python"
fi

say "== Mise à jour du code"
run "cd '$INSTALL_DIR' && git fetch --quiet && git merge --ff-only --quiet @{u}"
# Only the shell scripts are executed directly; the .py files are launched as
# "python3 file.py". chmod'ing tracked files would dirty the tree and block the
# git merge above on the next run - that is how this line used to break updates.
run "chmod +x '$INSTALL_DIR'/r4850-can-up.sh"
run "mkdir -p '$STATE_DIR' '$LOGDIR'"

if [ "$TEST_MODE" = 1 ]; then
    say "== Mode test : module virtuel, aucune modification de $CAN_IFACE"
else
    say "== Interface CAN ($CAN_IFACE à 125 kbit/s)"
    run "'$INSTALL_DIR/r4850-can-up.sh'"
fi

say "== Service runit"
if [ "$DRY_RUN" = 1 ]; then
    say "  [dry-run] créer $SERVICE_DIR/run puis rejouer le chemin de boot vers $LIVE_SERVICE_DIR"
else
    mkdir -p "$SERVICE_DIR"
    [ "$TEST_MODE" = 1 ] && { RUN_INSTANCE=$((DEVICE_INSTANCE + 1)); TEST_FLAG=" --test"; }
    cat > "$SERVICE_DIR/run" <<EOF
#!/bin/sh
exec 2>&1
. /etc/profile.d/profile.sh
'$INSTALL_DIR/r4850-can-up.sh' || true
exec python3 '$INSTALL_DIR/dbus-r4850-ac-charger.py' \\
    --can $CAN_IFACE --device-instance $RUN_INSTANCE$TEST_FLAG \\
    >> '$LOGDIR/current' 2>&1
EOF
    chmod +x "$SERVICE_DIR/run"
    say "  $SERVICE_DIR/run créé (persistant)"
    # Replay the boot path identically, so the live copy is byte-for-byte what
    # the next boot will produce. Stop first: deleting the directory of a
    # running service orphans its process, and runit would then supervise a
    # directory that no longer exists.
    svc -d "$LIVE_SERVICE_DIR" 2>/dev/null || true
    rm -rf "$LIVE_SERVICE_DIR"
    cp -a "$SERVICE_DIR" "$LIVE_SERVICE_DIR"
    say "  $LIVE_SERVICE_DIR rejoué depuis /opt"
fi

say "== Démarrage"
# svc -u only starts a stopped service: after an update the running process would
# keep the old code. -t restarts it whether it is up or down.
run "svc -t '$LIVE_SERVICE_DIR' || svc -u '$LIVE_SERVICE_DIR' || true"
run "sleep 2; svstat '$LIVE_SERVICE_DIR' || true"

say ""
say "== Contrôle (à lancer dans 10 s)"
say "  tail -n 30 $LOGDIR/current   (ou journalctl/service log du runit)"
say "  dbus-send --system --dest=com.victronenergy.Bus --print-reply / com.victronenergy.Bus.ListNames | grep r4850"
say "  ip -details link show $CAN_IFACE | grep bitrate"
say ""
say "Attention : sans module branché, le service se présente comme absent"
say "(/Connected=0, /State=0, /ErrorCode=0) et ne force aucun setpoint."
say "La cause exacte est dans /Diagnostics/Reason."
