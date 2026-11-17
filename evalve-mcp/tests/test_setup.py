"""The setup command writes only the `evalve` entry in the `.mcp.json` of the current directory."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from evalve_mcp.server import main

URL = "https://evalve.example.test/evalve"
KITARU = {"command": "uvx", "args": ["kitaru-mcp"], "env": {"KITARU_SERVER_URL": "https://kitaru.example.test"}}


def entry(url: str) -> dict:
    return {"command": "uv", "args": ["run", "evalve-mcp", "--server", url]}


class SetupTest(unittest.TestCase):
    def setUp(self):
        home = os.getcwd()
        project = tempfile.TemporaryDirectory()
        self.addCleanup(project.cleanup)
        self.addCleanup(os.chdir, home)
        os.chdir(project.name)
        self.file = Path(project.name, ".mcp.json")

    def setup_command(self, url: str = URL) -> str:
        """Run the command. Return the error text, or an empty string."""
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            try:
                main(["setup", "--server", url])
            except SystemExit as stop:
                return str(stop.code) + errors.getvalue()
        return ""

    def test_it_creates_the_file_when_the_file_is_missing(self):
        self.assertEqual(self.setup_command(), "")
        self.assertEqual(json.loads(self.file.read_text()), {"mcpServers": {"evalve": entry(URL)}})

    def test_the_entry_has_the_command_and_the_server_url_only(self):
        self.setup_command()
        self.assertEqual(set(json.loads(self.file.read_text())["mcpServers"]["evalve"]), {"command", "args"})

    def test_an_existing_kitaru_entry_and_each_other_key_stay_as_they_are(self):
        before = {"mcpServers": {"kitaru": KITARU}, "other": {"keep": True}}
        self.file.write_text(json.dumps(before))
        self.assertEqual(self.setup_command(), "")
        self.assertEqual(
            json.loads(self.file.read_text()), {"mcpServers": {"kitaru": KITARU, "evalve": entry(URL)}, "other": {"keep": True}},
        )

    def test_a_second_command_with_a_different_url_replaces_only_the_evalve_entry(self):
        self.file.write_text(json.dumps({"mcpServers": {"evalve": entry(URL) | {"env": {"OLD": "1"}}, "kitaru": KITARU}}))
        new = "https://new.example.test/evalve"
        self.assertEqual(self.setup_command(new), "")
        self.assertEqual(json.loads(self.file.read_text()), {"mcpServers": {"evalve": entry(new), "kitaru": KITARU}})

    def test_it_stops_and_changes_nothing_when_the_file_is_not_a_valid_config(self):
        for content in ('{"mcpServers": ', "[]", '{"mcpServers": []}'):
            with self.subTest(content=content):
                self.file.write_text(content)
                self.assertIn(".mcp.json", self.setup_command())
                self.assertEqual(self.file.read_text(), content)

    def test_it_does_not_search_the_parent_directories(self):
        parent_file = self.file
        parent_file.write_text(json.dumps({"mcpServers": {"kitaru": KITARU}}))
        child = self.file.parent / "agent"
        child.mkdir()
        os.chdir(child)
        self.assertEqual(self.setup_command(), "")
        self.assertEqual(json.loads((child / ".mcp.json").read_text()), {"mcpServers": {"evalve": entry(URL)}})
        self.assertEqual(json.loads(parent_file.read_text()), {"mcpServers": {"kitaru": KITARU}})

    def test_the_server_url_must_be_https_unless_the_host_is_localhost(self):
        for url in ("http://evalve.example.test/evalve", "ftp://evalve.example.test", "evalve.example.test", "https://"):
            with self.subTest(url=url):
                self.assertIn("--server", self.setup_command(url))
                self.assertFalse(self.file.exists())
        for url in ("http://localhost:8100/evalve", "http://127.0.0.1:8100/evalve", "http://[::1]:8100/evalve"):
            with self.subTest(url=url):
                self.assertEqual(self.setup_command(url), "")
                self.assertEqual(json.loads(self.file.read_text())["mcpServers"]["evalve"], entry(url))

    def test_the_server_command_refuses_an_unsafe_url_too(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            main(["--server", "http://evalve.example.test/evalve"])
        self.assertIn("HTTPS", str(stop.exception.code))


if __name__ == "__main__":
    unittest.main()
