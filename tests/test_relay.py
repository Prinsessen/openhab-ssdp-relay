"""Offline tests for the Sonos SSDP relay's logic (ssdp_relay.Relay). No sockets.
Run: python3 -m unittest discover -s tests
"""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from ssdp_relay import Relay, classify, parse_description, BROADCAST  # noqa: E402

VM = '192.0.2.22'; OH = '198.51.100.21'; PLAYER = '192.0.2.174'; GROUP = '239.255.255.250'
SEARCH = b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n'
ANSWER = (b'HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age = 1800\r\nEXT:\r\nLOCATION: http://192.0.2.174:1400/xml/device_description.xml\r\n'
          b'SERVER: Linux UPnP/1.0 Sonos/83.1-61820 (ZPS18)\r\nST: upnp:rootdevice\r\nUSN: uuid:RINCON_B8E9377870F201400::upnp:rootdevice\r\n\r\n')
ALIVE = (b'NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nCACHE-CONTROL: max-age = 1800\r\nLOCATION: http://192.0.2.174:1400/xml/device_description.xml\r\n'
         b'NT: upnp:rootdevice\r\nNTS: ssdp:alive\r\nSERVER: Linux UPnP/1.0 Sonos/83.1-61820 (ZPS18)\r\nUSN: uuid:RINCON_B8E9377870F201400::upnp:rootdevice\r\n\r\n')
TV_ALIVE = ALIVE.replace(b'Sonos/83.1-61820 (ZPS18)', b'Samsung UPnP SDK/1.0').replace(b'RINCON_B8E9377870F201400', b'0c9c8a6e-1234')


def relay(**over):
    cfg = dict(bind_ip=VM, upstream=[OH], search_interval=300, local_net='192.0.2.')
    cfg.update(over)
    return Relay(cfg)


class Classify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify(SEARCH), 'search')
        self.assertEqual(classify(ANSWER), 'response')
        self.assertEqual(classify(ALIVE), 'notify')
        self.assertEqual(classify(b'GET / HTTP/1.1\r\n'), 'other')


class Job1_SearchAndAnswer(unittest.TestCase):
    def test_upstream_search_is_rebroadcast_and_the_answer_goes_back_to_its_port(self):
        r = relay()
        out = r.on_packet(SEARCH, (OH, 1901), GROUP, 100.0)
        self.assertEqual(out, [(SEARCH, (BROADCAST, 1900))], 'the multicast search is re-sent as broadcast, unchanged')
        out = r.on_packet(ANSWER, (PLAYER, 1900), VM, 101.0)
        self.assertEqual(out, [(ANSWER, (OH, 1901))], 'the answer goes to the port openHAB searched from')

    def test_answer_after_the_mx_window_is_dropped(self):
        r = relay()
        r.on_packet(SEARCH, (OH, 1901), GROUP, 100.0)       # MX 2 -> window closes at 104
        self.assertEqual(r.on_packet(ANSWER, (PLAYER, 1900), VM, 104.5), [])

    def test_a_stations_search_is_not_relayed(self):
        r = relay()
        self.assertEqual(r.on_packet(SEARCH, ('192.0.2.69', 50000), BROADCAST, 100.0), [])
        self.assertEqual(r.on_packet(ANSWER, (PLAYER, 1900), VM, 101.0), [], 'nobody is waiting')

    def test_non_sonos_answer_is_dropped(self):
        r = relay()
        r.on_packet(SEARCH, (OH, 1901), GROUP, 100.0)
        tv = ANSWER.replace(b'Sonos/83.1-61820 (ZPS18)', b'Samsung').replace(b'RINCON_B8E9377870F201400', b'x')
        self.assertEqual(r.on_packet(tv, ('192.0.2.50', 1900), VM, 101.0), [])

    def test_answer_from_outside_the_local_net_is_dropped(self):
        r = relay()
        r.on_packet(SEARCH, (OH, 1901), GROUP, 100.0)
        self.assertEqual(r.on_packet(ANSWER, ('198.51.100.47', 1900), VM, 101.0), [])

    def test_own_broadcast_coming_back_is_ignored(self):
        r = relay()
        self.assertEqual(r.on_packet(SEARCH, (VM, 1900), BROADCAST, 100.0), [])


class Job2_Notify(unittest.TestCase):
    def test_player_alive_goes_to_upstream_1900(self):
        r = relay()
        self.assertEqual(r.on_packet(ALIVE, (PLAYER, 1900), BROADCAST, 100.0), [(ALIVE, (OH, 1900))])

    def test_byebye_too(self):
        r = relay()
        bye = ALIVE.replace(b'ssdp:alive', b'ssdp:byebye')
        self.assertEqual(r.on_packet(bye, (PLAYER, 1900), BROADCAST, 100.0), [(bye, (OH, 1900))])

    def test_other_upnp_devices_are_not_forwarded(self):
        r = relay()
        self.assertEqual(r.on_packet(TV_ALIVE, ('192.0.2.50', 1900), BROADCAST, 100.0), [])

    def test_two_upstreams_both_get_it(self):
        r = relay(upstream=[OH, '198.51.100.22'])
        self.assertEqual(r.on_packet(ALIVE, (PLAYER, 1900), BROADCAST, 100.0), [(ALIVE, (OH, 1900)), (ALIVE, ('198.51.100.22', 1900))])


