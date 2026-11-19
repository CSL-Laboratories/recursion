"""The destination check of the network handle: may this plugin reach this place?

One pure function, ``check_destination``. It opens no socket and looks up no name: the
caller looks the name up and passes the addresses as data. It answers the one address
to use, or raises ``Refused`` with one of three codes:

* ``destination_not_granted``: the destination is not in the plugin's granted
  ``network_destinations``. Host and port must match a granted entry exactly. A
  destination with no port means port 443 only, in the grant and in the request. A port
  outside 1 to 65535 never matches (the manifest's host pattern accepts five digits, so
  ``example.com:99999`` parses there and is refused here).
* ``destination_reserved``: the host is one the core reserves for an effect owner,
  as ``ReservedEffects.host_reserved`` answers it (called, never copied).
* ``address_refused``, with the class as its detail: an address the plugin may not
  reach (see ``_CLASSES``), or ``ip_literal`` when the destination names an address
  instead of a host name. Every address given is checked in the order given; the
  first that passes is answered, so one refused address among good ones is skipped.
  When none passes, the detail names each refused class once, in the order met.

An IPv4 address written inside IPv6 (``::ffff:0:0/96`` and ``64:ff9b::/96``) is
checked as the IPv4 address inside. The person's ``allow_private_addresses`` (default
off; it loosens safety) lets the classes ``private`` and ``ip_literal`` pass and no
other. The address ranges are written out here rather than taken from
``ipaddress``'s ``is_private`` and similar properties, whose meaning has changed
between Python versions.
"""

from __future__ import annotations

import ipaddress

from .errors import Refused
from .manifest import _HOST, ReservedEffects

__all__ = ["check_destination"]

_DEFAULT_PORT = 443


def _nets(*cidrs: str) -> tuple:
    return tuple(ipaddress.ip_network(c) for c in cidrs)


# Each class an address is refused for, with its ranges. The first class that holds the
# address names it.
_CLASSES = (
    ("unspecified", _nets("0.0.0.0/32", "::/128")),
    ("loopback", _nets("127.0.0.0/8", "::1/128")),
    ("link_local", _nets("169.254.0.0/16", "fe80::/10")),
    ("private", _nets("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")),
    ("carrier_grade_nat", _nets("100.64.0.0/10")),
    ("multicast", _nets("224.0.0.0/4", "ff00::/8")),
    ("broadcast", _nets("255.255.255.255/32")),
)
# The classes the person's allow_private_addresses lets pass.
_PRIVATE_ALLOWED = frozenset({"private", "ip_literal"})
_V4_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _split(destination: str) -> tuple | None:
    """``(host, port)`` of a destination in the manifest's form, or ``None``."""
    if type(destination) is not str or not _HOST.fullmatch(destination):
        return None
    host, sep, port = destination.partition(":")
    if not sep:
        return host, _DEFAULT_PORT
    if port.startswith("0") or not 1 <= int(port) <= 65535:
        return None
    return host, int(port)


def _is_ip_literal(host: str) -> bool:
    # No top-level domain begins with a digit, and the system's resolver reads forms
    # such as "127.1" or "0x7f.0.0.1" as addresses, so a last label that begins with a
    # digit is an address written as a destination.
    return host.rsplit(".", 1)[-1][:1].isdigit()


def _address_class(address) -> str | None:
    if address.version == 6:
        if address in _V4_MAPPED:
            address = address.ipv4_mapped
        elif address in _NAT64:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    for name, nets in _CLASSES:
        if any(address.version == n.version and address in n for n in nets):
            return name
    return None


def check_destination(destination: str, granted, reserved: ReservedEffects, addresses, *,
                      allow_private_addresses: bool = False) -> str:
    """Answer the one address this plugin may use for ``destination``, or refuse.

    ``granted`` is the plugin's granted ``network_destinations``; ``reserved`` is the
    core's ``ReservedEffects``; ``addresses`` is the list of addresses a lookup of the
    name gave, as data (this function never looks a name up). Raises ``Refused`` with
    ``destination_not_granted``, ``destination_reserved`` or ``address_refused``.
    Arguments of the wrong type raise ``TypeError``; an empty or malformed address list
    raises ``ValueError`` (a caller's mistake, never an answer).
    """
    if type(destination) is not str or type(allow_private_addresses) is not bool:
        raise TypeError("destination is a string and allow_private_addresses a bool")
    if not isinstance(reserved, ReservedEffects):
        raise TypeError("reserved must be a ReservedEffects")
    if isinstance(granted, (str, bytes)) or isinstance(addresses, (str, bytes)):
        raise TypeError("granted and addresses are lists of strings")
    granted = tuple(granted)
    addresses = tuple(addresses)
    if any(type(g) is not str for g in granted) or any(type(a) is not str for a in addresses):
        raise TypeError("granted and addresses are lists of strings")

    asked = _split(destination)
    if asked is None or asked not in {_split(g) for g in granted} - {None}:
        raise Refused("destination_not_granted")
    if reserved.host_reserved(destination):
        raise Refused("destination_reserved")
    if _is_ip_literal(asked[0]) and not allow_private_addresses:
        raise Refused("address_refused", "ip_literal")
    if not addresses:
        raise ValueError("no address given")
    parsed = tuple(ipaddress.ip_address(a) for a in addresses)
    refused = []
    for address in parsed:
        name = _address_class(address)
        if name is None or (allow_private_addresses and name in _PRIVATE_ALLOWED):
            return str(address)
        if name not in refused:
            refused.append(name)
    raise Refused("address_refused", ",".join(refused))
