# Native Path Model

`nddev-junie-cli-app` separates the explicit manager target from Junie Home.

- `T` is the canonical `--target`. It is the project/control root used as the
  launched Junie project.
- `H` is the isolated runtime home under the target-owned runtime directory.
- Only pathless Junie state is materialized below `H`.

The manager owns exact launch flags and environment variables. Do not duplicate
the full list in skills; check `cli-tools/nddev_junie_cli.py` before changing a
path-sensitive artifact.

Creator/checker work must keep deterministic source files under `T` and must not
write live account `~/.junie` state.
