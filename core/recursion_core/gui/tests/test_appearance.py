"""Appearance picker: a core-bar form for the loaded theme and layout.

Run: PYTHONPATH=core python3 -B -m unittest recursion_core.gui.tests.test_appearance
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from pathlib import Path
from unittest import mock

from recursion_core.gui.layouts import load_user_layout
from recursion_core.gui.surface import Application
from recursion_core.gui.tests.test_surface import HOST, ORIGIN, GuiHarness

ROOT = Path(__file__).resolve().parents[4]
THEME_GOOD = ROOT / "schemas" / "gui" / "v1" / "fixtures" / "theme" / "good"
LAYOUT_GOOD = ROOT / "schemas" / "gui" / "v1" / "fixtures" / "layout" / "good"

_MAIN = re.compile(r'<main id="main"[^>]*>(.*)</main>', re.S)
_THEME_STYLE = re.compile(r'<style id="theme">.*?</style>', re.S)


def _main(body: str) -> str:
    match = _MAIN.search(body)
    if match is None:
        raise AssertionError("page has no main")
    return match.group(1)


def _core(body: str) -> str:
    """Bytes outside the themed workspace and outside the compiled theme sheet."""
    return _MAIN.sub("", _THEME_STYLE.sub("", body))


class AppearanceTests(GuiHarness):
    """The four picker checks. The form is fixed core markup, so a theme cannot change it."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="g1e-appearance-")
        self.addCleanup(self._tmp.cleanup)
        self.xdg = Path(self._tmp.name) / "xdg"
        self.gui = self.xdg / "recursion" / "gui"
        self.themes = self.gui / "themes"
        self.layouts = self.gui / "layouts"
        self.themes.mkdir(parents=True, mode=0o700)
        self.layouts.mkdir(mode=0o700)
        for path in (self.xdg, self.xdg / "recursion", self.gui, self.themes, self.layouts):
            os.chmod(path, 0o700)
        self._prev_xdg = os.environ.get("XDG_CONFIG_HOME")
        self._prev_home = os.environ.get("HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.xdg)
        os.environ["HOME"] = str(Path(self._tmp.name) / "home")
        self.addCleanup(self._restore_env)
        self.app = Application(
            self.app._client, self.door, frozenset({HOST}), frozenset({ORIGIN}), theme_root=self.gui)
        self.selection = self.gui / "selection.json"
        self.layout_path = self.gui / "layout.json"

    def _restore_env(self) -> None:
        if self._prev_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev_xdg
        if self._prev_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._prev_home

    def _write(self, path: Path, data: bytes, mode: int = 0o600) -> None:
        path.write_bytes(data)
        os.chmod(path, mode)

    def _put_theme(self, document: dict, filename: str | None = None) -> None:
        name = filename or (document["name"] + ".json")
        self._write(self.themes / name, json.dumps(document).encode("utf-8"))

    def _put_layout_file(self, document: dict, filename: str) -> None:
        self._write(self.layouts / filename, json.dumps(document).encode("utf-8"))

    def _choose(self, theme: str, layout: str) -> tuple[str, dict, str]:
        return self.request("POST", "/appearance", {"theme": theme, "layout": layout})

    def _reset(self) -> tuple[str, dict, str]:
        return self.request("POST", "/appearance", {"reset": "default"})

    def _mode(self, path: Path) -> int:
        info = os.lstat(path)
        self.assertFalse(stat.S_ISLNK(info.st_mode), path)
        self.assertTrue(stat.S_ISREG(info.st_mode), path)
        return stat.S_IMODE(info.st_mode)

    def test_post_without_the_door_is_refused(self) -> None:
        dusk = json.loads((THEME_GOOD / "g01-dusk.json").read_text(encoding="utf-8"))
        paper = json.loads((THEME_GOOD / "g02-paper.json").read_text(encoding="utf-8"))
        self._put_theme(dusk)
        self._put_theme(paper)
        previous = json.dumps({"theme": "dusk"}).encode("utf-8")
        self._write(self.selection, previous)
        self.door.ok = False
        status, headers, body = self._choose("paper", "")
        self.assertEqual(status, "401 Unauthorized")
        self.assertIn("device_not_authenticated", body)
        self.assertIn("form-action 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.selection.read_bytes(), previous)
        self.assertFalse(self.layout_path.exists())
        self.door.ok = True
        status, _, body = self.request(
            "POST", "/appearance", {"theme": "paper", "layout": ""}, headers={"HTTP_HOST": "evil.example"})
        self.assertTrue(status.startswith("403"), status)
        self.assertIn("host_not_allowed", body)
        self.assertEqual(self.selection.read_bytes(), previous)

    def test_reset_control_is_present_under_every_loaded_theme(self) -> None:
        documents = [json.loads(path.read_text(encoding="utf-8"))
                     for path in sorted(THEME_GOOD.glob("*.json"))]
        self.assertGreaterEqual(len(documents), 1)
        reset_html = None
        for document in documents:
            self._put_theme(document, "theme.json")
            self._write(self.selection, json.dumps({"theme": document["name"]}).encode("utf-8"))
            status, headers, body = self.request("GET", "/")
            with self.subTest(theme=document["name"]):
                self.assertEqual(status, "200 OK")
                self.assertIn('<style id="theme">', body)
                self.assertIn("form-action 'self'", headers["Content-Security-Policy"])
                self.assertIn('action="/appearance"', body)
                self.assertIn('id="appearance-reset"', body)
                self.assertIn("Reset appearance", body)
                self.assertNotIn('id="appearance-reset"', _main(body))
                self.assertIn('id="appearance-reset"', _core(body))
                self.assertIn('id="core-approval"', _core(body))
                self.assertNotIn(document["tokens"]["bg"].lower(), _core(body))
                found = body[body.index('id="appearance-reset"') - 80:body.index('id="appearance-reset"') + 80]
                if reset_html is None:
                    reset_html = found
                else:
                    self.assertEqual(found, reset_html)
        # The control does the reset, and the core bars stay the pinned text.
        status, _, body = self._reset()
        self.assertEqual(status, "200 OK", body)
        self.assertIn("Appearance reset.", body)
        self.assertEqual(json.loads(self.selection.read_bytes().decode("utf-8")), {"theme": None})
        self.assertEqual(self._mode(self.selection), 0o600)
        follow = self.request("GET", "/")[2]
        self.assertNotIn('<style id="theme">', follow)
        self.assertIn('id="appearance-reset"', _core(follow))
        self.assertIn("Every route is BETA", follow)
        self.assertIn("I confirm stopping this run", follow)
        self.assertIn("confirmed shows as pending", follow)

    def test_unknown_theme_or_layout_is_refused_with_a_text(self) -> None:
        dusk = json.loads((THEME_GOOD / "g01-dusk.json").read_text(encoding="utf-8"))
        layout = json.loads((LAYOUT_GOOD / "h01-two-columns.json").read_text(encoding="utf-8"))
        self._put_theme(dusk)
        self._put_layout_file(layout, "twocol.json")
        previous = json.dumps({"theme": "dusk"}).encode("utf-8")
        self._write(self.selection, previous)
        kept = self.layout_path
        self._write(kept, json.dumps(layout).encode("utf-8"))
        before_calls = list(self.api.calls)
        status, _, body = self._choose("not-a-theme", layout["name"])
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("unknown theme", body)
        self.assertEqual(self.selection.read_bytes(), previous)
        self.assertEqual(kept.read_bytes(), json.dumps(layout).encode("utf-8"))
        status, _, body = self._choose("dusk", "not-a-layout")
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("unknown layout", body)
        self.assertEqual(self.selection.read_bytes(), previous)
        self.assertEqual(kept.read_bytes(), json.dumps(layout).encode("utf-8"))
        self.assertEqual(self.api.calls, before_calls)

    def test_selection_file_is_owner_only_and_a_failed_write_keeps_the_previous(self) -> None:
        dusk = json.loads((THEME_GOOD / "g01-dusk.json").read_text(encoding="utf-8"))
        paper = json.loads((THEME_GOOD / "g02-paper.json").read_text(encoding="utf-8"))
        first = json.loads((LAYOUT_GOOD / "h01-two-columns.json").read_text(encoding="utf-8"))
        second = json.loads((LAYOUT_GOOD / "h02-no-panels.json").read_text(encoding="utf-8"))
        self._put_theme(dusk)
        self._put_theme(paper)
        self._put_layout_file(first, "first.json")
        self._put_layout_file(second, "second.json")
        previous = json.dumps({"theme": "dusk"}).encode("utf-8")
        self._write(self.selection, previous)
        previous_layout = json.dumps(first).encode("utf-8")
        self._write(self.layout_path, previous_layout)

        old_umask = os.umask(0)
        try:
            status, _, body = self._choose(paper["name"], second["name"])
        finally:
            os.umask(old_umask)
        self.assertEqual(status, "200 OK", body)
        self.assertIn("Appearance kept.", body)
        self.assertEqual(self._mode(self.selection), 0o600)
        self.assertEqual(self._mode(self.layout_path), 0o600)
        self.assertEqual(json.loads(self.selection.read_text(encoding="utf-8")), {"theme": "paper"})
        self.assertEqual(set(json.loads(self.selection.read_text(encoding="utf-8"))), {"theme"})
        page = self.request("GET", "/")[2]
        self.assertIn('<style id="theme">', page)
        self.assertIn("--theme-bg:#ffffff", page)
        self.assertIn('class="layout cols-1"', page)
        self.assertEqual(load_user_layout(frozenset())["name"], second["name"])
        temps = [path.name for path in self.gui.iterdir() if ".tmp-" in path.name]
        self.assertEqual(temps, [])

        # A symlink is not a place to write. The target keeps its bytes.
        outside = Path(self._tmp.name) / "outside-selection.json"
        self._write(outside, previous)
        self.selection.unlink()
        os.symlink(outside, self.selection)
        status, _, body = self._choose(paper["name"], second["name"])
        self.assertNotEqual(status, "200 OK", body)
        self.assertIn("appearance not kept", body)
        self.assertEqual(outside.read_bytes(), previous)
        self.assertTrue(stat.S_ISLNK(os.lstat(self.selection).st_mode))
        self.selection.unlink()
        self._write(self.selection, previous)
        outside_layout = Path(self._tmp.name) / "outside-layout.json"
        self._write(outside_layout, previous_layout)
        self.layout_path.unlink()
        os.symlink(outside_layout, self.layout_path)
        status, _, body = self._choose(paper["name"], second["name"])
        self.assertNotEqual(status, "200 OK", body)
        self.assertEqual(outside_layout.read_bytes(), previous_layout)
        self.assertEqual(self.selection.read_bytes(), previous)
        self.assertTrue(stat.S_ISLNK(os.lstat(self.layout_path).st_mode))
        self.layout_path.unlink()
        self._write(self.layout_path, previous_layout)

        # The directory accepts reads and refuses writes. The previous files stay.
        os.chmod(self.gui, 0o500)
        try:
            status, _, body = self._choose(paper["name"], second["name"])
            self.assertNotEqual(status, "200 OK", body)
            self.assertIn("appearance not kept", body)
            self.assertEqual(self.selection.read_bytes(), previous)
            self.assertEqual(self.layout_path.read_bytes(), previous_layout)
        finally:
            os.chmod(self.gui, 0o700)

        # The layout replace fails after the theme file is in place: the theme file comes back.
        from recursion_core.gui import surface as surface_mod

        real_rename = surface_mod.os.rename
        calls = {"n": 0}

        def rename(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected")
            return real_rename(src, dst)

        with mock.patch.object(surface_mod.os, "rename", side_effect=rename):
            status, _, body = self._choose(paper["name"], second["name"])
        self.assertNotEqual(status, "200 OK", body)
        self.assertIn("appearance not kept", body)
        self.assertEqual(self.selection.read_bytes(), previous)
        self.assertEqual(self.layout_path.read_bytes(), previous_layout)
        temps = [path.name for path in self.gui.iterdir() if ".tmp-" in path.name]
        self.assertEqual(temps, [])

        # Two callers each leave one whole owner-only file, never a torn one.
        errors: list[BaseException] = []
        started = threading.Barrier(2)

        def post(theme: str, layout_name: str) -> None:
            try:
                started.wait(timeout=10)
                status, _, body = self.request(
                    "POST", "/appearance", {"theme": theme, "layout": layout_name})
                if status != "200 OK":
                    raise AssertionError(body)
            except BaseException as exc:  # noqa: BLE001 - the main thread reports it
                errors.append(exc)

        threads = [
            threading.Thread(target=post, args=(paper["name"], second["name"])),
            threading.Thread(target=post, args=(dusk["name"], first["name"])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(self._mode(self.selection), 0o600)
        self.assertEqual(self._mode(self.layout_path), 0o600)
        chosen = json.loads(self.selection.read_text(encoding="utf-8"))
        self.assertIn(chosen, ({"theme": "paper"}, {"theme": "dusk"}))
        laid = load_user_layout(frozenset())
        self.assertIn(laid["name"], {first["name"], second["name"]})
