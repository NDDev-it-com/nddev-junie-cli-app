# Validation Workflow

Public module validation must be non-live and module-local. Private harness tests
live outside this public repository.

Creator workflow:

1. Update the public validator when public contract fields or required files
   change.
2. Keep platform pins in the baseline file and manager checks.
3. Keep private fixtures, benchmarks, and broad lifecycle tests out of this module.

Checker workflow:

1. Run the public validator.
2. Run manager parse/compile checks.
3. Run non-live list/plan/status checks with temporary targets only.
4. Do not start Junie, install software, push, tag, or invoke CI from public
   validation.
