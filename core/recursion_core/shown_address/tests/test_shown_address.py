"""The one shown form of an address, and the structure test that holds every place of the product to it.

Made-up values only: ``SECRETSTANDIN`` stands where a password, a token or a user name would. No file is
written; the structure test only reads the product's own source files.
"""

from __future__ import annotations

import ast
import itertools
import re
import unittest
from pathlib import Path

from recursion_core.shown_address import HIDDEN_SIGN_IN, holds_sign_in, shown_address

S = "SECRETSTANDIN"
HTTP = ("http", "https")
NOT_SHOWN = "; the address is not shown>"
AT_AFTER_HOST = "<an address with an at sign after its host, which may end a sign-in" + NOT_SHOWN
NFKC = ("<an address with a character before the path that NFKC normalization turns into a slash, a question "
        "mark, a number sign, an at sign or a colon" + NOT_SHOWN)
NO_SCHEME = "<an address with no scheme such as http:// before its host" + NOT_SHOWN
NO_HOST = "<an address with no host" + NOT_SHOWN
BAD_PORT = "<an address with a port that is not a number from 0 to 65535" + NOT_SHOWN
ODD_INSIDE = "<an address with white space or a character that cannot be printed inside it" + NOT_SHOWN
ODD_HOST = ("<an address with a host that is neither a name of letters, digits, dots, dashes and underscores "
            "nor an IPv6 address in brackets" + NOT_SHOWN)
# The fixed words that may follow a shown address, one for each part that was there and is not shown.
SCHEME_WORDS = "<scheme not shown>"
WORDS = ("<sign-in hidden>", "<zone id not shown>", "<path parameters not shown>", "<path not shown>",
         "<query not shown>", "<fragment not shown>")
# What a shown address is made of: a scheme, a host that is a plain name or an IPv6 address in brackets, a
# port. No path, and no scheme but http or https (the schemes the tests take); the words stand for any other
# scheme (written <s> here). Nothing else of the given text.
BUILT = re.compile(r"(?:<s>:|[Hh][Tt][Tt][Pp][Ss]?:)?//(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?")
DESCRIBED = re.compile(r"<(?:an address with [^<>@]+; the address is not shown|an empty address"
                       r"|an address that could not be read; the address is not shown"
                       r"|a value of type [A-Za-z0-9_]+, not an address text)>")
# Characters the URL library deletes (tab, CR, LF) or strips from the front and the scheme (C0 controls and
# space), and others that cannot be printed or are white space by Python's own rule.
ODD = tuple(chr(i) for i in range(0x21)) + ("\x7f", "\x85", "\xa0", "\u2028", "\u2029", "\u3000", "\u200b",
                                            "\ufeff")

