#!/bin/sh
# Bring can1 up at 125 kbit/s for the Huawei module and keep it that way.
#
# Why this exists: Venus OS configures can1 from the CAN profile (currently
# "VE.Can (250 kbit/s)") and runs vecan-dbus on it.  The rectifier is fixed at
# 125 kbit/s, so the profile cannot express what we need and the two services
# cannot share the bus.  We stop vecan-dbus and take the interface over.
#
# Idempotent: safe to run at every boot.
set -eu

IFACE="${R4850_CAN_IFACE:-can1}"
BITRATE="${R4850_CAN_BITRATE:-125000}"

log() { echo "r4850-can-up: $*"; }

# Stop the VE.Can stack: it would reconfigure the bitrate and open its own socket.
if [ -d /service/vecan-dbus.can1 ]; then
    svc -d /service/vecan-dbus.can1 2>/dev/null || true
    log "vecan-dbus.can1 arrêté"
fi

if ! ip link show "$IFACE" >/dev/null 2>&1; then
    log "interface $IFACE absente, rien à faire"
    exit 1
fi

current=$(ip -details link show "$IFACE" | sed -n 's/.*bitrate \([0-9]*\).*/\1/p')
if [ "$current" = "$BITRATE" ]; then
    log "$IFACE déjà à $BITRATE bit/s"
else
    ip link set "$IFACE" down
    ip link set "$IFACE" type can bitrate "$BITRATE" restart-ms 100 2>/dev/null \
        || ip link set "$IFACE" type can bitrate "$BITRATE"
    ip link set "$IFACE" up
    log "$IFACE passé de ${current:-?} à $BITRATE bit/s"
fi
