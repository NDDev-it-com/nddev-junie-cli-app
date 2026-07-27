---
name: junie-hook-checker
description: Check Junie hook configs and adapters for bounded I/O and permission safety.
---

# Junie Hook Checker

Use this skill when reviewing hooks.

Read `../nddev-builder/references/hooks.md` first.

Verify target-owned commands, bounded JSON I/O, no logs/secrets/network, and no
unintended allow/ask/block decisions.
