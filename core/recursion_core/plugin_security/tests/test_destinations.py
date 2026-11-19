"""The destination check of the network handle (``destinations.check_destination``).

One test per refusal code and per address class, plus the rules that join them: a
destination with no port means 443 only, every address given is checked and the first
that passes is answered, and the check never opens a socket or looks up a name.
"""

from __future__ import annotations

import ipaddress
import socket
import unittest
from unittest import mock

from recursion_core.plugin_security.errors import Refused
from recursion_core.plugin_security.manifest import ReservedEffects

PUBLIC = "93.184.216.34"
PUBLIC6 = "2606:2800:220:1:248:1893:25c8:1946"
NONE_RESERVED = ReservedEffects()
# The known cloud platform addresses that hold secrets, as the module lists them.
PLATFORM_ADDRESSES = ("169.254.169.254", "169.254.170.2", "100.100.100.200", "168.63.129.16",
                      "fd00:ec2::254", "fd20:ce::254")


def check(destination, granted, addresses, reserved=NONE_RESERVED, **kwargs):
    # Imported here so that each test, not only the module, is red where the function
    # does not exist.
    from recursion_core.plugin_security.destinations import check_destination
    return check_destination(destination, granted, reserved, addresses, **kwargs)


class DestinationCheckTests(unittest.TestCase):
    def assertRefused(self, code, detail, destination, granted, addresses, **kwargs):
        with self.assertRaises(Refused) as caught:
            check(destination, granted, addresses, **kwargs)
        self.assertEqual((caught.exception.code, caught.exception.detail), (code, detail))

    def assertAddressClass(self, name, *addresses):
        for address in addresses:
            with self.subTest(address=address):
                self.assertRefused("address_refused", name, "api.example.com", ["api.example.com"],
                                   [address])

    # -- the codes -------------------------------------------------------------------

    def test_a_granted_destination_answers_its_address(self):
        self.assertEqual(check("api.example.com", ["api.example.com"], [PUBLIC]), PUBLIC)
        self.assertEqual(check("api.example.com", ["api.example.com"], [PUBLIC6]), PUBLIC6)

    def test_destination_not_granted_when_not_in_the_grant(self):
        self.assertRefused("destination_not_granted", "", "other.example.com", ["api.example.com"], [PUBLIC])
        self.assertRefused("destination_not_granted", "", "api.example.com", [], [PUBLIC])
        self.assertRefused("destination_not_granted", "", "sub.api.example.com", ["api.example.com"], [PUBLIC])

    def test_destination_not_granted_when_the_port_differs(self):
        self.assertRefused("destination_not_granted", "", "api.example.com:8444",
                           ["api.example.com:8443"], [PUBLIC])
        self.assertRefused("destination_not_granted", "", "api.example.com",
                           ["api.example.com:8443"], [PUBLIC])
        self.assertEqual(check("api.example.com:8443", ["api.example.com:8443"], [PUBLIC]), PUBLIC)

    def test_no_port_means_port_443_only(self):
        self.assertEqual(check("api.example.com:443", ["api.example.com"], [PUBLIC]), PUBLIC)
        self.assertEqual(check("api.example.com", ["api.example.com:443"], [PUBLIC]), PUBLIC)
        self.assertRefused("destination_not_granted", "", "api.example.com:80", ["api.example.com"], [PUBLIC])
        self.assertRefused("destination_not_granted", "", "api.example.com:8443",
                           ["api.example.com"], [PUBLIC])

    def test_destination_not_granted_for_a_port_outside_1_to_65535(self):
        # The manifest's host pattern accepts five digits, so these parse there.
        self.assertRefused("destination_not_granted", "", "example.com:99999", ["example.com:99999"], [PUBLIC])
        self.assertRefused("destination_not_granted", "", "example.com:65536", ["example.com:65536"], [PUBLIC])
        self.assertRefused("destination_not_granted", "", "example.com:0", ["example.com:0"], [PUBLIC])
        self.assertEqual(check("example.com:65535", ["example.com:65535"], [PUBLIC]), PUBLIC)
        self.assertEqual(check("example.com:1", ["example.com:1"], [PUBLIC]), PUBLIC)

    def test_destination_not_granted_for_a_port_written_with_a_leading_zero(self):
        self.assertRefused("destination_not_granted", "", "example.com:0443", ["example.com:443"], [PUBLIC])
        self.assertRefused("destination_not_granted", "", "example.com:0443", ["example.com:0443"], [PUBLIC])

    def test_destination_not_granted_for_a_destination_outside_the_manifest_form(self):
        for asked in ("API.example.com", "example", "[::1]:443", "example.com.", "", "api.example.com:"):
            with self.subTest(asked=asked):
                self.assertRefused("destination_not_granted", "", asked, [asked, "api.example.com"], [PUBLIC])

    def test_destination_reserved(self):
        reserved = ReservedEffects(hosts=frozenset({"venue.example"}))
        for asked in ("venue.example", "api.venue.example", "api.venue.example:8443"):
            with self.subTest(asked=asked):
                self.assertRefused("destination_reserved", "", asked, [asked], [PUBLIC], reserved=reserved)
        self.assertEqual(check("venue.example.org", ["venue.example.org"], [PUBLIC], reserved=reserved), PUBLIC)

    def test_the_reserved_check_is_the_cores_own_method(self):
        seen = []

        class Recording(ReservedEffects):
            def host_reserved(self, destination):
                seen.append(destination)
                return True

        self.assertRefused("destination_reserved", "", "api.example.com:8443", ["api.example.com:8443"],
                           [PUBLIC], reserved=Recording())
        self.assertEqual(seen, ["api.example.com:8443"])

    def test_a_destination_not_granted_is_refused_before_the_reserved_check(self):
        reserved = ReservedEffects(hosts=frozenset({"venue.example"}))
        self.assertRefused("destination_not_granted", "", "venue.example", ["api.example.com"], [PUBLIC],
                           reserved=reserved)

    # -- the address classes ---------------------------------------------------------

    def test_address_refused_loopback(self):
        self.assertAddressClass("loopback", "127.0.0.1", "127.255.255.254", "::1")

    def test_address_refused_unspecified(self):
        self.assertAddressClass("unspecified", "0.0.0.0", "::")

    def test_address_refused_link_local_including_the_metadata_address(self):
        self.assertAddressClass("link_local", "169.254.0.1", "fe80::1", "febf::1")
        self.assertAddressClass("platform_address", "169.254.169.254")

    def test_address_refused_private(self):
        self.assertAddressClass("private", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255",
                                "192.168.0.1", "192.168.255.255", "fc00::1", "fdff::1")

    def test_address_refused_carrier_grade_nat(self):
        self.assertAddressClass("carrier_grade_nat", "100.64.0.1", "100.127.255.255")

    def test_address_refused_multicast(self):
        self.assertAddressClass("multicast", "224.0.0.1", "239.255.255.255", "ff02::1")

    def test_address_refused_broadcast(self):
        self.assertAddressClass("broadcast", "255.255.255.255")

    def test_ipv4_written_inside_ipv6_is_checked_as_the_ipv4_inside(self):
        self.assertAddressClass("loopback", "::ffff:127.0.0.1")
        self.assertAddressClass("private", "::ffff:10.0.0.1")
        self.assertAddressClass("platform_address", "::ffff:169.254.169.254")
        # Python 3.13 changed how such an address is written, so the answer is compared
        # with ipaddress's own spelling of it.
        self.assertEqual(check("api.example.com", ["api.example.com"], ["::ffff:" + PUBLIC]),
                         str(ipaddress.ip_address("::ffff:" + PUBLIC)))

    def test_ipv4_written_inside_the_nat64_prefix_is_checked_as_the_ipv4_inside(self):
        self.assertAddressClass("loopback", "64:ff9b::7f00:1")
        self.assertAddressClass("platform_address", "64:ff9b::a9fe:a9fe")
        self.assertAddressClass("private", "64:ff9b::c0a8:101")
        self.assertEqual(check("api.example.com", ["api.example.com"], ["64:ff9b::5db8:d822"]),
                         str(ipaddress.ip_address("64:ff9b::5db8:d822")))

    def test_addresses_next_to_the_refused_ranges_pass(self):
        for address in ("126.255.255.255", "128.0.0.1", "11.0.0.1", "172.15.255.255", "172.32.0.1",
                        "192.167.255.255", "192.169.0.1", "100.63.255.255", "100.128.0.1",
                        "169.253.255.255", "169.255.0.1", "223.255.255.255",
                        "::ffff:" + PUBLIC, "64:ff9b::5db8:d822"):
            with self.subTest(address=address):
                self.assertEqual(check("api.example.com", ["api.example.com"], [address]),
                                 str(ipaddress.ip_address(address)))

    def test_address_refused_ip_literal(self):
        for asked in (PUBLIC, PUBLIC + ":8443", "127.1", "0x7f.0.0.1", "127.0.0.0x1", "8.8.8.8"):
            with self.subTest(asked=asked):
                self.assertRefused("address_refused", "ip_literal", asked, [asked], [PUBLIC])

    def test_a_name_whose_first_label_begins_with_a_digit_is_a_name(self):
        self.assertEqual(check("1.example.com", ["1.example.com"], [PUBLIC]), PUBLIC)

    # -- the known cloud platform addresses ------------------------------------------

    def test_platform_addresses_are_refused_under_every_setting(self):
        for address in PLATFORM_ADDRESSES:
            for allow in (False, True):
                with self.subTest(address=address, allow_private_addresses=allow):
                    self.assertRefused("address_refused", "platform_address", "api.example.com",
                                       ["api.example.com"], [address], allow_private_addresses=allow)

    def test_platform_addresses_written_inside_ipv6_are_refused_under_every_setting(self):
        for address in PLATFORM_ADDRESSES:
            if ":" in address:
                continue
            packed = int(ipaddress.IPv4Address(address))
            for written in ("::ffff:" + address, str(ipaddress.IPv6Address((0x64FF9B << 96) | packed))):
                for allow in (False, True):
                    with self.subTest(written=written, allow_private_addresses=allow):
                        self.assertRefused("address_refused", "platform_address", "api.example.com",
                                           ["api.example.com"], [written], allow_private_addresses=allow)

    def test_a_platform_address_named_as_the_destination_is_refused_with_the_switch_on(self):
        for address in PLATFORM_ADDRESSES:
            if ":" in address:
                continue
            with self.subTest(address=address):
                self.assertRefused("address_refused", "platform_address", address, [address], [address],
                                   allow_private_addresses=True)

    def test_a_platform_address_with_a_zone_id_is_refused(self):
        for address in ("fd20:ce::254%eth0", "fd00:ec2::254%1"):
            with self.subTest(address=address):
                self.assertRefused("address_refused", "platform_address", "api.example.com",
                                   ["api.example.com"], [address], allow_private_addresses=True)

    def test_the_platform_list_holds_exact_addresses_only(self):
        for address in ("168.63.129.15", "168.63.129.17", "168.63.128.16"):
            with self.subTest(address=address):
                self.assertEqual(check("api.example.com", ["api.example.com"], [address]), address)
        self.assertEqual(check("api.example.com", ["api.example.com"], ["fd20:ce::253"],
                               allow_private_addresses=True), "fd20:ce::253")

    def test_a_platform_address_among_good_ones_is_skipped_and_named_once(self):
        self.assertEqual(check("api.example.com", ["api.example.com"], ["168.63.129.16", PUBLIC]), PUBLIC)
        self.assertRefused("address_refused", "platform_address,private", "api.example.com",
                           ["api.example.com"], ["168.63.129.16", "10.0.0.1", "169.254.169.254"])

    # -- IPv4 carried inside IPv6, and ranges that are not routable -------------------

    # Each IPv6 form with the IPv4 address it carries; the form is judged as that address.
    CARRIED = (
        ("::ffff:0:a9fe:a9fe", "platform_address"),  # IPv4-translated ::ffff:0:0:0/96
        ("::ffff:0:7f00:1", "loopback"),
        ("64:ff9b:1::a9fe:a9fe", "platform_address"),  # local NAT64 64:ff9b:1::/48
        ("64:ff9b:1::7f00:1", "loopback"),
        ("64:ff9b::a9fe:a9fe", "platform_address"),  # NAT64 64:ff9b::/96
        ("::127.0.0.1", "loopback"),  # IPv4-compatible ::/96
        ("::169.254.169.254", "platform_address"),
        ("2002:7f00:1::", "loopback"),  # 6to4 2002::/16
        ("2002:a9fe:a9fe::", "platform_address"),
        ("2001:0:5db8:d822::80ff:fffe", "loopback"),  # Teredo, client 127.0.0.1
        ("2001:0:5db8:d822::5601:5601", "platform_address"),  # Teredo, client 169.254.169.254
        ("2001:0:7f00:1::f7f7:f7f7", "loopback"),  # Teredo, server 127.0.0.1
    )

    def test_ipv4_carried_inside_ipv6_is_refused_as_the_ipv4_inside_under_every_setting(self):
        for address, name in self.CARRIED:
            for allow in (False, True):
                with self.subTest(address=address, allow_private_addresses=allow):
                    self.assertRefused("address_refused", name, "api.example.com", ["api.example.com"],
                                       [address], allow_private_addresses=allow)

    def test_a_public_ipv4_carried_inside_ipv6_stays_allowed(self):
        for address in ("::ffff:0:5db8:d822", "64:ff9b:1::5db8:d822", "::93.184.216.34",
                        "2002:5db8:d822::", "2001:0:5db8:d822::f7f7:f7f7"):
            for allow in (False, True):
                with self.subTest(address=address, allow_private_addresses=allow):
                    self.assertEqual(check("api.example.com", ["api.example.com"], [address],
                                           allow_private_addresses=allow), str(ipaddress.ip_address(address)))

    def test_a_private_ipv4_carried_inside_ipv6_follows_the_switch_like_the_plain_one(self):
        for address in ("::ffff:0:a00:1", "64:ff9b:1::a00:1", "::10.0.0.1", "2002:a00:1::"):
            with self.subTest(address=address):
                self.assertRefused("address_refused", "private", "api.example.com", ["api.example.com"],
                                   [address])
                self.assertEqual(check("api.example.com", ["api.example.com"], [address],
                                       allow_private_addresses=True), str(ipaddress.ip_address(address)))

    def test_a_local_nat64_address_in_another_layout_is_judged_by_every_layout(self):
        # 64:ff9b:1:7f00:1:: read with a /48 prefix carries 127.0.1.0.
        self.assertRefused("address_refused", "loopback", "api.example.com", ["api.example.com"],
                           ["64:ff9b:1:7f00:1::5db8:d822"], allow_private_addresses=True)

    def test_unspecified_and_loopback_ipv6_keep_their_own_class(self):
        self.assertAddressClass("unspecified", "::")
        self.assertAddressClass("loopback", "::1")

    def test_address_refused_not_routable_under_every_setting(self):
        for address in ("0.0.0.1", "0.255.255.255", "240.0.0.1", "255.255.255.254", "fec0::1",
                        "feff::1", "::ffff:240.0.0.1"):
            for allow in (False, True):
                with self.subTest(address=address, allow_private_addresses=allow):
                    self.assertRefused("address_refused", "not_routable", "api.example.com",
                                       ["api.example.com"], [address], allow_private_addresses=allow)
        for asked in ("0.0.0.1", "240.0.0.1"):
            with self.subTest(asked=asked):
                self.assertRefused("address_refused", "ip_literal", asked, [asked], [asked])
                self.assertRefused("address_refused", "not_routable", asked, [asked], [asked],
                                   allow_private_addresses=True)

    def test_an_ipv6_form_named_as_the_destination_is_not_granted(self):
        for address, _ in self.CARRIED:
            for asked in (address, "[" + address + "]", "[" + address + "]:443"):
                for allow in (False, True):
                    with self.subTest(asked=asked, allow_private_addresses=allow):
                        self.assertRefused("destination_not_granted", "", asked, [asked], [address],
                                           allow_private_addresses=allow)

    def test_an_ip_literal_with_no_address_is_refused_before_the_list_is_read(self):
        # The docstring says so: ValueError comes once the destination itself has passed.
        self.assertRefused("address_refused", "ip_literal", "10.0.0.1", ["10.0.0.1"], [])
        with self.assertRaises(ValueError):
            check("10.0.0.1", ["10.0.0.1"], [], allow_private_addresses=True)

    # -- every address given is checked ---------------------------------------------

    def test_one_refused_address_among_good_ones_is_skipped(self):
        self.assertEqual(check("api.example.com", ["api.example.com"], ["127.0.0.1", "10.0.0.1", PUBLIC]),
                         PUBLIC)

    def test_the_first_address_that_passes_is_answered(self):
        self.assertEqual(check("api.example.com", ["api.example.com"], [PUBLIC6, PUBLIC]), PUBLIC6)
        self.assertEqual(check("api.example.com", ["api.example.com"], [PUBLIC, PUBLIC6]), PUBLIC)

    def test_all_addresses_refused_names_each_class_once_in_the_order_met(self):
        self.assertRefused("address_refused", "loopback,private", "api.example.com", ["api.example.com"],
                           ["127.0.0.1", "10.0.0.1", "::1", "192.168.1.1"])

    # -- the person's allow_private_addresses ----------------------------------------

    def test_allow_private_addresses_lets_private_addresses_and_ip_literals_pass_and_nothing_else(self):
        self.assertEqual(check("nas.home.example", ["nas.home.example"], ["192.168.1.10"],
                               allow_private_addresses=True), "192.168.1.10")
        self.assertEqual(check("192.168.1.10", ["192.168.1.10"], ["192.168.1.10"],
                               allow_private_addresses=True), "192.168.1.10")
        for address, name in (("127.0.0.1", "loopback"), ("169.254.169.254", "platform_address"),
                              ("100.64.0.1", "carrier_grade_nat"), ("0.0.0.0", "unspecified"),
                              ("224.0.0.1", "multicast"), ("255.255.255.255", "broadcast")):
            with self.subTest(address=address):
                self.assertRefused("address_refused", name, "nas.home.example", ["nas.home.example"],
                                   [address], allow_private_addresses=True)

    def test_allow_private_addresses_still_checks_the_addresses_of_an_ip_literal(self):
        for asked, address, name in (("169.254.169.254", "169.254.169.254", "platform_address"),
                                     ("169.254.0.1", "169.254.0.1", "link_local"),
                                     ("127.0.0.1", "127.0.0.1", "loopback"),
                                     ("127.1", "127.0.0.1", "loopback"),
                                     ("100.64.0.1", "100.64.0.1", "carrier_grade_nat")):
            with self.subTest(asked=asked):
                self.assertRefused("address_refused", name, asked, [asked], [address],
                                   allow_private_addresses=True)

    def test_allow_private_addresses_is_off_by_default(self):
        self.assertRefused("address_refused", "private", "nas.home.example", ["nas.home.example"],
                           ["192.168.1.10"])

    # -- pure: no socket, no name lookup, no mistake taken as an answer ---------------

    def test_the_check_opens_no_socket_and_looks_up_no_name(self):
        def refuse(*args, **kwargs):
            raise AssertionError("the check reached the network")

        with mock.patch.object(socket, "getaddrinfo", refuse), \
                mock.patch.object(socket, "gethostbyname", refuse), \
                mock.patch.object(socket, "socket", refuse), \
                mock.patch.object(socket, "create_connection", refuse):
            self.assertEqual(check("api.example.com", ["api.example.com"], [PUBLIC]), PUBLIC)
            self.assertRefused("address_refused", "loopback", "api.example.com", ["api.example.com"],
                               ["127.0.0.1"])

    def test_a_caller_mistake_raises_and_is_never_an_answer(self):
        with self.assertRaises(ValueError):
            check("api.example.com", ["api.example.com"], [])
        with self.assertRaises(ValueError):
            check("api.example.com", ["api.example.com"], ["not-an-address"])
        with self.assertRaises(ValueError):
            check("api.example.com", ["api.example.com"], [PUBLIC, "not-an-address"])
        with self.assertRaises(TypeError):
            check("api.example.com", ["api.example.com"], PUBLIC)
        with self.assertRaises(TypeError):
            check("api.example.com", "api.example.com", [PUBLIC])
        with self.assertRaises(TypeError):
            check("api.example.com", ["api.example.com"], [PUBLIC], allow_private_addresses=1)
        with self.assertRaises(TypeError):
            check("api.example.com", ["api.example.com"], [PUBLIC], reserved=frozenset())


if __name__ == "__main__":
    unittest.main()
