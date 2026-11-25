"""Static proof that the product reaches no network address a person did not give it.

Usage: python3 tools/network_reach.py            check the shipped package against the list
       python3 tools/network_reach.py --findings  print every finding, listed or not

The scan reads every shipped Python file of ``core/recursion_core`` (not the tests) by its
syntax tree and reports each import and each reference that can open a connection, look up
a name, start a program, or reach code by a name that cannot be decided from the source.
Every finding must be covered by a row of ``tools/network_reach.toml``; a finding with no
row, a row that covers nothing, a row whose address comes from the code itself, and a scan
that finds nothing at all are each a failure. What the scan cannot decide is refused, not
passed: an import from outside the standard library, a name looked up at run time on a
module, an ``eval`` or ``exec``, a source file in another language.
"""
from __future__ import annotations

import ast
import importlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python before 3.11 is not supported
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path("core/recursion_core")
LIST_FILE = Path("tools/network_reach.toml")

# Who may supply an address. 'code' is never one of them: an address the code itself
# chooses is exactly what this proof forbids.
WHO = {
    "settings": "an address in the person's own settings or engine configuration",
    "command": "an address the person gives in a command or a request",
    "plugin-grant": "an address a plugin uses only under a grant the person gave",
    "local": "a door on the person's own machine only (a Unix socket in the person's folders, or the loopback address)",
    "none": "no address at all: the program or call is handed none and reaches no network",
}

# The families of the standard library that can reach a network or start a program.
FAMILY_PREFIXES = (
    ("socket", ("socket", "ssl", "socketserver", "selectors.DefaultSelector")),
    ("http", ("http.client", "http.server", "http.cookiejar", "urllib.request", "urllib.robotparser",
              "xmlrpc", "ftplib", "smtplib", "poplib", "imaplib", "nntplib", "telnetlib",
              "webbrowser", "wsgiref.simple_server")),
    ("asyncio", ("asyncio.open_connection", "asyncio.start_server", "asyncio.open_unix_connection",
                 "asyncio.start_unix_server", "asyncio.create_subprocess_exec",
                 "asyncio.create_subprocess_shell", "asyncio.streams", "asyncio.subprocess",
                 "asyncio.base_events", "asyncio.unix_events", "asyncio.selector_events")),
    ("subprocess", ("subprocess", "os.system", "os.popen", "os.exec", "os.spawn", "os.posix_spawn",
                    "os.fork", "pty.spawn", "pty.fork", "multiprocessing",
                    "concurrent.futures.ProcessPoolExecutor", "concurrent.futures.process")),
    ("dynamic", ("importlib", "builtins.__import__", "builtins.eval", "builtins.exec",
                 "builtins.compile", "ctypes", "runpy", "code.interact", "pkgutil.resolve_name")),
)
# Builtins that reach code by a name the source does not name.
DYNAMIC_BUILTINS = {"__import__": "builtins.__import__", "eval": "builtins.eval",
                    "exec": "builtins.exec", "compile": "builtins.compile"}
# A method called on an object the scan cannot type: these names open or name connections.
NETWORK_METHODS = {
    "connect", "connect_ex", "bind", "sendto", "sendmsg", "create_connection", "create_server",
    "create_unix_connection", "create_unix_server", "create_datagram_endpoint", "getaddrinfo",
    "getnameinfo", "sock_connect", "subprocess_exec", "subprocess_shell", "urlopen",
    "open_connection", "start_server", "start_tls", "wrap_socket",
}
# Modules whose references the scan follows but that reach no network.
SAFE_MODULES = {"sqlite3"}
# Words that name a person or a machine in a way meant to be kept or sent (point 5).
IDENTITY_NAMES = ("uuid.getnode", "uuid.uuid1", "socket.gethostname", "socket.getfqdn",
                  "platform.node", "platform.uname", "os.uname")
IDENTITY_WORDS = re.compile(r"machine[-_ ]?id|install(ation)?[-_ ]?id|telemetry|crash[-_ ]?report|"
                            r"usage[-_ ]?(ping|report|stat)|analytics|phone[-_ ]?home|beacon",
                            re.IGNORECASE)
# Literal addresses in string constants (point 4).
LITERAL = re.compile(
    r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+"                       # a web or other address
    r"|\b(?:\d{1,3}\.){3}\d{1,3}\b"                          # a dotted IPv4 address
    r"|\blocalhost\b"
    r"|\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|ai|app|cloud|co|sh|me|so|xyz|info|gg)\b",
    re.IGNORECASE)


@dataclass(frozen=True, order=True)
class Finding:
    kind: str       # 'reach', 'literal' or 'identity'
    file: str
    function: str
    name: str
    family: str
    line: int

    def key(self):
        return (self.kind, self.file, self.function, self.name)


