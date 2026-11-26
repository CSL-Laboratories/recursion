"""Static proof that the product reaches no network address a person did not give it.

Usage: python3 tools/network_reach.py            check the shipped package against the list
       python3 tools/network_reach.py --findings  print every finding, listed or not

The scan reads every shipped Python file of ``core/recursion_core`` (not the tests) by its
syntax tree. It is a list of the ONLY things allowed, not a list of bad things it knows:

* every module a shipped file imports, or reaches through another module's attribute, must
  have a ``[[module]]`` row of ``tools/network_reach.toml`` with its reason (the package's own
  modules excepted); any other module is refused;
* a short list of constructs is refused outright (running text as code; reaching a module or a
  function by a built name; the foreign-function modules; the private socket, subprocess and
  zip-import modules; unpickling; the log handlers that send over a network; a shipped module
  that imports a test module or anything outside the package). An honest use the product needs
  has a ``[[construct]]`` row with its reason and the exact text of the call;
* each import and each reference that can open a connection, look up a name or start a program
  must have a ``[[reach]]`` row that says who supplies the address, and the row holds the exact
  text of every such call, so a call whose arguments change is refused until its row changes;
* a ``[[module]]`` row also holds the ``names`` the package takes from that module: any other name
  of an allowed module is refused, and a private name of another module (one that begins with '_')
  is refused outright;
* a ``[[construct]]`` row, and a ``[[reach]]`` row whose address comes from a person's settings or
  command, holds its ``callers``: a call of that place from anywhere else in the package is refused;
* a symbolic link anywhere under ``core/``, and a compiled file in a ``__pycache__`` folder that the
  interpreter would run instead of its source, are refused (the scan cannot see what they hold);
* a finding with no row, a row that covers nothing, a row whose address comes from the code
  itself, a file the scan cannot read, and a scan that finds nothing at all are each a failure.
"""
from __future__ import annotations

import ast
import hashlib
import builtins
import importlib
import importlib.util
import logging
import re
import sys
from collections import Counter
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
    ("dynamic", ("importlib", "runpy", "code.interact", "pkgutil.resolve_name")),
)
# A method called on an object the scan cannot type: these names open or name connections.
NETWORK_METHODS = {
    "connect", "connect_ex", "bind", "sendto", "sendmsg", "create_connection", "create_server",
    "create_unix_connection", "create_unix_server", "create_datagram_endpoint", "getaddrinfo",
    "getnameinfo", "sock_connect", "subprocess_exec", "subprocess_shell", "urlopen",
    "open_connection", "start_server", "start_tls", "wrap_socket",
}
# Modules whose references the scan does not follow. Empty: every module is followed and pinned by name.
SAFE_MODULES = set()
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

# The constructs refused outright in shipped source, and what each means to a person.
CONSTRUCTS = {
    "text-as-code": "running a text as code (exec, eval or compile, by any spelling)",
    "built-name": "reaching a module or a function by a name built at run time "
                  "(getattr and its kin, the modules table, vars, globals, locals, __dict__, __builtins__, "
                  "an import of a name that is not a literal)",
    "foreign-function": "a foreign-function module (it calls the operating system with no audit event)",
    "private-module": "a private module under socket, ssl, subprocess, os or zip import (it skips the public one's checks)",
    "unpickling": "unpickling or another loader that runs code found in data",
    "network-log-handler": "a log handler that sends records over a network",
    "outside-package": "a shipped module that imports a test module or anything outside the package",
    "interpreter-hook": "a builtin or a setting that makes the interpreter itself load a module or start a "
                        "program (help, breakpoint, a store into the process environment, a change of the "
                        "places modules are loaded from)",
    "private-name": "a private name of another module (a name that begins with '_' is not that module's "
                    "promise: uuid._get_command_stdout starts a program)",
}
# Modules refused outright: no [[module]] row can allow them.
REFUSED_MODULES = {
    "ctypes": "foreign-function", "_ctypes": "foreign-function", "cffi": "foreign-function",
    "_cffi_backend": "foreign-function",
    "_socket": "private-module", "_ssl": "private-module", "_posixsubprocess": "private-module",
    "_winapi": "private-module", "_overlapped": "private-module", "posix": "private-module",
    "nt": "private-module", "zipimport": "private-module", "_multiprocessing": "private-module",
    "pickle": "unpickling", "_pickle": "unpickling", "pickletools": "unpickling", "marshal": "unpickling",
    "shelve": "unpickling", "dill": "unpickling", "cloudpickle": "unpickling", "jsonpickle": "unpickling",
    "code": "text-as-code", "codeop": "text-as-code",
}
# Objects refused outright wherever the scan meets them (by name or through an attribute).
NETWORK_LOG_HANDLERS = ("logging.handlers.SocketHandler", "logging.handlers.DatagramHandler",
                        "logging.handlers.SysLogHandler", "logging.handlers.HTTPHandler",
                        "logging.handlers.SMTPHandler", "logging.handlers.NTEventLogHandler",
                        "logging.config.listen")
TEXT_AS_CODE = {"exec": "text-as-code", "eval": "text-as-code", "compile": "text-as-code",
                "__import__": "built-name", "__builtins__": "built-name", "vars": "built-name",
                "globals": "built-name", "locals": "built-name", "__loader__": "built-name",
                "__spec__": "built-name", "help": "interpreter-hook", "breakpoint": "interpreter-hook"}
BUILT_NAME_CALLS = {"getattr", "setattr", "hasattr", "delattr"}
BUILT_NAME_ATTRS = {"__dict__", "__getattribute__", "__builtins__", "__globals__", "__subclasses__",
                    "__import__", "__code__", "__mro__", "__bases__", "__base__", "__closure__"}
