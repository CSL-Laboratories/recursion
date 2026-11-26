"""A network address given where the control socket's path belongs is refused in its shown form.

Made-up values only: ``SECRETSTANDIN`` stands where a password would. Nothing is bound, connected or
written: only the address check runs.
"""

from __future__ import annotations

import unittest

from recursion_core import control_transport as ct

S = "SECRETSTANDIN"


class RemoteBindShownTests(unittest.TestCase):
    def refusal(self, address: object) -> ct.RemoteBindRefused:
        with self.assertRaises(ct.RemoteBindRefused) as ctx:
            ct.refuse_remote_bind(address)
        return ctx.exception

    def test_a_sign_in_never_comes_back(self) -> None:
        cases = {
            f"https://user:{S}@example.invalid:443": "<scheme not shown>://example.invalid:443 <sign-in hidden>",
            f"user:{S}@example.invalid:22": "//example.invalid:22 <sign-in hidden>",
            f"unix://user:{S}@example.invalid:22": "//example.invalid:22 <sign-in hidden>",
            f"http://example.invalid:80/?token={S}": "<scheme not shown>://example.invalid:80 <path not shown> <query not shown>",
        }
        for address, shown in cases.items():
            with self.subTest(address=address):
                exc = self.refusal(address)
                self.assertNotIn(S, str(exc))
                self.assertNotIn(S, repr(exc.args))
                self.assertIn(shown, str(exc))
                self.assertIn("control socket", str(exc))

    def test_a_second_line_or_a_path_parameter_never_comes_back(self) -> None:
        odd = "<an address with white space or a character that cannot be printed inside it; the address is not shown>"
        cases = {
            f"https://example.invalid:443/v1\ntoken={S}": odd,
            f"https://example.invalid:443/v1\t{S}": odd,
            f"https://example.invalid:443/v1\r{S}": odd,
            f"https://example.invalid:443/v1;token={S}": "<scheme not shown>://example.invalid:443 <path not shown>",
            f"https://[fe80::1%25{S}]:443": "<scheme not shown>://[fe80::1]:443 <zone id not shown>",
        }
        for address, shown in cases.items():
            with self.subTest(address=address):
                exc = self.refusal(address)
                self.assertNotIn(S, str(exc))
                self.assertIn(f"remote_bind_refused: {shown} is a network address", str(exc))

    def test_a_tuple_or_a_number_is_named_by_its_type(self) -> None:
        for address in ((f"user:{S}@example.invalid", 22), [f"user:{S}@example.invalid", 22], 8080):
            with self.subTest(address=address):
                text = str(self.refusal(address))
                self.assertNotIn(S, text)
                self.assertIn(type(address).__name__, text)

    def test_an_address_with_no_sign_in_still_says_which(self) -> None:
        for address, shown in (("127.0.0.1:8080", "//127.0.0.1:8080"), ("localhost", "//localhost"),
                               ("[::1]:22", "//[::1]:22"), ("tcp://127.0.0.1:9", "<scheme not shown>://127.0.0.1:9")):
            with self.subTest(address=address):
                self.assertIn(shown, str(self.refusal(address)))


if __name__ == "__main__":
    unittest.main()
