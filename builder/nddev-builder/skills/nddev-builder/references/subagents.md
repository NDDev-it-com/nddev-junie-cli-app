# Subagent Workflow

Junie custom subagents are Markdown files with YAML frontmatter and a prompt body.

Creator workflow:

1. Use a lowercase hyphenated name.
2. Write a concrete description so Junie can delegate accurately.
3. Add tool restrictions only when they are part of the artifact's role.
4. Avoid model-specific assumptions unless the target config owns that choice.

Checker workflow:

1. Verify `description` is present.
2. Verify tool groups are documented Junie tool groups.
3. Verify the prompt body has a bounded role and does not claim unsupported
   marketplaces, auth access, or live state access.
