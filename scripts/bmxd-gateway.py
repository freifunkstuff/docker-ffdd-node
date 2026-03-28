#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import fcntl


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def log_event(message: str) -> None:
    print(f"{timestamp()} [bmxd] {message}", file=sys.stderr, flush=True)


def parse_counts(content: str) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        name, count_text = line.split(":", 1)
        try:
            count = int(count_text.strip())
        except ValueError:
            continue
        result.append((name.strip(), count))
    return result


def serialize_counts(lines: list[tuple[str, int]]) -> str:
    if not lines:
        return ""
    return "".join(f"{name}:{count}\n" for name, count in lines)


def increment_usage(stat_file: Path, action: str) -> None:
    stat_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = stat_file.with_suffix(stat_file.suffix + ".lock")
    with lock_file.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            content = stat_file.read_text(encoding="utf-8") if stat_file.exists() else ""
            counts = parse_counts(content)

            updated = False
            for idx, (name, count) in enumerate(counts):
                if name == action:
                    counts[idx] = (name, count + 1)
                    updated = True
                    break

            if not updated:
                counts.append((action, 1))

            rendered = serialize_counts(counts)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(stat_file.parent), delete=False
            ) as handle:
                handle.write(rendered)
                temp_name = handle.name
            os.replace(temp_name, stat_file)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def action_message(action: str) -> str:
    if action == "init":
        return "Initializing bmxd gateway handler"
    if action == "gateway":
        return "Entering community gateway mode"
    if action == "del":
        return "Clearing selected gateway state"
    if action == "":
        return "Called without arguments"
    return f"Selected gateway node {action}"


def main() -> int:
    action = os.environ.get("BMXD_GATEWAY_ACTION")
    if action is None:
        action = sys.argv[1] if len(sys.argv) > 1 else ""

    stat_path = Path(os.environ.get("BMXD_GATEWAY_USAGE_FILE", "/data/statistic/gateway_usage"))

    log_event(action_message(action))
    if action:
        increment_usage(stat_path, action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
