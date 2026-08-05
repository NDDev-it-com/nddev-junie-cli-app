# Changelog

## [0.2.1] - 2026-08-05

- Update the supported Junie CLI release to `2548.5` (`26.8.3`) with exact
  official artifact URLs, sizes, and SHA-256 identities for macOS and Ubuntu
  on arm64 and x64.

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
