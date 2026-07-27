# Guidelines And Memory Workflow

Junie reads guidelines from AGENTS-style files and can also discover legacy
guidelines. NDDev uses one deterministic managed guidance file passed explicitly
by the manager.

Creator workflow:

1. Keep guidance concise and hierarchical.
2. Point to code-owned facts instead of copying version pins or launch tables.
3. Separate public user-facing guidance from private harness memory.

Checker workflow:

1. Verify the managed guidance file does not mention live user state.
2. Verify it does not duplicate volatile values owned by baseline, contract, or
   manager code.
3. Verify private operational memory is not copied into the public module.