# (value, schemes, the shown form). Each row also passes the checks of ``assertSafe``.
TABLE = (
    (f"http://user:{S}@127.0.0.1/v1", None, "<scheme not shown>://127.0.0.1 <sign-in hidden> <path not shown>"),
    (f"  http://user:{S}@127.0.0.1/v1\n", None, "<scheme not shown>://127.0.0.1 <sign-in hidden> <path not shown>"),
    (f"https://user:{S}@[::1]:8645/v1", None, "<scheme not shown>://[::1]:8645 <sign-in hidden> <path not shown>"),
    (f"http://{S}@127.0.0.1:8645", None, "<scheme not shown>://127.0.0.1:8645 <sign-in hidden>"),
    (f"http://:{S}@127.0.0.1", None, "<scheme not shown>://127.0.0.1 <sign-in hidden>"),
    ("http://user:@127.0.0.1", None, "<scheme not shown>://127.0.0.1 <sign-in hidden>"),
    ("http://@127.0.0.1", None, "<scheme not shown>://127.0.0.1 <sign-in hidden>"),
    (f"HTTPS://user:{S}@Example.Invalid:443/A", None, "<scheme not shown>://Example.Invalid:443 <sign-in hidden> <path not shown>"),
    (f"http://127.0.0.1:8645/v1?token={S}", None, "<scheme not shown>://127.0.0.1:8645 <path not shown> <query not shown>"),
    ("http://127.0.0.1:8645/v1?", None, "<scheme not shown>://127.0.0.1:8645 <path not shown> <query not shown>"),
    (f"http://127.0.0.1/v1#{S}", None, "<scheme not shown>://127.0.0.1 <path not shown> <fragment not shown>"),
    (f"http://u:{S}@127.0.0.1/v1?t={S}#{S}", None,
     "<scheme not shown>://127.0.0.1 <sign-in hidden> <path not shown> <query not shown> <fragment not shown>"),
    (f"http://u:{S}@h@127.0.0.1/", None, "<scheme not shown>://127.0.0.1 <sign-in hidden> <path not shown>"),
    (f"http://us%40er:{S}%3A@127.0.0.1", None, "<scheme not shown>://127.0.0.1 <sign-in hidden>"),
    (f"http://127.0.0.1/v1/{S}%40x", None, "<scheme not shown>://127.0.0.1 <path not shown>"),
    (f"http://127.0.0.1/a@{S}", None, AT_AFTER_HOST),
    (f"http://admin:12?{S}@127.0.0.1", None, AT_AFTER_HOST),
    (f"http://admin:12/{S}@127.0.0.1", None, AT_AFTER_HOST),
    (f"http://admin:pa#{S}@127.0.0.1", None, AT_AFTER_HOST),
    ("127.0.0.1:8645", None, NO_SCHEME),
    (f"user:{S}@127.0.0.1:22", None, NO_SCHEME),
    (f"http:{S}@127.0.0.1", None, NO_SCHEME),
    (f"//user:{S}@example.invalid:22", None, "//example.invalid:22 <sign-in hidden>"),
    ("//[::1]:22", None, "//[::1]:22"),
    (f"ftp://user:{S}@127.0.0.1/v1", HTTP,
     "<an address with a scheme that is not http or https" + NOT_SHOWN),
    (f"ftp://user:{S}@127.0.0.1/v1", None, "<scheme not shown>://127.0.0.1 <sign-in hidden> <path not shown>"),
    (f"//user:{S}@127.0.0.1/v1", HTTP, "<an address with no scheme, which must be http or https" + NOT_SHOWN),
    (f"http://user:{S}@/v1", None, NO_HOST),
    (f"http:///{S}", None, NO_HOST),
    (f"http://127.0.0.1:{S}/v1", None, BAD_PORT),
    ("http://127.0.0.1:99999", None, BAD_PORT),
    (f"http://user:pa\uff03{S}@127.0.0.1/v1", None, NFKC),
    (f"http://127.0.0.1\uff03{S}/v1", None, NFKC),
    (f"http://[::1/{S}", None, "<an address with a [ or ] that does not frame an IPv6 host" + NOT_SHOWN),
    (f"http://user:{S}@exa mple/v1", None, ODD_INSIDE),
    ("http://h\x07ost/v1", None, ODD_INSIDE),
    ("http://[fe80::1%25eth0]:8645/v1", None, "<scheme not shown>://[fe80::1]:8645 <zone id not shown> <path not shown>"),
    # built up from parts known to be harmless
    (f"http://127.0.0.1:8645/v1\n{S}", None, ODD_INSIDE),
    (f"http://127.0.0.1:8645/v1\ttoken={S}", None, ODD_INSIDE),
    (f"http://127.0.0.1:8645/v1\r{S}", None, ODD_INSIDE),
    (f"http://127.0.0.1:8645/v1\x00{S}", None, ODD_INSIDE),
    (f"\x01http://user:{S}@127.0.0.1/", None, ODD_INSIDE),
    (f"http://127.0.0.1:8645/v1;token={S}", None, "<scheme not shown>://127.0.0.1:8645 <path not shown>"),
    (f"http://127.0.0.1:8645/v1;k={S}/x?q={S}", None,
     "<scheme not shown>://127.0.0.1:8645 <path not shown> <query not shown>"),
    (f"http://127.0.0.1:8645/v1%3Ftoken={S}", None, "<scheme not shown>://127.0.0.1:8645 <path not shown>"),
    (f"http://127.0.0.1:8645/~{S}", None, "<scheme not shown>://127.0.0.1:8645 <path not shown>"),
    (f"http://127.0.0.1:8645/v1+{S}", None, "<scheme not shown>://127.0.0.1:8645 <path not shown>"),
    (f"http://[::1%25{S}]:8645/v1", None, "<scheme not shown>://[::1]:8645 <zone id not shown> <path not shown>"),
    (f"http://u:{S}@[fe80::1%25{S}]/v1?t={S}", None,
     "<scheme not shown>://[fe80::1] <sign-in hidden> <zone id not shown> <path not shown> <query not shown>"),
    (f"http://exa%41mple{S}/v1", None, ODD_HOST),
    (f"http://b\u00fccher{S}.example/v1", None, ODD_HOST),
    (f"http://[v1.{S}]/v1", None, ODD_HOST),
    ("http://127.0.0.1:08645/v1/a-b_c.d/", None, "<scheme not shown>://127.0.0.1:8645 <path not shown>"),
    ("http://127.0.0.1:8645/v1", HTTP, "http://127.0.0.1:8645 <path not shown>"),
    ("http://10.1.2.3:8645", None, "<scheme not shown>://10.1.2.3:8645"),
    ("", None, "<an empty address>"),
    ("   ", None, "<an empty address>"),
    (8645, None, "<a value of type int, not an address text>"),
    (None, None, "<a value of type NoneType, not an address text>"),
    (f"http://user:{S}@127.0.0.1".encode(), None, "<a value of type bytes, not an address text>"),
    (("127.0.0.1", 22), None, "<a value of type tuple, not an address text>"),
)