# The places modules are loaded from: any use of them in shipped source is refused.
IMPORT_PATH_NAMES = ("sys.path", "sys.meta_path", "sys.path_hooks", "sys.path_importer_cache")
# The process environment: read it freely; a store into it reaches every program started later.
ENV_NAMES = ("os.environ", "os.environb")
ENV_READS = {"get", "copy", "items", "keys", "values"}
ENV_STORE_CALLS = ("os.putenv", "os.unsetenv")
# Methods refused on any object: they load native code into the process.
REFUSED_METHODS = {"enable_load_extension": "foreign-function", "load_extension": "foreign-function"}
# Rows whose place is guarded by its callers: every construct row, and a reach row whose address comes from
# a person's settings or command (a call from elsewhere could hand it an address of its own).
GUARDED_WHO = ("settings", "command")
# Calls that load a module by the name in their first argument.
IMPORT_BY_NAME = {"importlib.import_module", "importlib.util.find_spec", "importlib.__import__",
                  "pkgutil.resolve_name", "operator.attrgetter", "operator.methodcaller"}
# Files outside the package that the interpreter would run by itself.
INTERPRETER_FILES = ("*.pth",)

OWN_PACKAGE = "recursion_core"


@dataclass(frozen=True, order=True)
class Finding:
    kind: str       # 'reach', 'literal', 'identity', 'module', 'construct', 'name' (a name taken from a
                    # module: family = the module) or 'call' (a use of the package's own function: name = it)
    file: str
    function: str
    name: str
    family: str
    line: int
    text: str = ""  # the source text of the call or statement (pinned by the row)

    def key(self):
        return (self.kind, self.file, self.function, self.name)


def _refused_object(obj):
    """The construct an object of the standard library is, or None."""
    for dotted in NETWORK_LOG_HANDLERS:
        mod, _, attr = dotted.rpartition(".")
        m = sys.modules.get(mod)
        if m is not None and getattr(m, attr, None) is obj:
            return "network-log-handler"
    if obj is sys.modules or obj is builtins or obj is vars(builtins):
        return "built-name"
    for name in ("exec", "eval", "compile"):
        if obj is getattr(builtins, name):
            return "text-as-code"
    if obj is builtins.__import__:
        return "built-name"
    return None


class _Resolver:
    def __init__(self):
        self._cache = {}
        self._paths = {}
        for name in NETWORK_LOG_HANDLERS:  # loaded so that a handler is known under any name
            importlib.import_module(name.rpartition(".")[0])

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

    def path(self, dotted: str):
        """Each prefix of a dotted name with the object it is: [(prefix, object), ...] as far as it resolves.

        Only names of the standard library are resolved: the scan never imports the product, and what
        else is installed must not change what it finds (a module outside the standard library has its
        names pinned from the import statements instead)."""
        if dotted in self._paths:
            return self._paths[dotted]
        parts = dotted.split(".")
        out = []
        if parts[0] != OWN_PACKAGE and parts[0] in sys.stdlib_module_names:
            try:
                obj = importlib.import_module(parts[0])
                out.append((parts[0], obj))
                for i in range(1, len(parts)):
                    prefix = ".".join(parts[:i + 1])
                    if hasattr(obj, parts[i]):
                        obj = getattr(obj, parts[i])
                    elif isinstance(obj, type(sys)):
                        obj = importlib.import_module(prefix)
                    else:
                        break
                    out.append((prefix, obj))
            except Exception:
                pass
        self._paths[dotted] = out
        return out


_RESOLVER = _Resolver()


def family_of(dotted: str):
    for family, prefixes in FAMILY_PREFIXES:
        for prefix in prefixes:
            if dotted == prefix or dotted.startswith(prefix + ".") or (
                    prefix.startswith("os.") and dotted.startswith(prefix)):
                return family
    return None


def _test_like(part: str) -> bool:
    return part == "tests" or part.startswith("test_") or part in ("conftest", "test")


