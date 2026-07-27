# Agent Skills Workflow

Junie Agent Skills are directories with a required `SKILL.md` file and optional
supporting references, scripts, templates, or checklists.

Creator workflow:

1. Give each skill a narrow routing description.
2. Put broad routing in `nddev-builder`; put focused artifact rules in references
   or artifact-specific skills.
3. Use progressive disclosure: short entry points, focused references, no large
   copied fact tables.

Checker workflow:

1. Confirm `SKILL.md` has valid frontmatter with `name`.
2. Confirm relative references exist.
3. Confirm the skill does not ask Junie to read secrets or live account state.
