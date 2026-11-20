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
  reach (``platform_address`` or a class of ``_CLASSES``), or ``ip_literal`` when the
  destination names an address instead of a host name. Every address given is checked
  in the order given; the first that passes is answered, so one refused address among
  good ones is skipped. When none passes, the detail names each refused class once, in
  the order met.

An IPv4 address carried inside IPv6 (``::ffff:0:0/96``, ``::ffff:0:0:0/96``, ``::/96``,
``64:ff9b::/96``, ``64:ff9b:1::/48``, 6to4 ``2002::/16`` and Teredo ``2001::/32``) is
checked as the IPv4 address it carries, by the same rules; a form that carries
several (Teredo's server and client, the readings of a local NAT64 address) passes only
if each of them passes. ``0.0.0.0/8``,
``240.0.0.0/4`` and the old site-local ``fec0::/10`` are ``not_routable``. A known cloud
platform address that holds secrets (``_PLATFORM_ADDRESSES``) is ``platform_address``,
looked at before every class and refused under every setting. The person's
``allow_private_addresses`` (default off; it loosens safety) lets the classes
``private`` and ``ip_literal`` pass and no other. The address ranges are written out
here rather than taken from ``ipaddress``'s ``is_private`` and similar properties,
whose meaning has changed between Python versions.
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
    # Looked at last, so 0.0.0.0 stays unspecified and 255.255.255.255 broadcast.
    ("not_routable", _nets("0.0.0.0/8", "240.0.0.0/4", "fec0::/10")),
)
# The known cloud platform addresses that hold secrets (instance metadata, credentials,
# the platform's own services). Each is refused as platform_address under every setting,
# before any class is looked at: some sit in public space, two inside fc00::/7 (which the
# person's allow_private_addresses lets pass), and the rest stay refused here whatever a
# later change does to their class.
_PLATFORM_ADDRESSES = frozenset(ipaddress.ip_address(a) for a in (
    "169.254.169.254",  # instance metadata on several clouds
    "169.254.170.2",    # one cloud's container credentials and metadata
    "100.100.100.200",  # one cloud's instance metadata
    "168.63.129.16",    # one cloud's platform service, in public space
    "fd00:ec2::254",    # one cloud's IPv6 instance metadata
    "fd20:ce::254",     # another cloud's IPv6 instance metadata
))
# The classes the person's allow_private_addresses lets pass.
_PRIVATE_ALLOWED = frozenset({"private", "ip_literal"})
# IPv6 forms that carry an IPv4 address. Each is judged as the IPv4 address it carries,
# by the same rules as that address written plainly.
_V4_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
_V4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")
_V4_COMPATIBLE = ipaddress.ip_network("::/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_NAT64_LOCAL_96 = ipaddress.ip_network("64:ff9b:1::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")


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


def _carried_ipv4(address) -> tuple:
    """The IPv4 addresses an IPv6 address carries, judged in its place (none: empty)."""
    b = address.packed
    v4 = ipaddress.IPv4Address
    if any(address in n for n in (_V4_MAPPED, _V4_TRANSLATED, _NAT64, _NAT64_LOCAL_96)):
        return (v4(b[12:16]),)
    if address in _V4_COMPATIBLE and int(address) > 1:  # :: and ::1 keep their own class
        return (v4(b[12:16]),)
    if address in _NAT64_LOCAL:
        # A local translator may use any standard layout (prefix /48, /56, /64 or /96):
        # every IPv4 address one of them could read here must pass.
        return tuple(v4(x) for x in (b[6:8] + b[9:11], b[7:8] + b[9:12], b[9:13], b[12:16]))
    if address in _SIX_TO_FOUR:
        return (v4(b[2:6]),)
    if address in _TEREDO:
        return address.teredo  # (server, client): both must pass
    return ()


def _one_class(address) -> str | None:
    # Compared without a zone id: ``fd20:ce::254%eth0`` is the same machine.
    if ipaddress.ip_address(address.packed) in _PLATFORM_ADDRESSES:
        return "platform_address"
    for name, nets in _CLASSES:
        if any(address.version == n.version and address in n for n in nets):
            return name
    return None


def _address_class(address, allowed: frozenset = frozenset()) -> str | None:
    # Every address carried is judged: the first class met that ``allowed`` does not let
    # pass names the address; when each one passes, the first class met (or None).
    carried = _carried_ipv4(address) if address.version == 6 else ()
    first = None
    for one in carried or (address,):
        name = _one_class(one)
        if name is not None and name not in allowed:
            return name
        first = first or name
    return first


def check_destination(destination: str, granted, reserved: ReservedEffects, addresses, *,
                      allow_private_addresses: bool = False) -> str:
    """Answer the one address this plugin may use for ``destination``, or refuse.

    ``granted`` is the plugin's granted ``network_destinations``; ``reserved`` is the
    core's ``ReservedEffects``; ``addresses`` is the list of addresses a lookup of the
    name gave, as data (this function never looks a name up). Raises ``Refused`` with
    ``destination_not_granted``, ``destination_reserved`` or ``address_refused``.
    Arguments of the wrong type raise ``TypeError``; once the destination itself has
    passed (granted, not reserved, not an IP literal refused as one), an empty or
    malformed address list raises ``ValueError`` (a caller's mistake, never an answer).
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
    allowed = _PRIVATE_ALLOWED if allow_private_addresses else frozenset()
    refused = []
    for address in parsed:
        name = _address_class(address, allowed)
        if name is None or name in allowed:
            return str(address)
        if name not in refused:
            refused.append(name)
    raise Refused("address_refused", ",".join(refused))