class _Scanner(ast.NodeVisitor):
    def __init__(self, rel: str, tree: ast.AST, root: Path):
        self.rel = rel
        self.root = root
        self.findings: list[Finding] = []
        self.scope: list[str] = []
        self.aliases: dict[str, str] = {}
        self.module_names: set[str] = set()
        self.imported: set[str] = set()  # every module an import statement names
        self.annotation = 0
        # The package this file is in (for a module and for a package's __init__ alike).
        self.package_parts = list(Path(rel).relative_to(PACKAGE.parent).with_suffix("").parts[:-1])
        own = list(Path(rel).relative_to(PACKAGE.parent).with_suffix("").parts)
        self.own_module = ".".join(own[:-1] if own[-1] == "__init__" else own)
        self.local_defs = {n.name for n in getattr(tree, "body", ())
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child._nr_parent = node
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.aliases[a.asname] = a.name
                    else:
                        top = a.name.split(".")[0]
                        self.aliases[top] = top
                    self.module_names.add(a.asname or a.name.split(".")[0])
                    self.imported.add(a.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                self.imported.add(node.module)
                for a in node.names:
                    self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"
            elif isinstance(node, ast.ImportFrom) and node.level and node.level - 1 <= len(self.package_parts) - 1:
                # The package's own name, imported relatively: its uses are followed as calls of the package.
                base = self.package_parts[:len(self.package_parts) - (node.level - 1)]
                mod = ".".join(base + (node.module.split(".") if node.module else []))
                for a in node.names:
                    if a.name != "*":
                        self.aliases[a.asname or a.name] = f"{mod}.{a.name}"

    # -- helpers
    def _fn(self):
        return ".".join(self.scope) or "<module>"

    @staticmethod
    def _text(node):
        """The text a row pins: the whole call a reference is called in, or the statement or node itself."""
        cur = node
        while True:
            parent = getattr(cur, "_nr_parent", None)
            if isinstance(parent, ast.Attribute) and parent.value is cur:
                cur = parent
                continue
            if isinstance(parent, ast.Call) and parent.func is cur:
                return ast.unparse(parent)
            break
        text = ast.unparse(cur)
        # Not called here: a callable handed on as a value. Where it is bound to a name (a parameter's
        # default, an assignment), the calls of that name are part of what the row pins.
        calls = _Scanner._bound_calls(cur)
        return text + (" called as: " + " ; ".join(calls) if calls else "")

    @staticmethod
    def _bound_calls(node):
        parent = getattr(node, "_nr_parent", None)
        names, scope = [], None
        if isinstance(parent, ast.arguments):
            positional = parent.posonlyargs + parent.args
            for i, d in enumerate(parent.defaults):
                if d is node:
                    names.append(positional[len(positional) - len(parent.defaults) + i].arg)
            for i, d in enumerate(parent.kw_defaults):
                if d is node:
                    names.append(parent.kwonlyargs[i].arg)
            scope = getattr(parent, "_nr_parent", None)
        elif isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node:
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            scope = parent
            while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                               ast.Lambda, ast.Module)):
                scope = getattr(scope, "_nr_parent", None)
        if not names or scope is None:
            return []
        # A bound name bound again (_p = popen) is the same callable: its calls are pinned too.
        names, grown = set(names), True
        while grown:
            grown = False
            for c in ast.walk(scope):
                if isinstance(c, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and isinstance(c.value, ast.Name) \
                        and c.value.id in names:
                    for t in (c.targets if isinstance(c, ast.Assign) else [c.target]):
                        if isinstance(t, ast.Name) and t.id not in names:
                            names.add(t.id)
                            grown = True
        out = []
        for c in ast.walk(scope):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in names:
                out.append(ast.unparse(c))
            elif isinstance(c, ast.Name) and c.id in names and isinstance(c.ctx, ast.Load):
                up = getattr(c, "_nr_parent", None)
                if isinstance(up, ast.Call) and up.func is c:
                    continue
                while up is not None and not isinstance(up, ast.stmt):
                    up = getattr(up, "_nr_parent", None)
                out.append("handed on: " + (ast.unparse(up) if up is not None else c.id))
        return out

    def _add(self, kind, name, family, node, text=None):
        self.findings.append(Finding(kind, self.rel, self._fn(), name, family, getattr(node, "lineno", 0),
                                     self._text(node) if text is None else text))

    def _construct(self, construct, name, node, text=None):
        self._add("construct", name, construct, node, text)

    def _dotted(self, node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name) and node.id in self.aliases:
            parts.append(self.aliases[node.id])
            return ".".join(reversed(parts))
        return None

    def _module_use(self, name, node, text=None):
        """A module the file imports or reaches: refused outright, or it needs a [[module]] row."""
        top = name.split(".")[0]
        if top == OWN_PACKAGE or top == "__future__":
            return
        refused = REFUSED_MODULES.get(name) or REFUSED_MODULES.get(top)
        if refused:
            self._construct(refused, f"module {name}", node, text)
        else:
            self._add("module", name, "module", node, text)

    def _own_use(self, dotted, node):
        """A use of the package's own function or class: recorded so a guarded row can pin its callers."""
        parent = getattr(node, "_nr_parent", None)
        how = "called" if isinstance(parent, ast.Call) and parent.func is node else "handed on"
        self.findings.append(Finding("call", self.rel, self._fn(), dotted, how, getattr(node, "lineno", 0)))

    def _environment(self, env, dotted, node):
        """True when the use of the process environment is a store (or the environment handed on to be stored)."""
        parent = getattr(node, "_nr_parent", None)
        if dotted != env:
            return dotted[len(env) + 1:].split(".")[0] not in ENV_READS
        if isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del)):
            return True
        if isinstance(parent, ast.Subscript) and parent.value is node:
            return isinstance(parent.ctx, (ast.Store, ast.Del))
        return isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.AugAssign)) and parent.value is node

    def _taken(self, dotted, node):
        """Each name taken from a module along a dotted name: pinned by the module's row, private ones refused."""
        parts = dotted.split(".")
        if parts[0] in (OWN_PACKAGE, "__future__"):
            return
        path = _RESOLVER.path(dotted)
        if parts[0] not in sys.stdlib_module_names and parts[0] not in (OWN_PACKAGE, "__future__"):
            # Not resolved: the longest prefix an import statement names is the module, the next part its name.
            for cut in range(len(parts) - 1, 0, -1):
                if ".".join(parts[:cut]) in self.imported:
                    attr = parts[cut]
                    if ".".join(parts[:cut + 1]) in self.imported:
                        return  # a module of its own
                    if attr.startswith("_") and not (attr.startswith("__") and attr.endswith("__")):
                        self._construct("private-name", f"{'.'.join(parts[:cut])}.{attr}", node)
                    else:
                        self.findings.append(Finding("name", self.rel, self._fn(), attr, ".".join(parts[:cut]),
                                                     getattr(node, "lineno", 0)))
                    return
            return
        for i, (prefix, obj) in enumerate(path):
            if not isinstance(obj, type(sys)) or i + 1 >= len(parts):
                continue
            attr = parts[i + 1]
            nxt = path[i + 1][1] if i + 1 < len(path) else None
            if isinstance(nxt, type(sys)):
                continue  # a module of its own: its own row governs it
            if attr.startswith("_") and not (attr.startswith("__") and attr.endswith("__")):
                self._construct("private-name", f"{prefix}.{attr}", node)
            else:
                self.findings.append(Finding("name", self.rel, self._fn(), attr, prefix, getattr(node, "lineno", 0)))

    def _reference(self, dotted, node):
        if dotted.split(".")[0] == OWN_PACKAGE:
            self._own_use(dotted, node)
            return
        if dotted.split(".")[0] in SAFE_MODULES:
            return
        if dotted in IMPORT_PATH_NAMES or dotted.startswith(tuple(n + "." for n in IMPORT_PATH_NAMES)):
            self._construct("interpreter-hook", f"{dotted} (the places modules are loaded from)", node)
            return
        if dotted in ENV_STORE_CALLS:
            self._construct("interpreter-hook", f"{dotted} (a store into the process environment)", node)
            return
        env = next((n for n in ENV_NAMES if dotted == n or dotted.startswith(n + ".")), None)
        if env and self._environment(env, dotted, node):
            self._construct("interpreter-hook", f"{dotted} (a store into the process environment)", node)
            return
        whole = _RESOLVER.path(dotted)
        if isinstance(node, ast.Attribute) and len(whole) == len(dotted.split(".")) and len(whole) > 1 \
                and isinstance(whole[-1][1], type(sys)) and not getattr(node, "_nr_receiver", False) \
                and not self._presence_test(node):
            # A module reached through a dotted name and handed on as a value (m = os.path).
            self._construct("built-name", f"module {dotted} (handed on as a value)", node)
            return
        self._taken(dotted, node)
        if dotted in IDENTITY_NAMES:
            self._add("identity", dotted, "identity", node)
        # Follow the name through the real objects: a module reached through another module's
        # attribute is a module use; a refused object is refused under whatever name it is reached.
        for prefix, obj in _RESOLVER.path(dotted)[1:]:
            if isinstance(obj, type(sys)):
                self._module_use(obj.__name__ if obj.__name__ in REFUSED_MODULES else prefix, node)
            refused = _refused_object(obj)
            if refused:
                self._construct(refused, f"{prefix} (reached as {dotted})", node)
                return
        call = getattr(node, "_nr_parent", None)
        called = isinstance(call, ast.Call) and call.func is node
        family = family_of(dotted)
        if family and not _RESOLVER.inert(dotted) and (called or not self.annotation):
            self._add("reach", dotted, family, node)
        if dotted in IMPORT_BY_NAME and called:
            self._import_by_name(dotted, call)

    def _import_by_name(self, dotted, call):
        arg = call.args[0] if call.args else None
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            self._construct("built-name", f"{dotted}(<a name decided at run time>)", call, ast.unparse(call))
        elif dotted.startswith(("importlib", "pkgutil")) and not arg.value.startswith("."):
            self._module_use(arg.value.split(":")[0], call, ast.unparse(call))

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
            args = node.args
            for default in args.defaults + [d for d in args.kw_defaults if d is not None]:
                self.visit(default)
            # Annotations are code too: they run when the function is defined (or read).
            for a in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
                if a is not None and a.annotation is not None:
                    self._annotation(a.annotation)
            if node.returns is not None:
                self._annotation(node.returns)
        for tp in getattr(node, "type_params", ()):
            self.visit(tp)
        for stmt in node.body:
            self.visit(stmt)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def _annotation(self, node):
        # A name in an annotation is only a type; a call in it runs. Both are visited, and a
        # reference that is not called is not a reach of its own.
        self.annotation += 1
        try:
            self.visit(node)
        finally:
            self.annotation -= 1

    def visit_AnnAssign(self, node):
        self._annotation(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)

    # -- imports
    def visit_Import(self, node):
        for a in node.names:
            self._module_use(a.name, node)
            if a.name.split(".")[0] == OWN_PACKAGE:
                self._own_import(a.name.split(".")[1:], None, node)
            self._import(a.name, node)

    def visit_ImportFrom(self, node):
        if node.level:
            up = node.level - 1
            if up > len(self.package_parts) - 1:
                self._construct("outside-package", f"from {'.' * node.level}{node.module or ''} import ...", node)
                return
            base = self.package_parts[:len(self.package_parts) - up]
            target = base + (node.module.split(".") if node.module else [])
            self._own_import(target[1:], [a.name for a in node.names], node)
            return
        if node.module.split(".")[0] == OWN_PACKAGE:
            self._own_import(node.module.split(".")[1:], [a.name for a in node.names], node)
            return
        self._module_use(node.module, node)
        for a in node.names:
            if a.name == "*":
                self._add("reach", f"from {node.module} import *", "dynamic", node)
                continue
            dotted = f"{node.module}.{a.name}"
            self._taken(dotted, node)
            path = _RESOLVER.path(dotted)
            if len(path) == len(dotted.split(".")):
                obj = path[-1][1]
                if isinstance(obj, type(sys)):
                    self._module_use(obj.__name__ if obj.__name__ in REFUSED_MODULES else dotted, node)
                refused = _refused_object(obj)
                if refused:
                    self._construct(refused, f"from {node.module} import {a.name}", node)
                    continue
            self._import(dotted, node, base=node.module)

    def _own_import(self, parts, names, node):
        """An import of the package's own module: never a test module, never a file the scan skips."""
        pkg_dir = self.root / PACKAGE
        targets = [parts]
        for n in names or ():
            t = parts + [n]
            if n != "*" and (pkg_dir.joinpath(*t).with_suffix(".py").exists() or pkg_dir.joinpath(*t).is_dir()):
                targets.append(t)  # the name is a module of its own, not a name inside one
        for t in targets:
            if any(_test_like(p) for p in t):
                self._construct("outside-package", f"the test module {'.'.join([OWN_PACKAGE] + t)}", node)
                return

    def _import(self, dotted, node, base=None):
        top = dotted.split(".")[0]
        if top not in sys.stdlib_module_names and top not in ("__future__", OWN_PACKAGE):
            return  # a module outside the standard library: its [[module]] row carries the reason
        family = family_of(dotted) or (family_of(base) if base else None)
        if family and not (base and _RESOLVER.inert(dotted)):
            self._add("reach", f"import {dotted}", family, node)

    # -- references
    def visit_Attribute(self, node):
        if node.attr in BUILT_NAME_ATTRS:
            self._construct("built-name", f".{node.attr}", node)
        dotted = self._dotted(node)
        if dotted is not None:
            self._reference(dotted, node)
            return
        self.generic_visit(node)

    def _is_module(self, name, target):
        if name in self.module_names:
            return True
        path = _RESOLVER.path(target)
        return len(path) == len(target.split(".")) and isinstance(path[-1][1], type(sys))

    @staticmethod
    def _presence_test(node):
        """A module name only tested for being there (fcntl is None, if termios:, not pwd): nothing is reached."""
        parent = getattr(node, "_nr_parent", None)
        if isinstance(parent, ast.Compare):
            return all(isinstance(op, (ast.Is, ast.IsNot)) for op in parent.ops) and all(
                isinstance(c, ast.Constant) and c.value is None for c in [parent.left] + parent.comparators if c is not node)
        if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
            return True
        if isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)) and parent.test is node:
            return True
        return isinstance(parent, ast.BoolOp) and isinstance(getattr(parent, "_nr_parent", None), (ast.If, ast.While, ast.IfExp, ast.Assert, ast.UnaryOp))

    def visit_Name(self, node):
        if not isinstance(node.ctx, ast.Load):
            return
        parent = getattr(node, "_nr_parent", None)
        called = isinstance(parent, ast.Call) and parent.func is node
        if node.id in self.aliases:
            target = self.aliases[node.id]
            if target.split(".")[0] != OWN_PACKAGE and not getattr(node, "_nr_receiver", False) \
                    and not self._presence_test(node) and self._is_module(node.id, target):
                # A module handed on as a value: what is later reached through it is not seen.
                self._construct("built-name", f"module {target} (handed on as a value)", node)
            else:
                self._reference(target, node)
        elif node.id in TEXT_AS_CODE:
            self._construct(TEXT_AS_CODE[node.id], f"{node.id}", node)
        elif node.id in BUILT_NAME_CALLS and not called:
            self._construct("built-name", f"{node.id} (handed on as a value)", node)
        elif node.id in self.local_defs:
            self._own_use(f"{self.own_module}.{node.id}", node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Name) and func.id in BUILT_NAME_CALLS and node.args:
            receiver = node.args[0]
            receiver._nr_receiver = True  # reported with this call, not again as a module handed on
            name = node.args[1] if len(node.args) > 1 else None
            literal = isinstance(name, ast.Constant) and isinstance(name.value, str)
            if not literal:
                self._construct("built-name", f"{func.id}(<object>, <a name decided at run time>)", node,
                                ast.unparse(node))
            elif isinstance(receiver, ast.Name) and receiver.id in self.aliases:
                # getattr(socket, "AF_UNIX", None): the same as the attribute written out.
                self._reference(f"{self.aliases[receiver.id]}.{name.value}", node)
                for extra in node.args[2:]:
                    self.visit(extra)
                return
            elif name.value in NETWORK_METHODS:
                self._add("reach", f".{name.value}() via getattr", "method", node, ast.unparse(node))
            elif name.value in BUILT_NAME_ATTRS or name.value in TEXT_AS_CODE:
                self._construct("built-name", f"{func.id}(<object>, {name.value!r})", node, ast.unparse(node))
        if isinstance(func, ast.Attribute) and self._dotted(func) is None and func.attr in NETWORK_METHODS:
            self._add("reach", f".{func.attr}()", "method", node, ast.unparse(node))
        if isinstance(func, ast.Attribute) and func.attr in REFUSED_METHODS:
            self._construct(REFUSED_METHODS[func.attr], f".{func.attr}()", node, ast.unparse(node))
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            for m in LITERAL.finditer(node.value):
                self._add("literal", m.group(0), "literal", node, "")
            for m in IDENTITY_WORDS.finditer(node.value):
                self._add("identity", f"word: {m.group(0).lower()}", "identity", node, "")


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