# The sign-in rule for free text of core/recursion_core/settings.py at 6a8d72d1 (CSL-Laboratories/recursion,
# draft #637), copied here word for word so that a shown form can be checked against it: a shown form must
# pass it unchanged, so a text that holds a shown address reads the same through either rule.
_HIDDEN_SIGN_IN_637 = "<sign-in hidden>"
_SPACE_637 = re.compile(r"(\s+)")
_SCHEME_AT_START_637 = re.compile(r"[A-Za-z0-9+.-]+://")


def _without_sign_in_637(text: str) -> str:
    if "@" not in text:
        return text
    return "".join(_run_without_sign_in_637(run) for run in _SPACE_637.split(text))


def _run_without_sign_in_637(run: str) -> str:
    last = run.rfind("@")
    if last < 0:
        return run
    scheme = _SCHEME_AT_START_637.match(run)
    begin = scheme.end() if scheme else 0
    if begin == last:
        return run
    return run[:begin] + _HIDDEN_SIGN_IN_637 + run[last:]


def _generated() -> list[str]:
    """Addresses put together from parts, every one with the made-up secret in a part that is never shown."""

    schemes = ("http://", "HTTPS://", "socks5h://", "//")
    sign_ins = ("", f"u:{S}@", f"{S}@", f":{S}@", f"u:pa/{S}@", f"u:pa?{S}@", f"u:pa#{S}@", f"u:{S}@x@",
                f"u%40x:{S}@", f"u: {S}@")
    hosts = ("127.0.0.1", "[::1]", "[::ffff:10.0.0.1]", "example.invalid", "")
    ports = ("", ":8645", ":0", f":{S}", ":65536")
    tails = ("", "/", "/v1", f"/v1?token={S}", f"/v1#{S}", f"?{S}", f"#{S}", f"/a@{S}", f"/{S}%40", f"/v1\n{S}",
             f"/v1\t?token={S}", f"/v1\r{S}", f"/v1;token={S}", f"/v1%3F{S}")
    return [a + b + c + d + e for a, b, c, d, e in itertools.product(schemes, sign_ins, hosts, ports, tails)
            if S in b + d + e]


