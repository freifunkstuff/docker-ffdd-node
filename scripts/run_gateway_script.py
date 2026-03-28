#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _read_shebang(script_path: Path) -> str:
    try:
        first_line = script_path.read_text(encoding="utf-8").splitlines()[0]
    except (FileNotFoundError, IndexError):
        return ""
    return first_line


def _build_command(script_path: Path, args: list[str]) -> list[str]:
    shebang = _read_shebang(script_path)
    if "python" in shebang:
        return [sys.executable, str(script_path), *args]
    if "sh" in shebang or "bash" in shebang:
        return ["sh", str(script_path), *args]
    return [str(script_path), *args]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("script_path", help="Path to gateway script under test")
    parser.add_argument("script_args", nargs="*", help="Arguments passed to the target script")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    script_path = Path(args.script_path)
    command = _build_command(script_path, list(args.script_args))
    result = subprocess.run(command, check=False)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())