def _unseen(root: Path):
    """Places the walk of the package cannot see into, refused: a symbolic link anywhere under core/ (the
    interpreter follows it, the walk does not), a Python file or a folder the interpreter may import in place
    of a module (a Python file directly under core/; one named like a standard module anywhere under core/;
    a folder outside the package, or a Python file there, named like a standard module or like the top name
    of a [[module]] row: the tests folder is on the import path of the tests and is not scanned), any file
    the interpreter can import (source, compiled or extension) directly under core/, any file or folder
    anywhere under core/ named like a standard module whatever its suffix, a compiled file outside
    __pycache__ with no source beside it, an extension module anywhere (machine code the scan cannot read,
    imported in place of a source of its name), and a
    compiled file in a __pycache__ folder that the interpreter would run instead of its source. A compiled file for this interpreter is compared with its
    source; one the interpreter would not use (stale) is harmless; one for another interpreter is judged by
    the run of the scan under that interpreter, and an unchecked one is refused under any interpreter."""
    import importlib.machinery
    import os
    compiled = tuple(importlib.machinery.BYTECODE_SUFFIXES)
    extension = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    importable = tuple(importlib.machinery.all_suffixes())
    out = []
    top = root / PACKAGE.parent
    if not top.is_dir():
        return out
    package = root / PACKAGE
    try:
        rows = load_list(root if (root / LIST_FILE).is_file() else ROOT).get("module", [])
    except (OSError, ValueError):
        rows = []
    listed = {str(row.get("name", "")).split(".")[0] for row in rows} - {"", OWN_PACKAGE}
    for here, dirs, files in os.walk(top, followlinks=False):
        outside = Path(here) != package and package not in Path(here).parents
        for name in sorted(dirs + files):
            path = Path(here) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                out.append(Finding("reach", rel, "<file>", "a symbolic link the scan does not follow", "undecidable", 0))
            elif name in files and name.endswith(".py") and (Path(here) == top or name[:-3] in
                                                             sys.stdlib_module_names or (outside and name[:-3] in listed)):
                out.append(Finding("reach", rel, "<file>", "a Python file beside the package that the interpreter "
                                   "may import in place of a module", "undecidable", 0))
            elif name in dirs and outside and path != package and (name in sys.stdlib_module_names or name in listed):
                out.append(Finding("reach", rel, "<file>", "a folder beside the package named like a module, which "
                                   "the interpreter may import in place of that module", "undecidable", 0))
            elif Path(here).name == "__pycache__" and name in files:
                pass  # judged below
            elif name in files and name.endswith(extension):
                out.append(Finding("reach", rel, "<file>", "an extension module, machine code the scan cannot "
                                   "read, which the interpreter imports in place of a source of its name",
                                   "undecidable", 0))
                continue
            elif name in files and name.endswith(compiled) and not (Path(here) / (
                    name[:-len(next(s for s in compiled if name.endswith(s)))] + ".py")).is_file():
                out.append(Finding("reach", rel, "<file>", "a compiled file with no source beside it, which "
                                   "the interpreter imports as a module: the scan reads only sources",
                                   "undecidable", 0))
                continue
            elif (name in files and Path(here) == top and name.endswith(importable)) or \
                    name.split(".")[0] in sys.stdlib_module_names or (outside and name.split(".")[0] in listed):
                out.append(Finding("reach", rel, "<file>", "a file or folder named like a module (whatever its "
                                   "suffix), which the interpreter may import in place of that module",
                                   "undecidable", 0))
                continue
            if Path(here).name == "__pycache__" and name in files and not path.is_symlink():
                why = _compiled_file(path)
                if why:
                    out.append(Finding("reach", rel, "<file>", f"a compiled file the interpreter would run: {why}",
                                       "undecidable", 0))
    return out