class ShownAddressTests(unittest.TestCase):
    def assertSafe(self, value: object, shown: str) -> None:
        self.assertIsInstance(shown, str)
        self.assertNotIn(S, shown)
        self.assertNotIn(S.lower(), shown.lower())
        self.assertNotIn("@", shown)  # no @ is left before which a sign-in could stand
        self.assertEqual(_without_sign_in_637(shown), shown)

    def test_the_table(self) -> None:
        self.assertGreaterEqual(len(TABLE), 20)
        for value, schemes, want in TABLE:
            with self.subTest(value=value, schemes=schemes):
                got = shown_address(value, schemes=schemes)
                self.assertEqual(got, want)
                self.assertSafe(value, got)

    def test_the_host_is_kept_where_the_address_parses(self) -> None:
        for value, schemes, want in TABLE:
            if isinstance(value, str) and (not want.startswith("<") or want.startswith(SCHEME_WORDS)):
                host = want.replace(SCHEME_WORDS, "s").split(" ", 1)[0].split("//", 1)[1].split("/", 1)[0]
                given = re.sub(r"%25[^]]*]", "]", value).replace(":08645", ":8645")
                with self.subTest(value=value):
                    self.assertIn(host, given)
                    self.assertTrue(host)

    def test_addresses_put_together_from_parts_never_show_the_secret(self) -> None:
        values = _generated()
        self.assertGreater(len(values), 1000)
        for value in values:
            for schemes in (None, HTTP):
                got = shown_address(value, schemes=schemes)
                self.assertSafe(value, got)

    def test_a_sign_in_is_named_by_the_shared_words(self) -> None:
        self.assertEqual(HIDDEN_SIGN_IN, "<sign-in hidden>")
        self.assertEqual(HIDDEN_SIGN_IN, _HIDDEN_SIGN_IN_637)

    def test_it_never_raises(self) -> None:
        class Odd(str):
            def strip(self, *args: object) -> str:
                raise RuntimeError(S)

        class Loud:
            def __repr__(self) -> str:
                raise RuntimeError(S)

        self.assertEqual(shown_address(Odd(f"http://u:{S}@h")),
                         "<an address that could not be read; the address is not shown>")
        self.assertEqual(shown_address(Loud()), "<a value of type Loud, not an address text>")
        self.assertEqual(shown_address("http://h", schemes=None), "<scheme not shown>://h")
        self.assertEqual(shown_address("http://h", schemes=()),
                         "<an address with a scheme that is not a scheme taken here" + NOT_SHOWN)

    def test_holds_sign_in(self) -> None:
        for value in (f"http://u:{S}@127.0.0.1", f"http://u:pa\uff03{S}@127.0.0.1/v1", f"//u:{S}@h:22",
                      f"ftp://{S}@h", f"http://:{S}@h", "http://u:@h", f"http://u:{S}\n@h", f"http://u:{S}@/v1"):
            with self.subTest(value=value):
                self.assertTrue(holds_sign_in(value))
        for value in ("http://127.0.0.1:8645/v1", "", None, 22, b"u:p@h", "http://h/a%40b", f"http://127.0.0.1/a@{S}",
                      f"http://h/v1?x=a@{S}", f"http://h/v1#a@{S}", "http://@h", "http://:@h", "http://\t@h",
                      f"user:{S}@h:22", "@", f"http://user:pa?{S}@h/v1"):
            with self.subTest(value=value):
                self.assertFalse(holds_sign_in(value))

    def test_white_space_and_unprintable_characters_are_read_before_the_library(self) -> None:
        for ch in ODD:
            for value in (f"http://127.0.0.1:8645/v1{ch}token={S}", f"http://us{ch}er:{S}@127.0.0.1/v1",
                          f"http://127.0.0.1{ch}{S}:8645/v1", f"ht{ch}tp://127.0.0.1:8645/{S}",
                          f"{ch}http://127.0.0.1:8645/v1{ch}{S}", f"http://127.0.0.1:8645/{S}{ch}x"):
                with self.subTest(ch=ch, value=value):
                    got = shown_address(value)
                    self.assertEqual(got, ODD_INSIDE)
                    self.assertSafe(value, got)

    def test_every_shown_form_is_built_from_harmless_parts(self) -> None:
        values = [value for value, _, _ in TABLE] + _generated()
        for ch in ODD:
            values.append(f"http://127.0.0.1:8645/v1{ch}{S}")
        for value in values:
            for schemes in (None, HTTP):
                got = shown_address(value, schemes=schemes)
                with self.subTest(value=value, schemes=schemes):
                    if got.startswith("<") and not got.startswith(SCHEME_WORDS):
                        self.assertRegex(got, "^" + DESCRIBED.pattern + "$")
                        continue
                    address, _, rest = got.replace(SCHEME_WORDS, "<s>", 1).partition(" ")
                    self.assertRegex(address, "^" + BUILT.pattern + "$")
                    notes = rest.split(" <")
                    notes = [notes[0]] + ["<" + note for note in notes[1:]] if rest else []
                    self.assertEqual([note for note in notes if note not in WORDS], [])
                    self.assertEqual(sorted(notes, key=WORDS.index), notes)  # each part's words once, in order
                    self.assertEqual(len(set(notes)), len(notes))


