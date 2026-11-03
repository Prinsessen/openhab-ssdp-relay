#!/usr/bin/env python3
"""openhab-ssdp-relay - SSDP relay for openHAB's UPnP bindings (Sonos and others) when the devices sit
behind Wi-Fi gear or a VLAN boundary that drops multicast. See README.md.

Runs on a wired host in the devices' VLAN. Four jobs, one socket bound to UDP 1900:

  1. openHAB's multicast M-SEARCH (openHAB -> 239.255.255.250:1900, if your router forwards it here)
     is re-sent as a *broadcast* (255.255.255.255:1900) - the form of search that reaches devices
     behind a controller that drops multicast - and the devices' unicast answers are forwarded to the
     port openHAB sent from (jUPnP's multicastResponsePort, 1901).
  2. The devices' own NOTIFY announcements (ssdp:alive / ssdp:byebye, as far as they reach the wire)
     are forwarded unicast to openHAB's port 1900, where jUPnP's multicast receiver takes them like
     any NOTIFY.
  3. Every `search_interval` seconds the relay searches by itself (ST upnp:rootdevice) and forwards
     the answers to openHAB's 1901 - so openHAB finds the devices even when its own multicast never
     reaches this VLAN, and a missed NOTIFY never lets a device expire from the registry.
  4. Sonos players no search reaches at all (`announce`: their IPs - e.g. one at another site, behind a
     site-to-site VPN that carries no multicast or broadcast) are announced by the relay itself every
     `search_interval`: it fetches the player's description page (proof it is there, and its UDN) and
     sends openHAB three NOTIFY ssdp:alive pointing at it. jUPnP fetches the page itself and the
     binding finds the player. A player that does not answer is not announced, so it expires in
     openHAB as a switched-off one would.

Only datagrams matching `filters` (default: RINCON / Sonos) are forwarded, so openHAB does not
discover every UPnP device in the VLAN; add a string to relay another device. All logic is in
`Relay` (pure, unit-tested in tests/test_relay.py); `main()` is the socket loop. Standard library
only; Python 3.9+; Linux (IP_PKTINFO).
"""
import json, logging, os, re, select, socket, struct, sys, time, urllib.request

LOG = logging.getLogger('openhab-ssdp-relay')
SSDP_GROUP = '239.255.255.250'
SSDP_PORT = 1900
BROADCAST = '255.255.255.255'

DEFAULT_CONFIG = {
    'bind_ip': None,                 # the relay host's address in the devices' VLAN; None = guess from the default route
    'upstream': [],                  # openHAB host(s) whose searches are relayed and who get the devices' announcements - REQUIRED
    'upstream_notify_port': 1900,    # jUPnP's multicast receiver (bound *:1900, takes unicast too)
    'upstream_response_port': 1901,  # jUPnP's multicastResponsePort (/var/lib/openhab/etc/org.jupnp.cfg)
    'filters': ['RINCON', 'Sonos'],  # a datagram is forwarded only if it contains one of these
    'search_interval': 300,          # the relay's own broadcast search, seconds (0 = off)
    'mx': 3,                         # MX of the relay's own search; answers are taken for mx + 2 s
    'local_net': '',                 # prefix of the devices' addresses, e.g. '192.168.10.'; '' = any source accepted
    'announce': [],                  # Sonos players no search reaches (another site): announced by the relay itself
}
DESCRIPTION_URL = 'http://%s:1400/xml/device_description.xml'
ZONEPLAYER = 'urn:schemas-upnp-org:device:ZonePlayer:1'


def _tag(xml, name):
    m = re.search(rb'<' + name + rb'>([^<]+)</' + name + rb'>', xml)
    return m.group(1).decode('utf-8', 'replace').strip() if m else None


def parse_description(xml):
    """A player's device_description.xml -> {'udn', 'model', 'room'}, or None if it is not a Sonos player.
    The first UDN is the root device's; the embedded MediaServer/MediaRenderer ones end in _MS/_MR."""
    udn = _tag(xml or b'', b'UDN')
    if not udn or 'RINCON_' not in udn:
        return None
    return {'udn': udn, 'model': _tag(xml, b'modelName'), 'room': _tag(xml, b'roomName')}