def _compiled_file(path: Path):
    """Why a __pycache__ file is refused, or None when the interpreter would never run anything but its source."""
    import marshal
    parts = path.name.split(".")
    if len(parts) < 3 or parts[-1] != "pyc":
        return "not a compiled module file"
    source = path.parent.parent / (parts[0] + ".py")
    data = path.read_bytes()
    if len(data) < 16:
        return "too short to be judged"
    flags = int.from_bytes(data[4:8], "little")
    if flags & 0b1 and not flags & 0b10:
        return "it holds an unchecked hash: the interpreter runs it without looking at its source"
    if not source.is_file():
        return None  # the interpreter never runs a __pycache__ file without its source
    if parts[1] != sys.implementation.cache_tag or data[:4] != importlib.util.MAGIC_NUMBER:
        return None  # judged by the run of the scan under the interpreter it was written for
    src = source.read_bytes()
    if flags & 0b1:
        if data[8:16] != importlib.util.source_hash(src):
            return None  # stale: the interpreter compiles the source again
    else:
        st = source.stat()
        if int.from_bytes(data[8:12], "little") != (int(st.st_mtime) & 0xFFFFFFFF) or \
                int.from_bytes(data[12:16], "little") != (st.st_size & 0xFFFFFFFF):
            return None  # stale: the interpreter compiles the source again
    opt = 0
    if len(parts) == 4 and parts[2].startswith("opt-") and parts[2][4:].isdigit():
        opt = int(parts[2][4:])
    try:
        held = marshal.loads(data[16:])
        made = compile(src, str(source), "exec", dont_inherit=True, optimize=opt)
    except Exception as exc:
        return f"it cannot be compared with its source ({type(exc).__name__})"
    return None if held == made else "its code is not the code of its source"


