# nddev-junie-cli-app

Public setup manager for current JetBrains Junie CLI stable channel.

The manager never installs or launches against live user state by default. Every
operation requires an explicit absolute target and writes only target-bound managed
files with a stamp, rollback snapshot, target-internal lock, and target-internal
rotating backups.

`--target` is the managed control and project root. The launched Junie Home is an
isolated runtime directory below that target. Scope ownership and projection
details are defined by `cli-tools/nddev_junie_cli.py` and summarized by
`config/nddev-contract.json` so this README does not duplicate managed-path
lists.

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
python3 cli-tools/nddev_junie_cli.py software-status --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py install-cli --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py update-cli --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py remove-cli --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py launch --target /absolute/target -- --version
```

`plan` is side-effect-free and reports the stable `changed` managed-path set
that the corresponding `install`, `switch`, `update`, or `migrate` mutation
would write or remove.

Setup commands manage projected Junie configuration, builder content, stamps,
locks, and backups. The target-owned Junie CLI runtime is managed only through
the `*-cli` software commands, so setup `install` never substitutes for software
installation.

## Junie CLI Baseline

The public contract is pinned to the official stable `release` channel and command
name `junie`. Official sources are recorded in
`references/junie-cli-baseline.json`.

Install, update, and launch behavior is intentionally code-owned. See
`cli-tools/nddev_junie_cli.py` for the executable lifecycle implementation,
`config/nddev-contract.json` for the public safety and runtime contract,
`references/junie-cli-baseline.json` for official source pins, and `AGENTS.md`
for the repository source-of-truth rule.

At the public interface level, lifecycle commands use pinned official sources,
isolate Junie from live account state, reject unsafe target state, and fail
closed rather than silently repairing unmanaged drift. Managed launch keeps the
target lifecycle owned until the child exits and post-launch state checks finish,
so concurrent lifecycle mutations are denied while Junie is running. The exact
managed paths, installer probe details, lock implementation, launch handoff,
scope flags, environment bindings, and same-user tamper boundary are defined by
the code-owned sources above.

## Supported Platforms

NDDev supports Junie CLI on `macos-arm64`, `macos-x64`,
`ubuntu-glibc-arm64`, and `ubuntu-glibc-x64`. Official JetBrains artifact keys
and URLs remain vendor-owned observations. The pinned update-info release also
contains official Windows artifacts, which NDDev records as official but
unsupported; product support remains exactly the four macOS and Ubuntu host ids
above.

Linux distribution detection uses structured OS metadata and accepts Ubuntu
Desktop or Server through the same `ID=ubuntu` plus glibc host check. JetBrains
does not publish an Ubuntu release or glibc version floor for the pinned
`linux-*` artifacts, so the contract stores that floor as `null` and
`no-official-floor`. `non-ubuntu-linux`, `linux-musl`, `windows`, and
`unsupported-architecture` hosts fail closed before installer network access,
staging, or runtime mutation.

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
