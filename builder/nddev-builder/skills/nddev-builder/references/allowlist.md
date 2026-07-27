# Allowlist Workflow

Junie CLI stores the action allowlist at the active Junie Home path. In this
manager, that means the isolated runtime home, not the control root.

Creator workflow:

1. Define the setup permission posture in `setups/<id>/setup.json`.
2. Use `defaultBehavior: "ask"` and empty rules for ask-first safe behavior.
3. Do not add broad allow rules unless the setup contract explicitly requires them.

Checker workflow:

1. Confirm safe uses Brave Off and ask-first empty allow rules.
2. Confirm full-auto relies on Brave On rather than fabricated allowlist rules.
3. Confirm the manager materializes the allowlist transactionally into runtime
   Junie Home and includes it in drift/backup/restore.