# Files of other kinds the scan can read as data; any other file is refused as undecidable.
DATA_SUFFIXES = {".md", ".json", ".jsonl", ".txt", ".toml"}


class UnreadableFile(ValueError):
    """A shipped file the scan cannot read or parse: the scan proves nothing, so it stops."""

    def __init__(self, rel, exc):
        super().__init__(f"{type(exc).__name__}: {exc}")
        self.filename = rel


def _parse(path, rel):
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as exc:
        raise UnreadableFile(rel, exc) from exc


# The source encoding declaration of PEP 263, in the first two lines of a file.
CODING = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
UTF8_NAMES = {"utf-8", "utf8", "utf_8", "utf-8-sig"}


def _coding(path: Path):
    """The encoding a file declares that is not utf-8 (the interpreter would then read it unlike the scan),
    or None."""
    with open(path, "rb") as fh:
        head = [fh.readline(), fh.readline()]
    for line in head:
        m = CODING.match(line)
        if m:
            name = m.group(1).decode("ascii", "replace").lower()
            return None if name in UTF8_NAMES else name
        if line.strip() and not line.lstrip().startswith(b"#"):
            return None  # the declaration counts only before the first line of code
    return None


def _digest(path: Path):
    """The SHA-256 of a file the scan cannot parse, with line ends made plain: its row holds it, so a change
    of the file is a changed call."""
    return "sha256:" + hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def scan(root: Path = ROOT):
    findings: list[Finding] = []
    for path, rel in shipped_files(root):
        if path.suffix == ".py" and _coding(path):
            findings.append(Finding("reach", rel, "<file>", f"a coding declaration other than utf-8 "
                                    f"({_coding(path)}): the interpreter would read the file unlike the scan",
                                    "undecidable", 0))
            continue
        if path.suffix == ".py":
            tree = _parse(path, rel)
            docs = _docstring_nodes(tree)
            scanner = _Scanner(rel, tree, root)
            scanner.visit(tree)
            for f in scanner.findings:
                if f.kind in ("literal", "identity") and _in_doc(f, tree, docs):
                    continue
                findings.append(f)
        elif path.suffix not in DATA_SUFFIXES:
            findings.append(Finding("reach", rel, "<file>", f"a {path.suffix or 'plain'} file the scan cannot read",
                                    "undecidable", 0, _digest(path)))
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            for m in LITERAL.finditer(text):
                if path.suffix in (".json", ".jsonl", ".toml"):
                    findings.append(Finding("literal", rel, "<data>", m.group(0), "literal", 0))
    findings.extend(_unseen(root))
    # A file beside the package that the interpreter runs by itself when it is installed.
    if (root / PACKAGE).is_dir():
        for pattern in INTERPRETER_FILES:
            for path in sorted((root / PACKAGE.parent).rglob(pattern)):
                rel = path.relative_to(root).as_posix()
                if not any(f.file == rel for f in findings):
                    findings.append(Finding("reach", rel, "<file>", f"a {path.suffix} file the interpreter runs by itself",
                                            "undecidable", 0))
    return sorted(findings)


