#!/usr/bin/env python3
"""Verify the NaturalHarmony v2 web UI end-to-end.

This harness is intentionally boring and repeatable:
1. build the TypeScript/Vite UI into nh_ui/static;
2. run the Playwright browser E2E tests for the v2 UI;
3. run the full Python package suite.

Hardware-dependent audio/MIDI E2E tests stay opt-in via NH_RUN_HARDWARE_E2E=1.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "packages" / "nh-ui" / "web"
PYTHON = ROOT / ".venv" / "bin" / "python"


def run(label: str, command: list[str], cwd: Path = ROOT) -> None:
    print(f"\n==> {label}")
    print("$", " ".join(command))
    completed = subprocess.run(command, cwd=cwd, text=True)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run pytest packages/ after the UI-specific checks.",
    )
    parser.add_argument(
        "--hardware",
        action="store_true",
        help="Also run hardware-dependent tests by setting NH_RUN_HARDWARE_E2E=1.",
    )
    args = parser.parse_args()

    if not PYTHON.exists():
        print(f"Missing virtualenv Python: {PYTHON}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    if args.hardware:
        env["NH_RUN_HARDWARE_E2E"] = "1"

    run("Build v2 web UI", ["npm", "run", "build"], cwd=WEB_DIR)
    run(
        "Run v2 browser E2E tests",
        [str(PYTHON), "-m", "pytest", "packages/nh-ui/tests/test_e2e.py", "-q"],
    )
    run(
        "Run scene API tests",
        [str(PYTHON), "-m", "pytest", "packages/nh-ui/tests/test_scene_api.py", "-q"],
    )
    if args.full:
        command = [str(PYTHON), "-m", "pytest", "packages/", "-q"]
        print("\n==> Run full package suite")
        print("$", " ".join(command))
        completed = subprocess.run(command, cwd=ROOT, text=True, env=env)
        if completed.returncode != 0:
            return completed.returncode

    print("\nOK: NaturalHarmony v2 UI verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
