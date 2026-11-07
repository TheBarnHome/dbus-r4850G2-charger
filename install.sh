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
SERVICE_DIR="/service/r4850-charger"
LOGDIR="/var/log/dbus-r4850"
CAN_IFACE="${R4850_CAN_IFACE:-can1}"
MQTT_HOST="${R4850_MQTT_HOST:-192.168.10.100}"
DEVICE_INSTANCE="${R4850_DEVICE_INSTANCE:-10}"
DRY_RUN=0

[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

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
for mod in dbus gi paho.mqtt.client; do
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
run "chmod +x '$INSTALL_DIR'/dbus-r4850-ac-charger.py '$INSTALL_DIR'/r4850.py '$INSTALL_DIR'/r4850-can-up.sh"
run "mkdir -p '$STATE_DIR' '$LOGDIR'"

say "== Interface CAN ($CAN_IFACE à 125 kbit/s)"
run "'$INSTALL_DIR/r4850-can-up.sh'"

say "== Service runit"
if [ "$DRY_RUN" = 1 ]; then
    say "  [dry-run] créer $SERVICE_DIR/run"
else
    mkdir -p "$SERVICE_DIR"
    cat > "$SERVICE_DIR/run" <<EOF
#!/bin/sh
exec 2>&1
. /etc/profile.d/profile.sh
'$INSTALL_DIR/r4850-can-up.sh' || true
exec python3 '$INSTALL_DIR/dbus-r4850-ac-charger.py' \\
    --can $CAN_IFACE --device-instance $DEVICE_INSTANCE --mqtt-host $MQTT_HOST
EOF
    chmod +x "$SERVICE_DIR/run"
    say "  $SERVICE_DIR/run créé"
fi

say "== Démarrage"
run "svc -u '$SERVICE_DIR' || true"
run "sleep 2; svstat '$SERVICE_DIR' || true"

say ""
say "== Contrôle (à lancer dans 10 s)"
say "  tail -n 30 $LOGDIR/current"
say "  dbus-send --system --dest=com.victronenergy.Bus --print-reply / com.victronenergy.Bus.ListNames | grep r4850"
say "  ip -details link show $CAN_IFACE | grep bitrate"
say ""
say "Attention : sans module branché, le service reste en défaut (/State=2,"
say "/Connected=0) et ne force aucun setpoint. C'est voulu."
