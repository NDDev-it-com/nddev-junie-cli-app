# Hooks Workflow

Junie hooks are configured in `config.json` and run shell commands at documented
session/tool lifecycle points. NDDev uses only hooks with proven output semantics.

Creator workflow:

1. Prefer `PreToolUse` only when the hook can add deterministic context without
   changing permission decisions.
2. Use bounded JSON stdin/stdout.
3. Do not write logs, read secrets, call networks, or inspect live account state.
4. Do not emit `decision`, `permissionDecision`, or `continue` unless the setup
   contract explicitly changes the permission model.

Checker workflow:

1. Verify every hook command points under the managed target.
2. Verify timeout is positive and small for context hooks.
3. Verify failures cannot approve sensitive actions or weaken full-auto.
4. Verify the hook script handles invalid or oversized stdin safely.