def classify(data):
    """'search' | 'notify' | 'response' | 'other' from the SSDP start line."""
    if data.startswith(b'M-SEARCH'):
        return 'search'
    if data.startswith(b'NOTIFY'):
        return 'notify'
    if data.startswith(b'HTTP/1.1 200'):
        return 'response'
    return 'other'


class Relay:
    """Pure relay logic. on_packet()/tick() return the datagrams to send as [(payload, (ip, port)), ...]."""

    def __init__(self, cfg):
        self.cfg = dict(DEFAULT_CONFIG, **cfg)
        self.ip = self.cfg['bind_ip']
        self.upstream = list(self.cfg['upstream'])
        self.filters = [f.encode() for f in self.cfg['filters']]
        self.pending = {}            # (ip, port) -> deadline: who is waiting for search answers
        self.next_search = 0.0
        self.counts = {'search': 0, 'response': 0, 'notify': 0, 'dropped': 0, 'announce': 0}
        self.next_announce = 0.0
        self.announced = {}          # ip -> the last description seen (None = did not answer), for the log

    # --- helpers -------------------------------------------------------------------------------
    def matches(self, data):
        return any(f in data for f in self.filters)

    def is_local(self, ip):
        return ip.startswith(self.cfg['local_net'] or '')

    def own_search(self):
        return ('M-SEARCH * HTTP/1.1\r\nHOST: %s:%d\r\nMAN: "ssdp:discover"\r\nMX: %d\r\n'
                'ST: upnp:rootdevice\r\n\r\n' % (SSDP_GROUP, SSDP_PORT, self.cfg['mx'])).encode()

    def _purge(self, now):
        self.pending = {k: d for k, d in self.pending.items() if d > now}

    # --- the three jobs ------------------------------------------------------------------------
    def on_packet(self, data, src, dst, now):
        """data: bytes; src: (ip, port); dst: the datagram's destination address; now: time.time()."""
        ip, port = src
        if ip == self.ip:                       # our own broadcast comes back on the same socket
            return []
        kind = classify(data)
        self._purge(now)
        if kind == 'search':
            if ip not in self.upstream:         # a station's or another host's search: not ours to relay
                return []
            self.pending[(ip, port)] = now + self._mx_of(data) + 2
            self.counts['search'] += 1
            return [(data, (BROADCAST, SSDP_PORT))]
        if kind == 'response':
            if not (self.is_local(ip) and self.matches(data)) or not self.pending:
                self.counts['dropped'] += 1
                return []
            self.counts['response'] += 1
            return [(data, target) for target in self.pending]
        if kind == 'notify':
            if not (self.is_local(ip) and self.matches(data)):
                self.counts['dropped'] += 1
                return []
            self.counts['notify'] += 1
            return [(data, (up, self.cfg['upstream_notify_port'])) for up in self.upstream]
        return []

    def tick(self, now):
        """Called about once a second. Emits the relay's own search when due."""
        if not self.cfg['search_interval'] or now < self.next_search:
            return []
        self.next_search = now + self.cfg['search_interval']
        for up in self.upstream:
            self.pending[(up, self.cfg['upstream_response_port'])] = now + self.cfg['mx'] + 2
        return [(self.own_search(), (BROADCAST, SSDP_PORT))]

    # --- job 4: players no search reaches ------------------------------------------------------
    def announce_due(self, now):
        """The players to fetch and announce now: all of them every search_interval (300 s if 0)."""
        if not self.cfg['announce'] or now < self.next_announce:
            return []
        self.next_announce = now + (self.cfg['search_interval'] or 300)
        return list(self.cfg['announce'])

    def announce(self, ip, desc):
        """Three NOTIFY ssdp:alive for the player at `ip` to every upstream; nothing if it did not answer."""
        if not desc:
            return []
        age = max(1800, 3 * (self.cfg['search_interval'] or 300))
        out = []
        for nt, usn in (('upnp:rootdevice', desc['udn'] + '::upnp:rootdevice'), (desc['udn'], desc['udn']),
                        (ZONEPLAYER, desc['udn'] + '::' + ZONEPLAYER)):
            msg = ('NOTIFY * HTTP/1.1\r\nHOST: %s:%d\r\nCACHE-CONTROL: max-age=%d\r\nLOCATION: %s\r\nNT: %s\r\n'
                   'NTS: ssdp:alive\r\nSERVER: Linux UPnP/1.0 Sonos/1.0 (openhab-ssdp-relay announce)\r\nUSN: %s\r\n\r\n'
                   % (SSDP_GROUP, SSDP_PORT, age, DESCRIPTION_URL % ip, nt, usn)).encode()
            out += [(msg, (up, self.cfg['upstream_notify_port'])) for up in self.upstream]
        self.counts['announce'] += 1
        return out

    def announce_changed(self, ip, desc):
        """True when a player started or stopped answering since the last fetch (the log says so once)."""
        was = self.announced.get(ip, 'never')
        self.announced[ip] = desc
        return was == 'never' or (was is None) != (desc is None)

    @staticmethod
    def _mx_of(data):
        for line in data.split(b'\r\n'):
            if line.upper().startswith(b'MX:'):
                try:
                    return min(int(line[3:].strip()), 5)
                except ValueError:
                    break
        return 3


