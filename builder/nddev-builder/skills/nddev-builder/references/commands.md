# Custom Command Workflow

Junie custom slash commands are Markdown files with YAML frontmatter. The file
name is the command name.

Creator workflow:

1. Use a short command name.
2. Add a `description` in YAML frontmatter.
3. Use named arguments such as `$path` only when required by the prompt.
4. Keep the command a prompt template, not executable code.

Checker workflow:

1. Confirm the command file is Markdown.
2. Confirm all template variables are intentionally required.
3. Confirm the command does not expose secrets, logs, auth files, or unmanaged
   paths.
