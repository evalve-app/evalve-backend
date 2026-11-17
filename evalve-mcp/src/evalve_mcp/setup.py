"""The setup command: write the `evalve` entry in the `.mcp.json` of the current directory."""

import json
from pathlib import Path


class SetupError(Exception):
    """A message for the Teammate. The command changed nothing."""


def write_entry(server_url: str) -> Path:
    """Set the `evalve` entry and keep each other entry. Teams commit this file, so the entry has no key."""
    path = Path.cwd() / ".mcp.json"
    config: dict = {}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise SetupError(f"{path} is not valid JSON. Correct the file, then run the command again.") from None
        if not isinstance(config, dict) or not isinstance(config.get("mcpServers", {}), dict):
            raise SetupError(f"{path} is not an MCP config: it must be a JSON object with an `mcpServers` object.")
    config.setdefault("mcpServers", {})["evalve"] = {
        "command": "uv", "args": ["run", "evalve-mcp", "--server", server_url],
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path
