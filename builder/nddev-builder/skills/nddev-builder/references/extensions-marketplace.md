# Extensions And Marketplace Workflow

Junie supports local marketplace sources with a native
`.junie-extension/marketplace.json` manifest. The manager packages a local source
tree but does not fabricate installed extension state.

Creator workflow:

1. Put the marketplace manifest at `.junie-extension/marketplace.json`.
2. Point each extension entry to a local source directory.
3. Include an `extension.json` in each extension source.
4. Package skills, agents, commands, guidelines, and MCP only as regular files.

Checker workflow:

1. Verify the marketplace source is a local regular-file tree.
2. Verify installed extension reference files are not generated unless Junie
   documents a noninteractive install path.
3. Verify extension cache location is target-owned and passed by the manager.