def _in_doc(finding, tree, docs):
    for node in ast.walk(tree):
        if id(node) in docs and getattr(node, "lineno", -1) <= finding.line <= getattr(node, "end_lineno", -1):
            return True
    return False


def load_list(root: Path = ROOT, path: Path | None = None):
    with open(path or (root / LIST_FILE), "rb") as fh:
        return tomllib.load(fh)


def _pins(findings, rows, section, problems, key_of):
    """Every call a row covers is pinned by its text: the texts the scan finds in a place must be
    exactly the texts the rows of that place hold."""
    want, where = {}, {}
    for i, row in enumerate(rows):
        k = key_of(row)
        want.setdefault(k, Counter()).update(row.get("calls", []))
        where.setdefault(k, f"{section} row {i + 1} ({row.get('file')} {row.get('function')})")
    have = {}
    for f in findings:
        have.setdefault(key_of(f), Counter())[f.text] += 1
    for k in sorted(set(want) & set(have), key=str):
        if want[k] != have[k]:
            new = sorted((have[k] - want[k]).elements())
            gone = sorted((want[k] - have[k]).elements())
            problems.append(f"refused (changed-call): {where[k]}: the calls in this place are not the calls "
                            f"the row holds; found now {new!r}, held by the row {gone!r}: a call whose "
                            f"arguments changed is a new call; read it, then put its text in the row's calls")


