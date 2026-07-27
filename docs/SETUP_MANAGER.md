# Setup Manager

`nddev-junie-cli-app` manages an explicit Junie CLI target rather than live
`~/.junie` state. The active content setup is `nddev-builder`; permission is an
orthogonal profile selected as `safe` or `full-auto`.

## Managed Surface

The exact managed file set, projection rules, stamp shape, backup layout,
runtime software layout, and lock artifacts are owned by
`cli-tools/nddev_junie_cli.py` and summarized for consumers by
`config/nddev-contract.json`. This document intentionally does not repeat those
lists.

At the lifecycle level, current targets record the active content setup and the
orthogonal permission profile. Legacy coupled identities remain readable for
status, migration, restore, and removal, but cannot be launched or switched until
they are migrated. The manager packages local extension marketplace source files
where the native runtime can discover explicit sources, and it does not fabricate
installed extension state that Junie does not document as a stable
noninteractive surface.

## Software Lifecycle

`install` and `update` use the official pinned Junie CLI sources declared by
`references/junie-cli-baseline.json` and the public contract. The implementation
details for installer verification, staging, probing, timeout behavior, and
rollback are owned by `cli-tools/nddev_junie_cli.py`.

Public commands do not honor env-based source, fixture, artificial-failure, or
timeout switches. Healthy target-owned software is left in place by `install`;
`update` is the explicit repair path for supported partial runtime state. Unsafe
targets fail closed before external installer work begins.

## Launch Isolation

`launch` validates the managed target, refuses drift, keeps lifecycle ownership
through child completion and post-launch live-state checks, and denies
concurrent lifecycle mutations while the managed Junie child is running. The
runtime handoff is verified by the manager before execution, but it is not a
same-user sandbox.

The exact lock topology, path derivation, file modes, acquisition and release
rules, executable handoff, subprocess construction, provider-secret filtering,
scope flags, environment bindings, and live-home guard are owned by
`cli-tools/nddev_junie_cli.py` and `config/nddev-contract.json`.