class _Resolver:
    def __init__(self):
        self._cache = {}

    def inert(self, dotted: str) -> bool:
        """True for a constant or an exception class of a standard module (it reaches nothing)."""
        if dotted in self._cache:
            return self._cache[dotted]
        parts = dotted.split(".")
        obj, result = None, False
        for cut in range(len(parts) - 1, 0, -1):
            try:
                obj = importlib.import_module(".".join(parts[:cut]))
            except Exception:
                continue
            try:
                for attr in parts[cut:]:
                    obj = getattr(obj, attr)
            except AttributeError:
                # Not on this platform (socket.AF_UNIX on Windows): a name in capitals is a
                # constant by the standard library's own naming; any other name stays a finding.
                result = parts[-1].isupper()
                break
            result = (not callable(obj)) or (isinstance(obj, type) and issubclass(obj, BaseException))
            break
        self._cache[dotted] = result
        return result


_RESOLVER = _Resolver()


def family_of(dotted: str):
    for family, prefixes in FAMILY_PREFIXES:
        for prefix in prefixes:
            if dotted == prefix or dotted.startswith(prefix + ".") or (
                    prefix.startswith("os.") and dotted.startswith(prefix)):
                return family
    return None


OWN_PACKAGE = "recursion_core"


def _stdlib(top: str) -> bool:
    """The standard library, or the package itself (whose modules are scanned themselves)."""
    return top in sys.stdlib_module_names or top in ("__future__", OWN_PACKAGE)


class _Scanner(ast.NodeVisitor):
    def __init__(self, rel: str, tree: ast.AST):
        self.rel = rel
        self.findings: list[Finding] = []
        self.scope: list[str] = []
        self.aliases: dict[str, str] = {}
        self.module_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.aliases[a.asname] = a.name
                    else:
                        top = a.name.split(".")[0]
                        self.aliases[top] = top
                    self.module_names.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for a in node.names:
                    self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"

    # -- helpers
    def _fn(self):
        return ".".join(self.scope) or "<module>"

    def _add(self, kind, name, family, node):
        self.findings.append(Finding(kind, self.rel, self._fn(), name, family, getattr(node, "lineno", 0)))

    def _dotted(self, node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name) and node.id in self.aliases:
            parts.append(self.aliases[node.id])
            return ".".join(reversed(parts))
        return None

    def _reference(self, dotted, node):
        if dotted.split(".")[0] in SAFE_MODULES:
            return
        if dotted in IDENTITY_NAMES:
            self._add("identity", dotted, "identity", node)
        family = family_of(dotted)
        if family and not _RESOLVER.inert(dotted):
            self._add("reach", dotted, family, node)

    # -- scopes
    def _scoped(self, node):
        self.scope.append(node.name)
        for dec in node.decorator_list:
            self.visit(dec)
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                self.visit(base)
            for kw in node.keywords:
                self.visit(kw)
        else:
            for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                self.visit(default)
        for stmt in node.body:
            self.visit(stmt)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_AnnAssign(self, node):  # an annotation calls nothing
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)

    # -- imports
    def visit_Import(self, node):
        for a in node.names:
            self._import(a.name, node)

    def visit_ImportFrom(self, node):
        if node.level:
            return  # the package's own modules are scanned themselves
        for a in node.names:
            if a.name == "*":
                self._add("reach", f"from {node.module} import *", "dynamic", node)
            else:
                self._import(f"{node.module}.{a.name}", node, base=node.module)

    def _import(self, dotted, node, base=None):
        top = dotted.split(".")[0]
        if not _stdlib(top):
            self._add("reach", f"import {base or dotted}", "outside", node)
            return
        family = family_of(dotted) or (family_of(base) if base else None)
        if family and not (base and _RESOLVER.inert(dotted)):
            self._add("reach", f"import {dotted}", family, node)

    # -- references
    def visit_Attribute(self, node):
        dotted = self._dotted(node)
        if dotted is not None:
            self._reference(dotted, node)
            return
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id in self.aliases:
            target = self.aliases[node.id]
            if node.id in self.module_names and family_of(target):
                self._add("reach", f"{target} (the module as a value)", family_of(target), node)
            else:
                self._reference(target, node)
        elif isinstance(node.ctx, ast.Load) and node.id in DYNAMIC_BUILTINS:
            self._add("reach", DYNAMIC_BUILTINS[node.id], "dynamic", node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("getattr", "hasattr") and node.args:
            receiver = node.args[0]
            name = node.args[1] if len(node.args) > 1 else None
            literal = isinstance(name, ast.Constant) and isinstance(name.value, str)
            on_module = (isinstance(receiver, ast.Name) and receiver.id in self.module_names) or (
                isinstance(receiver, ast.Name) and receiver.id in ("builtins", "__builtins__")) or (
                isinstance(receiver, ast.Subscript) and self._dotted(receiver.value) == "sys.modules") or (
                isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name)
                and receiver.func.id in ("globals", "vars", "__import__"))
            if on_module and not literal:
                self._add("reach", "getattr(<module>, <a name decided at run time>)", "dynamic", node)
            elif on_module and literal and isinstance(receiver, ast.Name) and receiver.id in self.aliases:
                # getattr(socket, "AF_UNIX", None): the same as the attribute written out.
                self._reference(f"{self.aliases[receiver.id]}.{name.value}", node)
                for extra in node.args[2:]:
                    self.visit(extra)
                return
            elif literal and name.value in NETWORK_METHODS:
                self._add("reach", f".{name.value}() via getattr", "method", node)
        if isinstance(func, ast.Attribute) and self._dotted(func) is None and func.attr in NETWORK_METHODS:
            self._add("reach", f".{func.attr}()", "method", node)
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            for m in LITERAL.finditer(node.value):
                self._add("literal", m.group(0), "literal", node)
            for m in IDENTITY_WORDS.finditer(node.value):
                self._add("identity", f"word: {m.group(0).lower()}", "identity", node)


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def shipped_files(root: Path):
    base = root / PACKAGE
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        parts = path.relative_to(base).parts
        if "tests" in parts or path.name.startswith("test_") or "__pycache__" in parts:
            continue
        yield path, rel


