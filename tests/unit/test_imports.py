"""Keep the orchestration core usable without Home Assistant installed."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_core_import_does_not_load_homeassistant() -> None:
    """Import the integration in a fresh interpreter without touching HA."""
    script = """
import importlib.abc
import sys

class BlockHomeAssistant(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'homeassistant' or fullname.startswith('homeassistant.'):
            raise AssertionError(f'Unexpected Home Assistant import: {fullname}')
        return None

sys.meta_path.insert(0, BlockHomeAssistant())
import custom_components.zigbee_led_orchestrator
assert not any(name == 'homeassistant' or name.startswith('homeassistant.') for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True)


def test_core_suite_has_no_ha_plugin_dependency() -> None:
    """Run the real core suite in a clean process without third-party autoload."""
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(ROOT), env.get("PYTHONPATH")))
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "asyncio",
            "-q",
            "tests/unit",
            "-k",
            "not test_core_suite_has_no_ha_plugin_dependency",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
