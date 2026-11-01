#!/bin/bash
# Install or update openhab-ssdp-relay on the relay host (a wired Linux host in the devices' VLAN).
# Run as root ON THE RELAY HOST, from the directory the files were copied to:
#   sudo bash install.sh
# Idempotent: re-run after changing ssdp_relay.py or config.json.
# Firewall: the relay must receive UDP 1900 - see the lines printed at the end (not added here).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DST=/opt/openhab-ssdp-relay
[ -f "$SRC/config.json" ] || { echo "no config.json next to install.sh - copy config.example.json and set upstream"; exit 1; }
install -d -m 755 "$DST"
install -m 644 "$SRC/ssdp_relay.py" "$SRC/config.json" "$DST/"
install -m 644 "$SRC/openhab-ssdp-relay.service" /etc/systemd/system/openhab-ssdp-relay.service
python3 -m py_compile "$DST/ssdp_relay.py"
systemctl daemon-reload
systemctl enable --now openhab-ssdp-relay.service
systemctl restart openhab-ssdp-relay.service
sleep 2
systemctl --no-pager --lines=5 status openhab-ssdp-relay.service || true
cat <<'TXT'

The relay must be able to receive UDP 1900: the devices' answers and announcements, and (if your
router forwards it) openHAB's multicast M-SEARCH. With nftables, in your input chain, for example:
    udp dport 1900 ip saddr <devices-subnet>/24 accept
    udp dport 1900 ip daddr 239.255.255.250 accept
With ufw:  ufw allow from <devices-subnet>/24 to any port 1900 proto udp
Watch the relay:  journalctl -u openhab-ssdp-relay -f
TXT