# Files of other kinds the scan can read as data; any other file is refused as undecidable.
DATA_SUFFIXES = {".md", ".json", ".jsonl", ".txt", ".toml"}


def scan(root: Path = ROOT):
    findings: list[Finding] = []
    for path, rel in shipped_files(root):
        if path.suffix == ".py":
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            docs = _docstring_nodes(tree)
            scanner = _Scanner(rel, tree)
            scanner.visit(tree)
            for f in scanner.findings:
                if f.kind != "reach" and f.family in ("literal", "identity") and _in_doc(f, tree, docs):
                    continue
                findings.append(f)
        elif path.suffix not in DATA_SUFFIXES:
            findings.append(Finding("reach", rel, "<file>", f"a {path.suffix or 'plain'} file the scan cannot read",
                                    "undecidable", 0))
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            for m in LITERAL.finditer(text):
                if path.suffix in (".json", ".jsonl", ".toml"):
                    findings.append(Finding("literal", rel, "<data>", m.group(0), "literal", 0))
    return sorted(findings)


def _in_doc(finding, tree, docs):
    for node in ast.walk(tree):
        if id(node) in docs and getattr(node, "lineno", -1) <= finding.line <= getattr(node, "end_lineno", -1):
            return True
    return False


def load_list(root: Path = ROOT, path: Path | None = None):
    with open(path or (root / LIST_FILE), "rb") as fh:
        return tomllib.load(fh)


def check(findings, listing):
    """Every failure, as words a person reads; an empty list means the proof holds."""
    problems = []
    if not findings:
        problems.append("the scan found nothing at all: an empty scan is a broken scan "
                        "(is the package where the scan looks?)")
    covered = {}
    for kind, section in (("reach", "reach"), ("literal", "literal"), ("identity", "identity")):
        for i, row in enumerate(listing.get(section, [])):
            where = f"{section} row {i + 1} ({row.get('file')} {row.get('function')})"
            if kind == "reach":
                who = row.get("who")
                if who == "code" or who not in WHO:
                    problems.append(f"{where}: who supplies the address is {who!r}; it must be one of "
                                    f"{', '.join(sorted(WHO))}, never the code itself")
                if not str(row.get("says", "")).strip():
                    problems.append(f"{where}: has no sentence a person would read")
            elif not str(row.get("reason", "")).strip():
                problems.append(f"{where}: has no reason")
            for name in row.get("names", []):
                k = (kind, row.get("file"), row.get("function"), name)
                where_k, count = covered.get(k, (where, 0))
                covered[k] = (where_k, count + 1)
    found = {}
    for f in findings:
        found.setdefault(f.key(), []).append(f)
    for k, hits in sorted(found.items()):
        listed = covered.get(k, (None, 0))[1]
        if listed == 0:
            f = hits[0]
            problems.append(f"no row in {LIST_FILE.as_posix()} for {f.kind} {f.name!r} in {f.file} "
                            f"{f.function} (line {', '.join(str(h.line) for h in hits)}, family {f.family}): "
                            f"add a row that says who supplies the address, or take the call out")
        elif len(hits) != listed:
            problems.append(f"{covered[k][0]}: lists {k[3]!r} {listed} time(s), the scan finds it {len(hits)} "
                            f"time(s) (line {', '.join(str(h.line) for h in hits)}): every use has its own entry")
    for k, (where, listed) in covered.items():
        if k not in found:
            problems.append(f"{where}: lists {k[3]!r}, which the scan no longer finds: take the name "
                            f"out of the row")
    return problems


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    findings = scan(ROOT)
    if "--findings" in argv:
        for f in findings:
            print(f"{f.kind}\t{f.family}\t{f.file}\t{f.function}\t{f.name}\t{f.line}")
        return 0
    problems = check(findings, load_list(ROOT))
    for p in problems:
        print(p)
    if problems:
        print(f"NOT PROVEN: {len(problems)} problem(s) over {len(findings)} finding(s).")
        return 1
    print(f"PROVEN: all {len(findings)} finding(s) of the shipped package have a row; "
          f"no address comes from the code itself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
