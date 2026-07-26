# Setup Manager

`nddev-junie-cli-app` manages an explicit Junie CLI target rather than live
`~/.junie` state.

## Managed Files

- `config.json`
- `allowlist.json`
- `AGENTS.md`
- `skills/nddev-builder/SKILL.md`
- `agents/nddev-builder.md`
- `mcp/mcp.json`
- `NDDEV-JUNIE-CLI-SETUP.json`

Unknown JSON keys in `config.json` and `allowlist.json` are preserved. Existing
content outside the NDDev managed block in `AGENTS.md` is preserved. Third-party
skills, agents, MCP files, auth files, logs, and session state are not removed.

## Launch Isolation

`launch` checks the stamp and refuses drift before spawning `junie`. The child process
gets an isolated `HOME`, `JUNIE_DATA`, config, skill, agent, MCP, extension, guidelines,
and cache scope under the managed target. Provider credential variables are stripped.

`--skip-update-check` is added unless the caller already supplied it.
