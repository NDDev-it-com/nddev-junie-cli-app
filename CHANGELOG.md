# Changelog

## [0.2.0]

- Provide a target-explicit Junie CLI setup manager with transactional setup
  lifecycle, target-bound locks and backups, and isolated runtime state.
- Ship one `nddev-builder` content setup with orthogonal `full-auto` and `safe`
  permission profiles and Junie-native builder projections.
- Pin the official stable Junie CLI release `2470.4` and support macOS and Ubuntu
  glibc hosts on arm64 and x64.
- Manage target-owned Junie CLI software through explicit status, install,
  update, and removal commands while rejecting unsupported hosts before network
  or target mutation.
