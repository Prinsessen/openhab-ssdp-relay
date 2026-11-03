# openhab-ssdp-relay

A small SSDP relay for openHAB's UPnP bindings (Sonos first of all) when the devices sit where
**multicast does not reach them**: behind Wi-Fi gear that drops multicast but passes broadcast
(enterprise controllers such as FortiWLC/Meru, and some Aruba, Cisco and Unifi setups with multicast
filtering), or in a VLAN your router does not route multicast into. One Python file, standard library
only, run as a systemd service on a **wired** Linux host in the devices' VLAN.

It is not another mDNS/multicast reflector. Those forward multicast between VLANs and help when the
*router* is the obstacle. This relay helps when the obstacle is between the wire and the device
(the controller), or when there is no multicast routing at all.

## What it does

One UDP socket bound to port 1900 on the relay host, four jobs (the fourth below the diagram):

```
 openHAB (other VLAN)                              relay host (devices' VLAN, wired)                        devices (e.g. Sonos)
 ─────────────────────                             ────────────────────────────────                        ────────────────────
 jUPnP M-SEARCH ─multicast 239.255.255.250─(if routed)─► [1] re-sent as BROADCAST 255.255.255.255:1900 ─────────────────────► answer
 jUPnP :1901 ◄──unicast─────────────────────────────── [1] each answer forwarded to the port openHAB searched from ◄─unicast─

 jUPnP :1900 ◄──unicast─────────────────────────────── [2] NOTIFY (ssdp:alive / byebye) forwarded as unicast  ◄──broadcast NOTIFY─

 jUPnP :1901 ◄──unicast─────────────────────────────── [3] every 300 s the relay searches by itself (ST upnp:rootdevice)
                                                           and forwards the answers - works even when openHAB's
                                                           multicast never reaches this VLAN; keeps devices from expiring
```

- Payloads are never rewritten. jUPnP receives exactly what the device sent; the `LOCATION` URLs
  point at the devices, which openHAB reaches by unicast as usual.
- Only datagrams containing one of `filters` (default `RINCON`, `Sonos`) are forwarded, so openHAB
  does not discover every UPnP device in the VLAN. Add a string to relay another device type.
- Searches from other hosts (a phone, a TV) are ignored; only `upstream` hosts are served.

**[4] Sonos players at another site.** A player behind a site-to-site VPN gets no SSDP at all: VPNs
rarely carry multicast or broadcast, and a Sonos player does not answer a unicast M-SEARCH. Its
description page (`http://<ip>:1400/xml/device_description.xml`) does answer across the VPN, and that
is all jUPnP needs. List such players by IP in `announce`; every `search_interval` the relay fetches
each one's page (3 s timeout - proof it is there, and its UDN) and sends openHAB three `NOTIFY ssdp:alive`
(rootdevice, the UDN, ZonePlayer) pointing at it, with `max-age` three intervals (at least 1800 s).
jUPnP fetches the page itself and the binding finds the player. A player that does not answer is not
announced and expires in openHAB like one switched off; the log says so once, and again when it is
back. The player must reach openHAB's HTTP port (8080) across the VPN too, for the binding's events
and for audio sinks. The relay host only needs a route to the player; it need not be at that site.

## Requirements

- Python 3.9+ on Linux (uses `IP_PKTINFO` to tell broadcast from multicast per datagram).
- The relay host wired in the devices' VLAN, with UDP 1900 open on its firewall (see `install.sh`).
- Unicast from the relay host to the openHAB host on UDP 1900 and 1901, and from the devices to
  openHAB's HTTP port (UPnP event callbacks) - the usual firewall rules between the VLANs.
- openHAB with jUPnP's `multicastResponsePort` fixed, so answers have a known port to go to:
  `/var/lib/openhab/etc/org.jupnp.cfg` (or `userdata/etc/`) with
  ```
  multicastResponsePort=1901
  ```
  **A change to that file needs a restart of the openHAB process**; changed at runtime, jUPnP's router
  can fail to shut down cleanly and event callbacks are lost afterwards although discovery works.
  (`systemctl restart openhab`; check `systemctl show openhab --property=ActiveEnterTimestamp`.)
- openHAB's firewall allowing UDP 1900/1901 from the relay host.

## Install

```bash
git clone https://github.com/Prinsessen/openhab-ssdp-relay.git
cd openhab-ssdp-relay
cp config.example.json config.json      # set "upstream" to your openHAB host, "local_net" to the devices' subnet prefix
sudo bash install.sh                    # /opt/openhab-ssdp-relay + systemd service openhab-ssdp-relay
```

`config.json`:

| key | meaning | default |
|---|---|---|
| `bind_ip` | the relay host's address in the devices' VLAN | guessed from the default route |
| `upstream` | openHAB host(s) - required | `[]` |
| `upstream_notify_port` | jUPnP's multicast receiver | `1900` |
| `upstream_response_port` | jUPnP's `multicastResponsePort` | `1901` |
| `filters` | forward only datagrams containing one of these | `["RINCON", "Sonos"]` |
| `search_interval` | the relay's own search, seconds; `0` = off | `300` |
| `mx` | MX of the relay's own search; answers are taken for `mx + 2` s | `3` |
| `local_net` | prefix of the devices' addresses, e.g. `"192.168.10."`; `""` = any | `""` |

## Is it working?

1. `systemctl status openhab-ssdp-relay`; `journalctl -u openhab-ssdp-relay` shows
   `listening on <ip>:1900 ...` and an hourly summary `last hour: {'search': n, 'response': n, 'notify': n, 'dropped': n, 'announce': n}`.
   With `announce` set: `announcing <ip> (<model>, <room>, uuid:RINCON_...)` once after a start, or
   `<ip> does not answer on :1400` while the player or the route to it is down.
   `Environment=SONOS_RELAY_DEBUG=1` in the unit logs one line per datagram.
2. In openHAB the devices appear in the inbox / the things go ONLINE within `search_interval`.
3. If the things are ONLINE but their channels never update: the UPnP **event** path is broken, not
   discovery - see the `org.jupnp.cfg` note above and check that the devices reach openHAB's HTTP port.

`tcpdump -ni <if> udp port 1900` on the relay host shows the whole conversation; mind the difference
between `dst 255.255.255.255` (broadcast) and `dst 239.255.255.250` (multicast) - a socket bound to
port 1900 receives both, which is easy to misread.

## Tests

```bash
python3 -m unittest discover -s tests
```

The relay's logic (`Relay.on_packet`, `Relay.tick`, `Relay.announce`, `parse_description`) is pure and
covered by 22 offline tests; the socket loop and the HTTP fetch are not.

## Limits, honestly

- Proved on one installation: seven Sonos players on a FortiWLC-50D (tunnelled ESS), openHAB 5.2 in
  another VLAN behind a FortiGate, the relay on a Debian VM. That a controller delivers a wired
  broadcast to its wireless stations was measured there, not guaranteed elsewhere.
- Answers from devices outside `local_net` and datagrams not matching `filters` are dropped.
- `announce` is for Sonos players (port 1400, ZonePlayer); another device type would need its own
  description URL and device type. A player at another site is added by hand - nothing discovers it.
  Proved with one Play:1 across a site-to-site VPN (2026-11-03).
- No IPv6.

## Origin

Written 2026-11-01 for a house where the Sonos binding had never found its players across the VLAN
boundary (the investigation that led here took two days of measuring switches, a FortiGate and a
wireless controller). This repository is a copy of that house's relay; fixes are made there and
copied here by hand, never the other way round.

License: EPL-2.0. Author: Nanna Agesen.
