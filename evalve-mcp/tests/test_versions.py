"""One version number: the backend package, its `__version__`, and the evalve-mcp package."""

import re
import tomllib
import unittest
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def project_version(path: Path) -> str:
    return tomllib.loads(path.read_text())["project"]["version"]


class VersionsTest(unittest.TestCase):
    def test_the_three_version_values_are_the_same(self):
        source = (ROOT / "src" / "evalve_backend" / "__init__.py").read_text()
        values = {
            "pyproject.toml": project_version(ROOT / "pyproject.toml"),
            "src/evalve_backend/__init__.py": re.search(r'^__version__ = "([^"]+)"$', source, re.M).group(1),
            "evalve-mcp/pyproject.toml": project_version(ROOT / "evalve-mcp" / "pyproject.toml"),
        }
        self.assertEqual(len(set(values.values())), 1, f"The version values differ: {values}")

    def test_the_installed_package_reports_the_version_of_its_pyproject(self):
        self.assertEqual(version("evalve-mcp"), project_version(ROOT / "evalve-mcp" / "pyproject.toml"))


if __name__ == "__main__":
    unittest.main()