# ---------------------------------------------------------------------------------------------------------
# The structure test. An ast scan of the product tree finds every place that puts a value into words a
# person or a log can see (a raise and its cause, a call that makes an exception, a log call, print, warn,
# an f-string, a dict value, the repr of a dataclass) and fails when such a place takes an address that has
# not passed through ``shown_address``.
#
# A value is taken for an address when it is a name or an attribute whose name holds one of ADDRESS_WORDS
# (split at _; a name that ends in _name or _names holds the name of a variable, and a name in capitals is a
# constant of the code, not an address a person gave), a value read from a mapping or the environment by a
# key that holds one of those words, a value parsed by urlsplit or urlparse, the exception caught around
# such a parse, or a name assigned from any of these in the same function. A value carries an address on
# through text made of it: an f-string, a + or %, str, repr, format or join, and a method of the value
# itself (``url.strip()``). The result of any other call is a new value (a cause, a count, an object) and
# is not taken for the address. The words are this test's own list; no person chooses them. "registry" is not one of them: in
# this product a registry is the daemon's run registry, an object, never an address.
#
# What it does not see, known: an address under a name with none of the words that is not assigned in the
# same function (a parameter called ``value``); a value carried into a nested function; a helper that
# returns its argument as it was; an address shown through a call that is not one of the kinds above (a
# write to a file, a return value a caller prints).

ADDRESS_WORDS = frozenset({
    "url", "urls", "uri", "uris", "address", "addresses", "proxy", "proxies", "endpoint", "endpoints",
    "origin", "origins", "remote", "remotes", "netloc", "hostname", "hostnames",
})
_NAME_ENDINGS = frozenset({"name", "names"})
_SHOWN = "shown_address"
# Calls whose result is text made of their arguments.
_TEXT_OF = frozenset({"str", "repr", "ascii", "format"})
_TEXT_METHODS = frozenset({"join", "format", "format_map"})
_PARSERS = frozenset({"urlsplit", "urlparse"})
_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})
_EXCEPTION_ENDINGS = ("Error", "Exception", "Refused", "Refusal", "Warning")
_KEY_READERS = frozenset({"get", "pop", "getenv", "setdefault"})
SCANNED = ("core", "eval", "conformance")
ROOT = Path(__file__).resolve().parents[4]

# Places allowed to show such a value, each with why it shows no address. An entry is one function or
# method, never a whole file, and an entry that no longer finds anything is taken off.
ALLOWED = {
    ("core/recursion_core/child_env/policy.py", "ChildRoute.__repr__"):
        "url and trusted there are the words set and none and a count; the address itself is never read.",
    ("core/recursion_core/child_env/policy.py", "_ip_forms"):
        "the f-string makes the IPv6 form of an IPv4 address object for a comparison and is never shown.",
}
# The function itself puts the parts of an address together; it is the one place that may.
NOT_SCANNED = frozenset({"core/recursion_core/shown_address/__init__.py"})


