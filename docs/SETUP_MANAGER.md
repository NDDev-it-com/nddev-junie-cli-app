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
- `.nddev-junie-cli-runtime/home/.local/bin/junie`
- `.nddev-junie-cli-runtime/home/.local/share/junie/versions/<version>/`
- `NDDEV-JUNIE-CLI-SETUP.json`

Unknown JSON keys in `config.json` and `allowlist.json` are preserved. Existing
content outside the NDDev managed block in `AGENTS.md` is preserved. Third-party
skills, agents, MCP files, auth files, logs, and session state are not removed.

## Software Lifecycle

`install` verifies the pinned official `install.sh` digest, verifies the official
`update-info.jsonl` metadata for the pinned release, and runs the installer only in
an isolated staging `HOME` with `JUNIE_VERSION` set. A stage-local `junie --version`
probe must report the pinned version before the runtime is moved under the target.

Healthy target-owned software is not reinstalled by `install`. Use `update` to
repair a safe partial runtime. Unsafe target path types, symlinks, hardlinks, and
non-private managed target directories fail before network access.

## Launch Isolation

`launch` checks the stamp and refuses drift before spawning the target-owned Junie
shim. The child process
gets an isolated `HOME`, `JUNIE_DATA`, config, skill, agent, MCP, extension, guidelines,
and cache scope under the managed target. Provider credential variables are stripped.

`--skip-update-check` is added unless the caller already supplied it.
