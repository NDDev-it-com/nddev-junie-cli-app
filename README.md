# nddev-junie-cli-app

Public setup manager for current JetBrains Junie CLI stable channel.

The manager never installs or launches against live user state by default. Every
operation requires an explicit absolute target and writes only target-bound managed
files with a stamp, rollback snapshot, target-internal lock, and target-internal
rotating backups.

`--target` is the managed control and project root. The launched Junie Home is an
isolated runtime directory below that target. Explicit config, guidelines, skills,
agents, commands, MCP, hooks, and extension source files remain deterministic
regular files under the control root and are passed to Junie by exact flags and
environment variables. The action allowlist is pathless native Junie state and is
materialized under the isolated runtime home.

## Commands

```bash
python3 cli-tools/nddev_junie_cli.py list --json
python3 cli-tools/nddev_junie_cli.py plan --setup nddev-builder --profile full-auto --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py install --setup nddev-builder --profile full-auto --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py update --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py switch --setup nddev-builder --profile safe --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py migrate --setup nddev-builder --profile full-auto --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py status --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py restore --backup 0 --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py remove --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py launch --target /absolute/target -- --version
```

## Junie CLI Baseline

The public contract is pinned to the official stable `release` channel and command
name `junie`. Official sources are recorded in
`references/junie-cli-baseline.json`.

The manager verifies the official installer SHA256 and exact `update-info.jsonl`
artifact metadata before running `install.sh` in an isolated staging `HOME` with
`JUNIE_VERSION` set to the pinned release. Public commands do not accept env-based
source, fixture, or timeout overrides. Only the target-owned runtime under
`.nddev-junie-cli-runtime/` is persisted. Runtime probes and launches use a fixed
minimal subprocess `PATH` and bind `JUNIE_DATA`, `JUNIE_LOG_DIR`, official
default-location controls, cache/temp paths, and JVM `user.home` to target-owned
or stage-owned directories; the manager fails closed if the account `~/.junie`
metadata changes. Managed lifecycle operations acquire a persistent external
bootstrap flock first, under the resolved fixed system temp root
(`/private/tmp` on macOS, `/tmp` on Linux) and keyed by the full SHA256 of the
product namespace plus canonical absolute target. The persistent target-internal
lock is acquired second and released first; the external lock is released last
and is never exposed to the child environment. Managed launch holds both locks
through child completion and post-launch live-home validation, so lifecycle
mutations are denied while the launched Junie process is running even if the
target-local lock directory is renamed from the writable target root. The
target-owned shim and pinned Junie binary identity are captured during launch
preflight and revalidated immediately before child execution. Because macOS does
not provide a portable `fexecve` or `/dev/fd` execution path for this handoff,
the manager retains open verified file descriptors as evidence, materializes a
dedicated launch image at `.nddev-junie-cli-runtime/launch-image/junie`,
write-protects only that dedicated launcher directory through child completion,
and starts the launch image path with `Popen`. Runtime `HOME`, `TMP`, XDG, data,
log, project, and config/source directories remain writable for the launched
CLI. This blocks ordinary target-local lock and launcher replacement during
launch, but it is not a sandbox against deliberate same-UID bootstrap-root or
ancestor tampering.

## Setups

`nddev-builder` is the only active content setup. `full-auto` is the default
permission profile and enables Junie Brave mode. `safe` disables Brave mode and
uses an ask-first empty allowlist. Profile switches do not duplicate or replace
the shared builder toolkit. No unproven Auto-style profile is shipped.

## Builder Toolkit

The managed `nddev-builder` toolkit is projected directly through Junie-native
guidelines, Agent Skills, custom subagents, custom commands, MCP, and hooks. The
manager also packages a local native Junie extension marketplace source under the
target, but it does not fabricate installed extension state because Junie does not
document a stable noninteractive local marketplace installation command.
