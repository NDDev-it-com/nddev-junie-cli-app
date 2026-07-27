# nddev-junie-cli-app

Public setup manager for current JetBrains Junie CLI stable channel.

The manager never installs or launches against live user state by default. Every
operation requires an explicit absolute target and writes only target-bound managed
files with a stamp, rollback snapshot, and rotating backups.

## Commands

```bash
python3 cli-tools/nddev_junie_cli.py list --json
python3 cli-tools/nddev_junie_cli.py plan --setup safe --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py install --setup safe --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py update --target /absolute/target --json
python3 cli-tools/nddev_junie_cli.py switch --setup balanced --target /absolute/target --json
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
`JUNIE_VERSION` set to the pinned release. Only the target-owned runtime under
`.nddev-junie-cli-runtime/` is persisted. Runtime probes and launches also bind
`JUNIE_DATA`, `JUNIE_LOG_DIR`, official default-location controls, cache/temp
paths, and JVM `user.home` to target-owned or stage-owned directories; the
manager fails closed if the account `~/.junie` metadata changes.
