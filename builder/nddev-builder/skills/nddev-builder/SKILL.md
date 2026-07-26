---
name: nddev-builder
description: Build and review native Junie CLI setup artifacts inside an isolated NDDev target.
---

# NDDev Builder

Use this skill when creating or reviewing Junie CLI setup artifacts managed by
`nddev-junie-cli-app`.

Work only through confirmed Junie CLI surfaces: guidelines selected by
`JUNIE_GUIDELINES_FILENAME`, skill directories selected by `JUNIE_SKILL_LOCATIONS`,
agent directories selected by `JUNIE_AGENT_LOCATIONS`, and MCP configuration selected
by `JUNIE_MCP_LOCATIONS`. Treat extension marketplace projection as unavailable until
a published NDDev marketplace manifest is confirmed.

Keep provider credentials, user auth state, and live Junie session state outside the
managed target. Prefer explicit absolute paths, bounded reads, and reversible changes.