# --- the socket loop -------------------------------------------------------------------------------
def guess_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((SSDP_GROUP, SSDP_PORT))
    return s.getsockname()[0]


def open_socket(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(('', SSDP_PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 struct.pack('4s4s', socket.inet_aton(SSDP_GROUP), socket.inet_aton(ip)))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
    s.setsockopt(socket.IPPROTO_IP, 8, 1)       # IP_PKTINFO: the destination address per datagram
    return s


def recv(sock):
    data, anc, _flags, addr = sock.recvmsg(65535, 256)
    dst = None
    for lvl, typ, d in anc:
        if lvl == socket.IPPROTO_IP and typ == 8:
            dst = socket.inet_ntoa(d[8:12])
    return data, addr, dst


def fetch_description(ip, timeout=3):
    try:
        with urllib.request.urlopen(DESCRIPTION_URL % ip, timeout=timeout) as resp:
            return parse_description(resp.read(65536))
    except OSError:
        return None


def main(argv):
    cfg_path = argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            cfg.update(json.load(f))
    logging.basicConfig(level=logging.DEBUG if os.environ.get('SONOS_RELAY_DEBUG') else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s', stream=sys.stdout)
    if not cfg.get('upstream'):
        LOG.error('config: "upstream" must name at least one openHAB host (see config.example.json)')
        sys.exit(2)
    cfg['bind_ip'] = cfg.get('bind_ip') or guess_ip()
    relay = Relay(cfg)
    sock = open_socket(cfg['bind_ip'])
    LOG.info('listening on %s:%d, upstream %s (notify -> :%d, answers -> :%d), filters %s, own search every %s s, announcing %s',
             cfg['bind_ip'], SSDP_PORT, relay.upstream, cfg['upstream_notify_port'],
             cfg['upstream_response_port'], cfg['filters'], cfg['search_interval'], cfg['announce'] or 'none')
    last_summary = time.time()
    while True:
        now = time.time()
        for payload, target in relay.tick(now):
            sock.sendto(payload, target)
            LOG.debug('own search -> %s', target)
        for ip in relay.announce_due(now):          # a few seconds at most, once per search_interval
            desc = fetch_description(ip)
            if relay.announce_changed(ip, desc):
                if desc:
                    LOG.info('announcing %s (%s, %s, %s) to %s', ip, desc['model'], desc['room'], desc['udn'], relay.upstream)
                else:
                    LOG.warning('%s does not answer on :1400 - not announced (it expires in openHAB)', ip)
            for payload, target in relay.announce(ip, desc):
                try:
                    sock.sendto(payload, target)
                except OSError as e:
                    LOG.warning('send to %s failed: %s', target, e)
        rl, _, _ = select.select([sock], [], [], 1.0)
        if rl:
            data, src, dst = recv(sock)
            out = relay.on_packet(data, src, dst, time.time())
            for payload, target in out:
                try:
                    sock.sendto(payload, target)
                except OSError as e:
                    LOG.warning('send to %s failed: %s', target, e)
            if out:
                LOG.debug('%s from %s:%d (dst %s) -> %s', classify(data), src[0], src[1], dst,
                          ', '.join('%s:%d' % t for _, t in out))
        if now - last_summary >= 3600:
            LOG.info('last hour: %s', relay.counts)
            relay.counts = {k: 0 for k in relay.counts}
            last_summary = now


if __name__ == '__main__':
    main(sys.argv)