def check(findings, listing):
    """Every failure, as words a person reads; an empty list means the proof holds."""
    problems = []
    if not findings:
        problems.append("the scan found nothing at all: an empty scan is a broken scan "
                        "(is the package where the scan looks?)")
    # -- the allow list of modules
    allowed = {}
    for i, row in enumerate(listing.get("module", [])):
        name = row.get("name")
        where = f"module row {i + 1} ({name})"
        if not str(row.get("reason", "")).strip():
            problems.append(f"{where}: has no reason")
        if name in REFUSED_MODULES or str(name).split(".")[0] in REFUSED_MODULES:
            problems.append(f"{where}: a [[module]] row cannot allow {name}: it is refused outright "
                            f"({REFUSED_MODULES.get(name) or REFUSED_MODULES[str(name).split('.')[0]]}); "
                            f"an honest use needs a [[construct]] row with its reason and the call")
        allowed[name] = where
    used = set()
    for f in findings:
        if f.kind != "module":
            continue
        used.add(f.name)
        if f.name not in allowed:
            problems.append(f"refused (unlisted-module): {f.name!r} in {f.file} {f.function} (line {f.line}): "
                            f"only the modules of the [[module]] rows of {LIST_FILE.as_posix()} may be imported "
                            f"or reached; add a row with the reason the product needs it, or take it out")
    for name, where in allowed.items():
        if name not in used:
            problems.append(f"{where}: allows {name!r}, which no shipped file imports any more: take the row out")
    # -- the names each allowed module may give
    taken = {}
    for f in findings:
        if f.kind == "name":
            taken.setdefault(f.family, {}).setdefault(f.name, f)
    rows_by_name = {row.get("name"): row for row in listing.get("module", [])}
    for mod in sorted(taken):
        row = rows_by_name.get(mod)
        names = set(row.get("names", [])) if row is not None else set()
        for name in sorted(set(taken[mod]) - names):
            f = taken[mod][name]
            problems.append(f"refused (unlisted-name): {mod}.{name} in {f.file} {f.function} (line {f.line}): "
                            f"only the names of a module's row may be taken from it; read what {name!r} does, "
                            f"then add it to the names of the row of {mod!r}, or take the use out")
    for i, row in enumerate(listing.get("module", [])):
        where = f"module row {i + 1} ({row.get('name')})"
        if "names" not in row:
            problems.append(f"{where}: holds no names: a row allows only the names it holds (names = [] for none)")
        for name in row.get("names", []):
            if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
                problems.append(f"{where}: cannot allow the private name {name!r}: a private name needs a "
                                f"[[construct]] row (private-name) with its reason and the call")
            elif name not in taken.get(row.get("name"), {}):
                problems.append(f"{where}: allows the name {name!r}, which no shipped file takes any more: "
                                f"take it out of the row")
    # -- the refused constructs
    construct_rows = listing.get("construct", [])
    for i, row in enumerate(construct_rows):
        where = f"construct row {i + 1} ({row.get('file')} {row.get('function')})"
        if row.get("construct") not in CONSTRUCTS:
            problems.append(f"{where}: construct {row.get('construct')!r} is not one of {', '.join(CONSTRUCTS)}")
        if not str(row.get("reason", "")).strip():
            problems.append(f"{where}: has no reason")
        if not row.get("calls"):
            problems.append(f"{where}: holds no call: a row allows only the exact calls it holds")
    ckey = lambda r: (r.file, r.function, r.family) if isinstance(r, Finding) else (
        r.get("file"), r.get("function"), r.get("construct"))
    held = {ckey(r) for r in construct_rows}
    cons = [f for f in findings if f.kind == "construct"]
    for f in cons:
        if ckey(f) not in held:
            problems.append(f"refused ({f.family}): {f.name!r} in {f.file} {f.function} (line {f.line}): "
                            f"{CONSTRUCTS[f.family]} is refused in shipped source; the call: {f.text!r}")
    for i, row in enumerate(construct_rows):
        if ckey(row) not in {ckey(f) for f in cons}:
            problems.append(f"construct row {i + 1} ({row.get('file')} {row.get('function')}): the scan no longer "
                            f"finds {row.get('construct')} there: take the row out")
    _pins(cons, construct_rows, "construct", problems, ckey)
    # -- the reach rows and the literal rows
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
        if f.kind in ("reach", "literal", "identity"):
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
    rkey = lambda r: (r.file, r.function) if isinstance(r, Finding) else (r.get("file"), r.get("function"))
    _pins([f for f in findings if f.kind == "reach"], listing.get("reach", []), "reach", problems, rkey)
    _callers(findings, listing, problems)
    return problems


def _dotted_of(file, function):
    """The package's own dotted name of a row's place, or None for a place that is not a function."""
    if not file or not function or function.startswith("<"):
        return None
    parts = list(Path(file).relative_to(PACKAGE.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts + [function])


def _callers(findings, listing, problems):
    """A guarded row's place may be used only from the places its row names (callers)."""
    uses = {}
    for f in findings:
        if f.kind == "call":
            uses.setdefault(f.name, set()).add(f"{f.file} {f.function}" + ("" if f.family == "called" else " (handed on)"))
    rows = [("construct", i, r) for i, r in enumerate(listing.get("construct", []))]
    rows += [("reach", i, r) for i, r in enumerate(listing.get("reach", [])) if r.get("who") in GUARDED_WHO]
    for section, i, row in rows:
        target = _dotted_of(row.get("file"), row.get("function"))
        if target is None:
            continue
        where = f"{section} row {i + 1} ({row.get('file')} {row.get('function')})"
        if "callers" not in row:
            problems.append(f"{where}: holds no callers: a guarded row names every place that uses it (callers = [] for none)")
            continue
        have = {u for name, us in uses.items() if name == target or name.startswith(target + ".") for u in us}
        want = set(row.get("callers", []))
        for u in sorted(have - want):
            problems.append(f"refused (new-caller): {u} uses {target}, which {where} allows only from "
                            f"{sorted(want)!r}: read the new use, then add it to the row's callers, or take it out")
        for u in sorted(want - have):
            problems.append(f"{where}: names the caller {u!r}, which no longer uses it: take it out of the row")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        findings = scan(ROOT)
        listing = load_list(ROOT)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as exc:
        where = getattr(exc, "filename", None) or "a file"
        what = str(exc) if isinstance(exc, UnreadableFile) else f"{type(exc).__name__}: {exc}"
        print(f"NOT PROVEN: {where} cannot be read ({what}). "
              f"A scan that cannot read a file proves nothing; make the file readable or take it out.")
        return 1
    if "--findings" in argv:
        for f in findings:
            print(f"{f.kind}\t{f.family}\t{f.file}\t{f.function}\t{f.name}\t{f.line}\t{f.text}")
        return 0
    problems = check(findings, listing)
    for p in problems:
        print(p)
    if problems:
        print(f"NOT PROVEN: {len(problems)} problem(s) over {len(findings)} finding(s).")
        return 1
    n_mod = len(listing.get("module", []))
    print(f"PROVEN: all {len(findings)} finding(s) of the shipped package have a row; it imports only the "
          f"{n_mod} module(s) of the allow list; no address comes from the code itself.")
    print("This is proven of the kinds the tests plant (core/tests/test_network_reach.py); the known ways "
          "past this scan that are not built are the rows of tools/network_gaps.toml, named in the README.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
