---
name: nddev-builder
description: Route Junie CLI setup artifact work to the NDDev creator/checker skills and verified native-surface references.
---

# NDDev Builder Router

Use this skill when a task involves Junie CLI setup artifacts managed by
`nddev-junie-cli-app`: config, allowlist, guidelines, skills, subagents, custom
commands, MCP, hooks, local extensions, launch isolation, migration, or
validation.

## First Checks

1. Identify whether the task is creating an artifact, checking an artifact, or
   explaining the managed target model.
2. Read `references/native-path-model.md` before touching any path.
3. Read only the focused reference for the artifact family being changed.
4. Delegate to the matching creator/checker skill when the task is concrete.

## Artifact Routing

- Config and launch scope: `junie-config-creator` or `junie-config-checker`; read
  `references/config.md` and `references/native-path-model.md`.
- Action allowlist: read `references/allowlist.md`; it is materialized only by the
  manager.
- Guidelines and memory: `junie-instructions-creator` or
  `junie-instructions-checker`; read `references/guidelines-memory.md`.
- Agent Skills: `junie-skill-creator` or `junie-skill-checker`; read
  `references/skills.md`.
- Custom subagents: `junie-agent-creator` or `junie-agent-checker`; read
  `references/subagents.md`.
- Custom slash commands: `junie-command-creator` or `junie-command-checker`; read
  `references/commands.md`.
- MCP: `junie-mcp-creator` or `junie-mcp-checker`; read `references/mcp.md`.
- Hooks: `junie-hook-creator` or `junie-hook-checker`; read `references/hooks.md`.
- Local extensions/marketplaces: `junie-extension-creator` or
  `junie-extension-checker`; read `references/extensions-marketplace.md`.
- Validation and migration: read `references/validation.md`.

## Rules

- Treat the explicit manager target as the project/control root, not Junie Home.
- Do not copy release pins, artifact hashes, platform lists, or launch flag tables
  into new artifacts. Point to the manager, contract, and baseline that own those
  facts.
- Do not read live account Junie state, provider credentials, auth files, logs, or
  unmanaged extension references.
- Prefer deterministic regular files, bounded JSON/Markdown, and reversible changes.