class Job3_OwnSearch(unittest.TestCase):
    def test_tick_searches_on_the_interval_and_routes_answers_to_1901(self):
        r = relay(search_interval=300)
        out = r.tick(1000.0)
        self.assertEqual(len(out), 1); payload, target = out[0]
        self.assertEqual(target, (BROADCAST, 1900)); self.assertIn(b'ST: upnp:rootdevice', payload)
        self.assertEqual(r.tick(1001.0), [], 'not again within the interval')
        self.assertEqual(r.on_packet(ANSWER, (PLAYER, 1900), VM, 1002.0), [(ANSWER, (OH, 1901))])
        self.assertEqual(r.on_packet(ANSWER, (PLAYER, 1900), VM, 1006.0), [], 'window mx+2 closed')
        self.assertEqual(len(r.tick(1300.0)), 1, 'due again')

    def test_interval_zero_disables_it(self):
        self.assertEqual(relay(search_interval=0).tick(1000.0), [])

    def test_answer_goes_to_both_a_waiting_openhab_and_the_own_search(self):
        r = relay()
        r.tick(100.0)
        r.on_packet(SEARCH, (OH, 1901), GROUP, 100.5)
        self.assertEqual(sorted(r.on_packet(ANSWER, (PLAYER, 1900), VM, 101.0)), [(ANSWER, (OH, 1901))],
                         'same target once: the own search and openHAB both wait on 1901')


class Counters(unittest.TestCase):
    def test_counts(self):
        r = relay()
        r.on_packet(SEARCH, (OH, 1901), GROUP, 100.0); r.on_packet(ANSWER, (PLAYER, 1900), VM, 101.0)
        r.on_packet(ALIVE, (PLAYER, 1900), BROADCAST, 102.0); r.on_packet(TV_ALIVE, ('192.0.2.50', 1900), BROADCAST, 103.0)
        self.assertEqual(r.counts, {'search': 1, 'response': 1, 'notify': 1, 'dropped': 1, 'announce': 0})


if __name__ == '__main__':
    unittest.main()


# --- job 4: announcing players no search reaches ---------------------------------------------------
# A Sonos player at another site, behind a site-to-site VPN: no multicast or broadcast crosses it, and
# the player does not answer a unicast M-SEARCH either. Its description page does answer over the VPN,
# and one NOTIFY alive pointing at it, sent to openHAB's 1900, puts it in the inbox within a second
# (measured on the original installation). So the relay announces such players itself.
REMOTE = '203.0.113.100'
DESCRIPTION = (b'<?xml version="1.0" encoding="utf-8" ?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
               b'<deviceType>urn:schemas-upnp-org:device:ZonePlayer:1</deviceType><roomName>Office</roomName>'
               b'<modelNumber>S12</modelNumber><modelName>Sonos Play:1</modelName><UDN>uuid:RINCON_000E58AA0BB001400</UDN>'
               b'<deviceList><device><deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>'
               b'<UDN>uuid:RINCON_000E58AA0BB001400_MS</UDN></device><device>'
               b'<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType><UDN>uuid:RINCON_000E58AA0BB001400_MR</UDN>'
               b'</device></deviceList></device></root>')


class Job4_Announce(unittest.TestCase):
    def test_the_description_gives_the_root_players_udn_not_an_embedded_ones(self):
        d = parse_description(DESCRIPTION)
        self.assertEqual(d, {'udn': 'uuid:RINCON_000E58AA0BB001400', 'model': 'Sonos Play:1', 'room': 'Office'})
        self.assertIsNone(parse_description(b'<html>not a player</html>'))
        self.assertIsNone(parse_description(b''))

    def test_nothing_to_announce_without_the_setting(self):
        self.assertEqual(relay().announce_due(100.0), [])

    def test_due_at_start_then_every_interval(self):
        r = relay(announce=[REMOTE])
        self.assertEqual(r.announce_due(100.0), [REMOTE])
        self.assertEqual(r.announce_due(101.0), [])
        self.assertEqual(r.announce_due(399.0), [])
        self.assertEqual(r.announce_due(400.0), [REMOTE])

    def test_three_notify_alive_to_every_upstream_pointing_at_the_players_page(self):
        r = relay(announce=[REMOTE], upstream=[OH, '198.51.100.22'])
        out = r.announce(REMOTE, parse_description(DESCRIPTION))
        self.assertEqual(sorted({t for _, t in out}), [(OH, 1900), ('198.51.100.22', 1900)])
        self.assertEqual(len(out), 6)
        for payload, _ in out:
            self.assertEqual(classify(payload), 'notify')
            self.assertIn(b'LOCATION: http://203.0.113.100:1400/xml/device_description.xml\r\n', payload)
            self.assertIn(b'NTS: ssdp:alive\r\n', payload)
            self.assertIn(b'CACHE-CONTROL: max-age=1800\r\n', payload)
            self.assertTrue(payload.endswith(b'\r\n\r\n'))
        usns = sorted(l for p, t in out if t == (OH, 1900) for l in p.split(b'\r\n') if l.startswith(b'USN:'))
        self.assertEqual(usns, [b'USN: uuid:RINCON_000E58AA0BB001400', b'USN: uuid:RINCON_000E58AA0BB001400::upnp:rootdevice',
                                b'USN: uuid:RINCON_000E58AA0BB001400::urn:schemas-upnp-org:device:ZonePlayer:1'])

    def test_a_player_that_did_not_answer_is_not_announced_so_it_expires_in_openhab(self):
        r = relay(announce=[REMOTE])
        self.assertEqual(r.announce(REMOTE, None), [])
        self.assertEqual(r.counts['announce'], 0)

    def test_the_log_hears_of_a_change_only(self):
        r = relay(announce=[REMOTE])
        d = parse_description(DESCRIPTION)
        self.assertEqual([r.announce_changed(REMOTE, x) for x in (d, d, None, None, d)], [True, False, True, False, True])

    def test_max_age_covers_three_intervals_at_least_1800_s(self):
        r = relay(announce=[REMOTE], search_interval=900)
        self.assertIn(b'max-age=2700', r.announce(REMOTE, parse_description(DESCRIPTION))[0][0])