def address_word(identifier: str, *, constant_too: bool = False) -> bool:
    if identifier.isupper() and not constant_too:
        return False
    tokens = [token for token in identifier.lower().split("_") if token]
    if not tokens or tokens[-1] in _NAME_ENDINGS:
        return False
    return any(token in ADDRESS_WORDS for token in tokens)


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _word_key(node: ast.expr) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and address_word(node.value, constant_too=True))


def raw_parts(node: ast.AST | None, tainted: frozenset[str] | set[str]) -> list[ast.AST]:
    """The parts of ``node`` that are addresses not passed through ``shown_address``."""

    if node is None:
        return []
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name == _SHOWN:
            return []
        if name in _PARSERS:
            return [node]
        if name in _KEY_READERS and node.args and _word_key(node.args[0]):
            return [node]
        args = [*node.args, *(keyword.value for keyword in node.keywords)]
        found: list[ast.AST] = []
        if isinstance(node.func, ast.Name) and name in _TEXT_OF:
            found = [part for arg in args for part in raw_parts(arg, tainted)]
        elif isinstance(node.func, ast.Attribute):
            found = raw_parts(node.func.value, tainted)  # a method of the value itself
            if name in _TEXT_METHODS:
                found += [part for arg in args for part in raw_parts(arg, tainted)]
        return found
    if isinstance(node, ast.Compare) or (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)):
        return []  # a truth value shows nothing of the text
    if isinstance(node, ast.IfExp):
        return raw_parts(node.body, tainted) + raw_parts(node.orelse, tainted)
    if isinstance(node, ast.Name):
        return [node] if node.id in tainted or address_word(node.id) else []
    if isinstance(node, ast.Attribute):
        return [node] if address_word(node.attr) else raw_parts(node.value, tainted)
    if isinstance(node, ast.Subscript) and _word_key(node.slice):
        return [node]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return []
    found = []
    for child in ast.iter_child_nodes(node):
        found += raw_parts(child, tainted)
    return found


def _scope_nodes(body: list[ast.stmt]) -> tuple[list[ast.AST], list[ast.AST]]:
    """Every node of one scope, and the functions and classes nested in it (scanned as scopes of their own)."""

    nodes: list[ast.AST] = []
    nested: list[ast.AST] = []
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nested.append(node)
            nodes.extend(node.decorator_list)
            if not isinstance(node, ast.ClassDef):
                nodes.extend(d for d in node.args.defaults + node.args.kw_defaults if d is not None)
            continue
        nodes.append(node)
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return nodes, nested


def _target_names(target: ast.AST) -> list[str]:
    """The names a target binds: a name, or the names of a tuple, list or starred target. A subscript or an
    attribute target binds no name of this scope."""

    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    return []


