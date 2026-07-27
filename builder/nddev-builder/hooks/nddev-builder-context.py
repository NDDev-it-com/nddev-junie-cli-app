#!/usr/bin/env python3
"""Bounded Junie hook adapter that adds NDDev target context before tool use."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_STDIN_BYTES = 65536
SUPPORTED_TOOLS = {"Bash", "Write", "Edit", "Read", "Grep", "Glob"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    return parser.parse_args()


def read_payload() -> dict[str, object] | None:
    data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(data) > MAX_STDIN_BYTES:
        return None
    if not data.strip():
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    payload = read_payload()
    if payload is None:
        return 0
    if payload.get("hook_event_name") != "PreToolUse":
        return 0
    tool_name = payload.get("tool_name")
    if tool_name not in SUPPORTED_TOOLS:
        return 0
    context = (
        "NDDev Junie target context:\n"
        f"- Control/project root: {target}\n"
        f"- Isolated Junie Home: {target / '.nddev-junie-cli-runtime' / 'home'}\n"
        "- Use target-owned config, guidelines, skills, agents, MCP, commands, hooks, "
        "and extension source files passed by the manager.\n"
        "- Do not read or write live account ~/.junie, provider credentials, auth files, "
        "logs, or unmanaged extension references."
    )
    print(json.dumps({"additionalContext": context}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
