# nddev-junie-cli-app Agent Instructions

This public module owns the reusable Junie CLI setup manager, public contract,
baseline metadata, setup catalog, and nddev-builder toolkit.

## Boundaries

- Keep private tests, fixtures, benchmarks, root memories, and harness operational
  skills out of this repository.
- Do not write live `~/.junie`, provider credentials, auth files, logs, caches, or
  installed extension references.
- Treat `cli-tools/nddev_junie_cli.py`, `config/nddev-contract.json`, and
  `references/junie-cli-baseline.json` as the source of truth for managed paths,
  launch scope, and release pins.

## Development

- Use Conventional Commits.
- Keep public documentation in English.
- Prefer bounded I/O, regular files, target-bound backups, locks, rollback, and
  fail-closed behavior.
- Run `python3 cli-tools/validate_public_contracts.py` after public contract or
  builder toolkit changes.