def _tainted(nodes: list[ast.AST]) -> set[str]:
    pairs: list[tuple[list[str], ast.AST | None]] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            pairs.append(([n for t in node.targets for n in _target_names(t)], node.value))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            pairs.append((_target_names(node.target), node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            pairs.append((_target_names(node.target), node.iter))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            pairs.append((_target_names(node.optional_vars), node.context_expr))
        elif isinstance(node, ast.Try):
            parsed = any(isinstance(n, ast.Call) and _call_name(n.func) in _PARSERS
                         for stmt in node.body for n in ast.walk(stmt))
            for handler in node.handlers:
                if parsed and handler.name:
                    pairs.append(([handler.name], ast.Name(id="url")))
    tainted: set[str] = set()
    for _ in range(len(pairs) + 1):
        grown = {name for names, value in pairs if raw_parts(value, tainted) for name in names}
        if grown <= tainted:
            break
        tainted |= grown
    return tainted


def _dataclass(node: ast.ClassDef) -> bool:
    for deco in node.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if _call_name(target) == "dataclass":
            if isinstance(deco, ast.Call) and any(k.arg == "repr" and isinstance(k.value, ast.Constant)
                                                  and k.value.value is False for k in deco.keywords):
                return False
            return True
    return False


def _repr_false(value: ast.AST | None) -> bool:
    return (isinstance(value, ast.Call) and _call_name(value.func) == "field"
            and any(k.arg == "repr" and isinstance(k.value, ast.Constant) and k.value.value is False
                    for k in value.keywords))


def _sinks(nodes: list[ast.AST]) -> list[tuple[ast.AST, str]]:
    sinks: list[tuple[ast.AST, str]] = []
    for node in nodes:
        if isinstance(node, ast.Raise):
            sinks.append((node, "raise"))
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in ("print", "warn") or (isinstance(node.func, ast.Attribute) and name in _LOG_METHODS) \
                    or (name or "").endswith(_EXCEPTION_ENDINGS):
                sinks.append((node, f"call {name}"))
        elif isinstance(node, ast.JoinedStr):
            sinks.append((node, "f-string"))
        elif isinstance(node, ast.Dict):
            sinks.append((node, "dict value"))
    return sinks


def _sink_raw(node: ast.AST, tainted: set[str]) -> list[ast.AST]:
    if isinstance(node, ast.Raise):
        exc = node.exc
        if isinstance(exc, ast.Call):  # the words given to the exception made here
            parts = [p for a in [*exc.args, *(k.value for k in exc.keywords)] for p in raw_parts(a, tainted)]
        else:
            parts = raw_parts(exc, tainted)
        return parts + raw_parts(node.cause, tainted)
    if isinstance(node, ast.Call):
        return [p for a in [*node.args, *(k.value for k in node.keywords)] for p in raw_parts(a, tainted)]
    if isinstance(node, ast.JoinedStr):
        return [p for v in node.values if isinstance(v, ast.FormattedValue) for p in raw_parts(v.value, tainted)]
    if isinstance(node, ast.Dict):
        return [p for v in node.values for p in raw_parts(v, tainted)]
    return []


def scan_source(source: str, path: str) -> list[tuple[str, str, int, str]]:
    """(path, qualified name of the function, line, what) for every place that shows a raw address."""

    findings: set[tuple[str, str, int, str]] = set()
    tree = ast.parse(source, filename=path)

    def scope(body: list[ast.stmt], qual: str, klass: ast.ClassDef | None) -> None:
        nodes, nested = _scope_nodes(body)
        tainted = _tainted(nodes)
        for node, kind in _sinks(nodes):
            for part in _sink_raw(node, tainted):
                findings.add((path, qual, getattr(part, "lineno", node.lineno), f"{kind}: {ast.unparse(part)}"))
        if klass is not None and _dataclass(klass):
            has_repr = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__repr__"
                           for n in klass.body)
            for stmt in klass.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) \
                        and address_word(stmt.target.id) and not has_repr and not _repr_false(stmt.value):
                    findings.add((path, qual, stmt.lineno, f"dataclass repr: {stmt.target.id}"))
        for node in nested:
            name = f"{qual}.{node.name}" if qual else node.name
            scope(node.body, name, node if isinstance(node, ast.ClassDef) else None)

    scope(tree.body, "", None)
    return sorted(findings)


def product_files() -> list[Path]:
    files = []
    for top in SCANNED:
        base = ROOT / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(ROOT)
            if "tests" in rel.parts or path.name.startswith("test_") or "__pycache__" in rel.parts:
                continue
            files.append(path)
    return files


def scan_product() -> list[tuple[str, str, int, str]]:
    findings = []
    for path in product_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in NOT_SCANNED:
            continue
        findings += scan_source(path.read_text(encoding="utf-8"), rel)
    return findings


