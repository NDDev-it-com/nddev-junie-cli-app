# Config Workflow

The managed `config.json` is a complete manager-owned JSON object. It is not a
merge target and must not preserve arbitrary keys from previous user state.

Creator workflow:

1. Add only fields documented by Junie CLI.
2. Keep discovery paths explicit and target-owned.
3. Keep update behavior out of the managed config. The manager passes
   `--skip-update-check` at launch.
4. Keep guidelines paths out of the managed config. The manager passes
   `--guidelines-filename` and `JUNIE_GUIDELINES_FILENAME`.
5. Do not place provider API keys, proxies, auth material, or personal settings in
   the managed config.

Checker workflow:

1. Verify JSON is an object.
2. Verify every path points under the managed target.
3. Verify hooks are explicit, bounded, and do not contain secrets.
4. Verify unknown keys are either rejected by the manager or deliberately added to
   the public contract first.
