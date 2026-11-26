"""No path, and no scheme the caller does not take, is shown in any refusal, repr or socket-path refusal.

The one place that shows a path is the doctor's answer for an address the route takes, and only a plain
path. A user name or a password is shown nowhere. A secret used as a host name is shown wherever the host
is (nothing tells it from a name): such addresses are left out of the search below on purpose. Made-up
values only; nothing is started and nothing is written.
"""

from __future__ import annotations

import unittest

from recursion_core.control_transport.unix import refuse_remote_bind
from recursion_core.shown_address import shown_address
from recursion_core.subscription_routes.own_loop import OwnLoopRouteRefused, check_nous_own_loop, nous_proxy_doctor

S = "SECRETSTANDIN"


def _route(url: str) -> dict:
    return {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy", "base_url": url}


def places(url: str) -> dict[str, str]:
    """Every text that shows ``url``, by the place that shows it."""

    out: dict[str, str] = {}
    try:
        decision = check_nous_own_loop(_route(url), {})
    except OwnLoopRouteRefused as exc:
        out["route refusal"] = " | ".join((str(exc), exc.detail, repr(exc.args), repr(exc)))
    else:
        out["decision repr"] = " | ".join((repr(decision), str(decision), f"{decision}", repr([decision])))
    doctor = nous_proxy_doctor(url)
    out["doctor success" if doctor["ok"] else "doctor refusal"] = repr(doctor)
    try:
        refuse_remote_bind(url)
    except Exception as exc:  # RemoteBindRefused or InsecureSocketPath: both are shown
        out["socket path refusal"] = " | ".join((str(exc), repr(exc.args)))
    out["shown_address"] = " | ".join((shown_address(url), shown_address(url, schemes=("http", "https"))))
    return out


# Where the secret stands -> address templates. {sc} a scheme the route takes, {h} a host.
PLACEMENTS = {
    "scheme": ("{s}://{h}/v1", "{s}+x://{h}", "x-{s}://{h}:22", "{s}://{h}"),
    "sign-in": ("{sc}://{s}@{h}/v1", "{sc}://u:{s}@{h}", "{sc}://u:a{s}b@{h}/v1", "{sc}://{s}:@{h}"),
    "plain path": ("{sc}://{h}/{s}", "{sc}://{h}/v1/{s}", "{sc}://{h}/key/{s}", "{sc}://{h}/v1/sk-{s}",
                   "{sc}://{h}/a/b/c/{s}/d", "{sc}://{h}/./{s}/..", "{sc}://{h}/{s};x=1", "{sc}://{h}/{s}.json"),
    "other path": ("{sc}://{h}/v1;key={s}", "{sc}://{h}/v1/%2F{s}", "{sc}://{h}/~{s}", "{sc}://{h}/v1/{s}%20",
                   "{sc}://{h}/v1/{s}!", "{sc}://{h}/v1/%53ECRETSTANDIN"),
    "query": ("{sc}://{h}/v1?token={s}", "{sc}://{h}?{s}", "{sc}://{h}/v1?a=1&key={s}"),
    "fragment": ("{sc}://{h}/v1#{s}", "{sc}://{h}#{s}"),
}
SCHEMES = ("http", "HTTP", "https")
HOSTS = ("127.0.0.1:8645", "10.0.0.1", "[::1]:8645", "localhost:8645", "example.invalid", "[::ffff:127.0.0.1]")


def addresses():
    for where, templates in PLACEMENTS.items():
        for template in templates:
            for sc in SCHEMES:
                for h in HOSTS:
                    yield where, template.format(s=S, sc=sc, h=h)
    for zone in (f"fe80::1%25{S}", f"::1%25{S}"):
        yield "zone id", f"http://[{zone}]:8645/v1"


class TheCheckExamplesTests(unittest.TestCase):
    def test_a_plain_path_of_a_taken_address_is_shown_only_by_the_doctor(self) -> None:
        for url in (f"http://127.0.0.1:8645/key/{S}", f"http://127.0.0.1:8645/v1/sk-{S}"):
            with self.subTest(url=url):
                got = places(url)
                self.assertIn("decision repr", got)
                self.assertEqual(nous_proxy_doctor(url)["address"], url)  # the one place, a plain path
                for place, text in got.items():
                    if place != "doctor success":
                        self.assertNotIn(S, text, place)
                self.assertIn("http://127.0.0.1:8645 <path not shown>", got["decision repr"])

    def test_a_path_is_not_shown_in_a_refusal(self) -> None:
        url = f"http://10.0.0.1/v1/{S}"
        got = places(url)
        self.assertIn("proxy_not_loopback", got["route refusal"])
        for place, text in got.items():
            self.assertNotIn(S, text, place)
        self.assertEqual(nous_proxy_doctor(url)["address"], "http://10.0.0.1 <path not shown>")

    def test_a_scheme_not_taken_is_shown_nowhere(self) -> None:
        url = f"{S}://127.0.0.1:22"
        got = places(url)
        self.assertEqual(set(got), {"route refusal", "doctor refusal", "socket path refusal", "shown_address"})
        for place, text in got.items():
            self.assertNotIn(S.lower(), text.lower(), place)
        self.assertIn("a scheme that is not http or https", got["route refusal"])
        self.assertIn("<scheme not shown>://127.0.0.1:22", got["socket path refusal"])

    def test_the_route_and_the_socket_name_what_was_wrong(self) -> None:
        self.assertEqual(shown_address("ftp://h/x", schemes=("http", "https")),
                         "<an address with a scheme that is not http or https; the address is not shown>")
        self.assertEqual(shown_address("https://example.invalid:443/a/b"),
                         "<scheme not shown>://example.invalid:443 <path not shown>")
        self.assertEqual(shown_address("https://example.invalid/a/b", schemes=("https",)),
                         "https://example.invalid <path not shown>")
        self.assertEqual(shown_address("https://example.invalid/", schemes=("https",), plain_path=True),
                         "https://example.invalid/")
        self.assertEqual(shown_address("https://example.invalid", schemes=("https",)), "https://example.invalid")


class SecretSearchTests(unittest.TestCase):
    def test_no_place_shows_a_secret_but_the_named_ones(self) -> None:
        seen = 0
        for where, url in addresses():
            for place, text in places(url).items():
                seen += 1
                allowed = where == "plain path" and place == "doctor success"
                with self.subTest(where=where, url=url, place=place):
                    if allowed:
                        continue
                    self.assertNotIn(S.lower(), text.lower())
                    self.assertNotIn("SECRETSTAND", text.upper())
        self.assertGreater(seen, 1000)

    def test_the_doctor_shows_a_plain_path_only_on_success(self) -> None:
        shown = refused = 0
        for where, url in addresses():
            doctor = nous_proxy_doctor(url)
            if S not in doctor["address"]:
                continue
            with self.subTest(url=url):
                self.assertTrue(doctor["ok"])
                self.assertEqual(where, "plain path")
            shown += 1
        for where, url in addresses():
            if not nous_proxy_doctor(url)["ok"]:
                refused += 1
        self.assertGreater(shown, 0)
        self.assertGreater(refused, 0)

    def test_no_shown_form_tells_the_length_of_a_path(self) -> None:
        for h in HOSTS:
            short = places(f"http://{h}/{S}?q=1")
            long = places(f"http://{h}/{S * 3}/x/y?q=1")
            for place in short:
                if place == "doctor success":
                    continue
                with self.subTest(h=h, place=place):
                    self.assertEqual(short[place], long[place])


if __name__ == "__main__":
    unittest.main()