class StructureTests(unittest.TestCase):
    def test_no_place_of_the_product_shows_a_raw_address(self) -> None:
        self.assertTrue((ROOT / "core" / "recursion_core").is_dir(), ROOT)
        self.assertGreater(len(product_files()), 50)
        findings = scan_product()
        shown = [f for f in findings if (f[0], f[1]) not in ALLOWED]
        self.assertEqual(shown, [], "an address is shown without shown_address (recursion_core.shown_address): "
                                    "pass it through that function, or name the place in ALLOWED with why")
        used = {(f[0], f[1]) for f in findings}
        self.assertEqual(sorted(set(ALLOWED) - used), [], "an allowed place no longer shows such a value")

    def test_the_allowed_places_are_single_functions_with_a_reason(self) -> None:
        for (path, qual), why in ALLOWED.items():
            with self.subTest(path=path, qual=qual):
                self.assertTrue((ROOT / path).is_file())
                self.assertTrue(qual and "*" not in qual and "*" not in path)
                self.assertTrue(why.endswith(".") and len(why.split()) >= 5)
        for path in NOT_SCANNED:
            self.assertTrue((ROOT / path).is_file())

    def test_the_scan_sees_each_kind_of_place(self) -> None:
        cases = {
            "raise": "def f(base_url):\n    raise ValueError(base_url)\n",
            "raise from": ("def f(v):\n    try:\n        urlsplit(v)\n    except ValueError as exc:\n"
                           "        raise KeyError('bad') from exc\n"),
            "except text": ("def f(v):\n    try:\n        p = urlsplit(v)\n    except ValueError as exc:\n"
                            "        raise Refused('nous', str(exc))\n"),
            "parsed": "def f(v):\n    p = urlsplit(v)\n    raise E(p.path)\n",
            "assigned": "def f(proxy):\n    t = proxy.strip()\n    x = t\n    return OwnRefused(x)\n",
            "f-string": "def f(self):\n    return f'{self.endpoint}'\n",
            "log": "def f(remote):\n    log.warning('x %s', remote)\n",
            "print": "def f(urls):\n    print(*urls)\n",
            "dict": "def f(address):\n    return {'address': address}\n",
            "key": "def f(route):\n    raise E(route.get('base_url'))\n",
            "environ": "import os\ndef f():\n    print(os.environ['HTTPS_PROXY'])\n",
            "dataclass": "@dataclass(frozen=True)\nclass D:\n    base_url: str = ''\n",
            "method": "class C:\n    def m(self, origin):\n        raise E(f'{origin!r}')\n",
            "plus": "def f(url):\n    raise E('at ' + url)\n",
            "percent": "def f(url):\n    raise E('at %s' % url)\n",
            "format": "def f(url):\n    raise E('at {}'.format(url))\n",
            "join": "def f(urls):\n    raise E(', '.join(urls))\n",
            "str": "def f(remote):\n    log.error(str(remote))\n",
        }
        for kind, source in cases.items():
            with self.subTest(kind=kind):
                self.assertNotEqual(scan_source(source, "x.py"), [], source)
        quiet = {
            "shown": "def f(base_url):\n    raise ValueError(shown_address(base_url))\n",
            "type": "def f(address):\n    raise ValueError(f'a {type(address).__name__}')\n",
            "count": "def f(urls):\n    print(len(urls), bool(urls))\n",
            "a constant": "def f():\n    raise ValueError(DEFAULT_URL)\n",
            "a new value": "def f(url):\n    cause = check(url)\n    raise ValueError(cause)\n",
            "a name": "def f(base_url_name):\n    raise ValueError(base_url_name)\n",
            "a test": "def f(url):\n    raise ValueError('x' if url else 'y', url == 'z')\n",
            "shown first": "def f(url):\n    s = shown_address(url) if url else ''\n    raise E(f'{s}')\n",
            "dataclass repr": "@dataclass\nclass D:\n    url: str = field(default='', repr=False)\n",
            "own repr": "@dataclass\nclass D:\n    url: str = ''\n    def __repr__(self):\n        return 'D'\n",
        }
        for kind, source in quiet.items():
            with self.subTest(kind=kind):
                self.assertEqual(scan_source(source, "x.py"), [], source)


if __name__ == "__main__":
    unittest.main()
