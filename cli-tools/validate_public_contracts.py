#!/usr/bin/env python3
"""Validate public nddev-junie-cli-app release contracts."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
SHARED_CI_COMMIT = "2ccb80e96f5771b6a6b4eae63a4f47e232906dc7"
SHARED_CI_VERSION = "0.12.0"
SETUP_ORDER = ["nddev-builder"]
PROFILE_ORDER = ["safe", "full-auto"]
DEFAULT_SETUP_ID = "nddev-builder"
DEFAULT_PROFILE_ID = "full-auto"
SUPPORTED_PLATFORMS = {"linux-aarch64", "linux-amd64", "macos-aarch64", "macos-amd64"}
REQUIRED_WORKFLOWS = {
    "actionlint.yml": ".github/workflows/actionlint.yml",
    "codeql.yml": ".github/workflows/public-codeql.yml",
    "dependency-review.yml": ".github/workflows/public-dependency-review.yml",
    "release.yml": ".github/workflows/release-supply-chain.yml",
    "scorecard.yml": ".github/workflows/public-scorecard-json.yml",
    "secret-scan.yml": ".github/workflows/secret-scan.yml",
    "zizmor.yml": ".github/workflows/zizmor-sarif.yml",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    return parser.parse_args(argv)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_version() -> str:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not version:
        raise ValueError("VERSION owner must not be empty")
    return version


def load_manager() -> Any:
    spec = importlib.util.spec_from_file_location("nddev_junie_cli_public", ROOT / "cli-tools" / "nddev_junie_cli.py")
    if spec is None or spec.loader is None:
        raise ValueError("cannot load manager module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def setup_ids() -> list[str]:
    ids: list[str] = []
    for setup_id in SETUP_ORDER:
        setup_json = ROOT / "setups" / setup_id / "setup.json"
        setup = load_json(setup_json)
        ids.append(str(setup["id"]))
        if setup_json.parent.name != setup["id"]:
            raise ValueError(f"{setup_json}: directory name and id differ")
        if setup.get("nddev_builder_default") is not True:
            raise ValueError(f"{setup_json}: nddev-builder must be default-on")
    extra_setup_dirs = sorted(
        path.name
        for path in (ROOT / "setups").iterdir()
        if path.is_dir() and path.name not in SETUP_ORDER
    )
    if extra_setup_dirs:
        raise ValueError(f"unsupported setup directories exist: {', '.join(extra_setup_dirs)}")
    return ids


def profile_ids() -> list[str]:
    ids: list[str] = []
    for profile_id in PROFILE_ORDER:
        profile_json = ROOT / "profiles" / profile_id / "profile.json"
        profile = load_json(profile_json)
        ids.append(str(profile["id"]))
        if profile_json.parent.name != profile["id"]:
            raise ValueError(f"{profile_json}: directory name and id differ")
    extra_profile_dirs = sorted(
        path.name
        for path in (ROOT / "profiles").iterdir()
        if path.is_dir() and path.name not in PROFILE_ORDER
    )
    if extra_profile_dirs:
        raise ValueError(f"unsupported profile directories exist: {', '.join(extra_profile_dirs)}")
    return ids


def validate_workflows() -> None:
    workflow_root = ROOT / ".github" / "workflows"
    for filename, workflow in REQUIRED_WORKFLOWS.items():
        path = workflow_root / filename
        if not path.is_file():
            raise ValueError(f"missing workflow {path.relative_to(ROOT)}")
        expected = (
            f"uses: NDDev-it-com/ci-workflows/{workflow}@{SHARED_CI_COMMIT} # {SHARED_CI_VERSION}"
        )
        text = path.read_text(encoding="utf-8")
        if text.count(expected) != 1:
            raise ValueError(f"{filename}: missing exact shared CI caller")


def validate_required_files() -> None:
    required = [
        "AGENTS.md",
        "VERSION",
        "README.md",
        "build/version.json",
        "build/manifest.json",
        "config/nddev-contract.json",
        "references/junie-cli-baseline.json",
        "docs/SETUP_MANAGER.md",
        "cli-tools/nddev_junie_cli.py",
        "builder/nddev-builder/agents/nddev-builder.md",
        "builder/nddev-builder/commands/nddev-builder-check.md",
        "builder/nddev-builder/hooks/nddev-builder-context.py",
        "builder/nddev-builder/skills/nddev-builder/SKILL.md",
        "setups/nddev-builder/setup.json",
        "profiles/safe/profile.json",
        "profiles/full-auto/profile.json",
    ]
    references = ROOT / "builder" / "nddev-builder" / "skills" / "nddev-builder" / "references"
    for reference in (
        "native-path-model.md",
        "config.md",
        "allowlist.md",
        "guidelines-memory.md",
        "skills.md",
        "subagents.md",
        "commands.md",
        "mcp.md",
        "hooks.md",
        "extensions-marketplace.md",
        "validation.md",
    ):
        required.append(str((references / reference).relative_to(ROOT)))
    for relative in required:
        if not (ROOT / relative).is_file():
            raise ValueError(f"missing required public path {relative}")


def validate_baseline(baseline: dict[str, Any], contract: dict[str, Any]) -> None:
    if baseline["release"]["channel"] != "release":
        raise ValueError("baseline channel must be release")
    if baseline["runtime"]["command"] != "junie":
        raise ValueError("baseline command must be junie")
    if set(baseline["release"]["exact_artifacts"]) != SUPPORTED_PLATFORMS:
        raise ValueError("baseline must contain only macOS/Linux artifacts")
    if set(baseline["release"]["exact_artifact_hashes"]) != SUPPORTED_PLATFORMS:
        raise ValueError("baseline exact hashes must contain only macOS/Linux artifacts")
    for platform_id, artifact in baseline["release"]["exact_artifacts"].items():
        hashes = baseline["release"]["exact_artifact_hashes"][platform_id]
        if artifact["sha256"] != hashes["sha256"] or artifact["size"] != hashes["size"]:
            raise ValueError(f"{platform_id}: exact artifact hash metadata differs")
        if not (platform_id.startswith("linux-") or platform_id.startswith("macos-")):
            raise ValueError("unsupported platform artifact is out of scope")
        if not str(artifact["download_url"]).startswith("https://github.com/JetBrains/junie/"):
            raise ValueError(f"{platform_id}: exact artifact URL must be official")
    lifecycle = contract["software_lifecycle"]
    installer = baseline["release"]["installer"]
    if lifecycle["installer_url"] != installer["url"]:
        raise ValueError("contract installer URL differs from baseline")
    if lifecycle["installer_sha256"] != installer["sha256"]:
        raise ValueError("contract installer SHA256 differs from baseline")
    if lifecycle["version"] != baseline["release"]["exact_version"]:
        raise ValueError("contract software version differs from baseline")
    if lifecycle["marketing_version"] != baseline["release"]["marketing_version"]:
        raise ValueError("contract marketing version differs from baseline")


def validate_generated_files(manager: Any, version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-public-") as raw:
        target = Path(raw) / "target"
        target.mkdir(mode=0o700)
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        profile = manager.load_profile(DEFAULT_PROFILE_ID)
        files = manager.desired_files(target, setup, profile)
    required = {
        "config.json",
        "AGENTS.md",
        "skills/nddev-builder/SKILL.md",
        "skills/junie-hook-checker/SKILL.md",
        "agents/nddev-builder.md",
        "commands/nddev-builder-check.md",
        "hooks/nddev-builder-context.py",
        "mcp/mcp.json",
        ".nddev-junie-cli-runtime/home/.junie/allowlist.json",
        "extensions/nddev-builder-marketplace/.junie-extension/marketplace.json",
        "extensions/nddev-builder-marketplace/extensions/nddev-builder/extension.json",
    }
    missing = sorted(required - set(files))
    if missing:
        raise ValueError(f"generated managed files are missing: {', '.join(missing)}")
    forbidden = [relative for relative in files if relative.startswith(".junie/")]
    if forbidden:
        raise ValueError("managed files must not use control-root .junie default discovery")
    config = json.loads(files["config.json"].decode("utf-8"))
    if config.get("brave") is not True:
        raise ValueError("default generated profile must be full-auto")
    forbidden_config_keys = {"auto-update", "guidelines-location"}
    present_forbidden_config_keys = sorted(forbidden_config_keys & set(config))
    if present_forbidden_config_keys:
        raise ValueError(
            "generated config uses unsupported keys: "
            + ", ".join(present_forbidden_config_keys)
        )
    if "hooks" not in config:
        raise ValueError("managed config must enable the deterministic builder hook")
    allowlist = json.loads(files[".nddev-junie-cli-runtime/home/.junie/allowlist.json"].decode("utf-8"))
    if allowlist.get("defaultBehavior") != "ask":
        raise ValueError("managed allowlist must remain ask-first")
    marketplace = json.loads(
        files["extensions/nddev-builder-marketplace/.junie-extension/marketplace.json"].decode("utf-8")
    )
    if marketplace.get("metadata", {}).get("version") != version:
        raise ValueError("extension marketplace metadata version differs from VERSION")
    extensions = marketplace.get("extensions")
    if not isinstance(extensions, list) or len(extensions) != 1:
        raise ValueError("extension marketplace must expose exactly one managed extension")
    extension = extensions[0]
    if not isinstance(extension, dict) or extension.get("version") != version:
        raise ValueError("managed extension version differs from VERSION")


def builder_projection_keys(files: dict[str, bytes]) -> set[str]:
    profile_dependent = {
        "config.json",
        "AGENTS.md",
        ".nddev-junie-cli-runtime/home/.junie/allowlist.json",
        "extensions/nddev-builder-marketplace/extensions/nddev-builder/guidelines/AGENTS.md",
    }
    return {relative for relative in files if relative not in profile_dependent}


def validate_builder_projection_invariant(manager: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-builder-invariant-") as raw:
        target = Path(raw) / "target"
        target.mkdir(mode=0o700)
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        safe_files = manager.desired_files(target, setup, manager.load_profile("safe"))
        full_auto_files = manager.desired_files(target, setup, manager.load_profile("full-auto"))
    if builder_projection_keys(safe_files) != builder_projection_keys(full_auto_files):
        raise ValueError("builder projection file sets differ across permission profiles")
    for relative in builder_projection_keys(safe_files):
        if safe_files[relative] != full_auto_files[relative]:
            raise ValueError(f"builder projection changed across profiles: {relative}")


def validate_manager_parse(manager: Any) -> None:
    manager.parse_args(["plan", "--target", "/tmp/target"])
    manager.parse_args(["install", "--target", "/tmp/target"])
    manager.parse_args(
        ["switch", "--setup", "nddev-builder", "--profile", "safe", "--target", "/tmp/target"]
    )
    manager.parse_args(["migrate", "--target", "/tmp/target"])
    manager.parse_args(
        ["migrate", "--setup", "nddev-builder", "--profile", "full-auto", "--target", "/tmp/target"]
    )
    manager.parse_args(["launch", "--target", "/tmp/target", "--", "--version"])


def expect_manager_failure(label: str, callback: Any) -> None:
    try:
        callback()
    except Exception as exc:
        if exc.__class__.__name__ == "JunieCliSetupError":
            return
        raise ValueError(f"{label}: unexpected exception {exc!r}") from exc
    raise ValueError(f"{label}: expected fail-closed manager rejection")


def validate_production_source() -> None:
    source = (ROOT / "cli-tools" / "nddev_junie_cli.py").read_text(encoding="utf-8")
    forbidden_literals = [
        "ALLOW_TEST",
        "TEST_INSTALLER",
        "INSTALLER_URL",
        "INSTALLER_SHA256",
        "UPDATE_INFO_URL",
        "VERIFY_ARTIFACT_SHA256",
        "INSTALL_TIMEOUT_SECONDS\"",
        "PROBE_TIMEOUT_SECONDS\"",
        "test_override_enabled",
        "env_timeout_seconds",
        "fixture override",
        "artifact fixture",
    ]
    present = sorted(literal for literal in forbidden_literals if literal in source)
    if present:
        raise ValueError(
            "production manager exposes test/source/timeout switch literals: "
            + ", ".join(present)
        )
    if "os.environ.get(\"NDDEV_" in source or "os.environ['NDDEV_" in source:
        raise ValueError("production manager must not read NDDEV_* environment overrides")
    allowed_child_env = {
        'env["NDDEV_JUNIE_EXPECTED_ARTIFACT_SHA256"] = artifact["sha256"]',
        'env["NDDEV_JUNIE_EXPECTED_ARTIFACT_SIZE"] = str(artifact["size"])',
    }
    nddev_lines = {
        line.strip()
        for line in source.splitlines()
        if "NDDEV_" in line and not line.strip().startswith("#")
    }
    if nddev_lines != allowed_child_env:
        raise ValueError("production manager has unexpected NDDEV_* environment surface")
    if '["bash",' in source or "'bash'," in source:
        raise ValueError("installer must use an absolute trusted shell, not PATH lookup")
    if '"PATH": os.environ' in source or "'PATH': os.environ" in source:
        raise ValueError("subprocess PATH must not inherit the ambient user PATH")


def validate_adversarial_smokes(manager: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-security-") as raw:
        root = Path(raw)
        target = root / "target"
        target.mkdir(mode=0o700)

        open_target = root / "open-target"
        open_target.mkdir(mode=0o777)
        try:
            expect_manager_failure(
                "0777 target",
                lambda: manager.validate_target(open_target, create=False),
            )
        finally:
            os.chmod(open_target, 0o700)

        lock_target = root / "lock-target"
        lock_target.mkdir(mode=0o700)
        external_lock = root / "external-lock"
        external_lock.mkdir(mode=0o700)
        (lock_target / manager.LOCK_DIR_NAME).symlink_to(external_lock)
        expect_manager_failure(
            "symlink lock path",
            lambda: manager.target_lock(lock_target, create_parent=False).__enter__(),
        )

        prelocked = root / "prelocked"
        prelocked.mkdir(mode=0o700)
        (prelocked / manager.LOCK_DIR_NAME).mkdir(mode=0o700)
        expect_manager_failure(
            "precreated lock path",
            lambda: manager.target_lock(prelocked, create_parent=False).__enter__(),
        )

        backup_target = root / "backup-target"
        backup_target.mkdir(mode=0o700)
        external_backup = root / "external-backup"
        external_backup.mkdir(mode=0o700)
        (backup_target / manager.BACKUP_DIR_NAME).symlink_to(external_backup)
        expect_manager_failure(
            "symlink backup pool",
            lambda: manager.choose_backup_slot(manager.backup_pool(backup_target)),
        )

        slot_target = root / "slot-target"
        slot_target.mkdir(mode=0o700)
        pool = slot_target / manager.BACKUP_DIR_NAME
        pool.mkdir(mode=0o700)
        slot = pool / "0"
        slot.mkdir(mode=0o777)
        try:
            expect_manager_failure(
                "precreated shared backup slot",
                lambda: manager.choose_backup_slot(pool),
            )
        finally:
            os.chmod(slot, 0o700)

        marker_target = root / "marker-target"
        marker_target.mkdir(mode=0o700)
        marker = marker_target / "external-marker.txt"
        marker.write_text("keep\n", encoding="utf-8")
        managed = marker_target / "config.json"
        manager.atomic_write(managed, b"{}\n", marker_target)
        snapshot = manager.snapshot_files(marker_target, ["config.json"])
        managed.unlink()
        manager.restore_snapshot(marker_target, snapshot)
        if marker.read_text(encoding="utf-8") != "keep\n":
            raise ValueError("external marker was not preserved by restore snapshot")

        old_path = os.environ.get("PATH")
        fake_bin = root / "fake-bin"
        fake_bin.mkdir(mode=0o700)
        (fake_bin / "python3").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        os.chmod(fake_bin / "python3", 0o700)
        os.environ["PATH"] = str(fake_bin)
        try:
            env = manager.sanitized_subprocess_env(
                root / "home", root / "home" / ".local" / "share" / "junie", root / "tmp"
            )
            if env["PATH"] != manager.SAFE_SUBPROCESS_PATH:
                raise ValueError("subprocess env inherited fake PATH")
            config = manager.render_config(
                target,
                manager.load_setup(DEFAULT_SETUP_ID),
                manager.load_profile(DEFAULT_PROFILE_ID),
            )
            hook_command = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
            hook_exe = Path(shlex.split(hook_command)[0])
            if not hook_exe.is_absolute() or hook_exe.parent == fake_bin:
                raise ValueError("hook command used a PATH-selected interpreter")
        finally:
            if old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old_path

    sticky_parent = Path("/tmp")
    if sticky_parent.is_dir() and (stat.S_IMODE(sticky_parent.stat().st_mode) & stat.S_ISVTX):
        target = sticky_parent / f"nddev-junie-validator-{os.getpid()}-{id(manager)}"
        with manager.target_lock(target, create_parent=True):
            canonical = manager.validate_target(target, create=False)
            if stat.S_IMODE(canonical.lstat().st_mode) & 0o077:
                raise ValueError("sticky-temp target was not created private")
        canonical = manager.validate_target(target, create=False)
        canonical.rmdir()


def fake_software_metadata() -> dict[str, Any]:
    return {
        "version": "validator",
        "platform": "validator",
        "installer": {"url": "validator", "sha256": "validator"},
        "update_info": "validator",
        "artifact": {
            "download_url": "validator",
            "sha256": "validator",
            "size": 0,
            "platform": "validator",
        },
        "artifact_verification": {
            "size_verified": True,
            "sha256_verified": True,
            "method": "validator",
        },
        "receipt_sha256": "validator",
        "home": ".nddev-junie-cli-runtime/home",
        "shim": {
            "path": ".nddev-junie-cli-runtime/home/.local/bin/junie",
            "mode": "0700",
            "sha256": "validator",
        },
        "binary": {
            "path": ".nddev-junie-cli-runtime/home/.local/share/junie/versions/validator/junie",
            "mode": "0700",
            "size": 0,
            "sha256": "validator",
        },
        "current_link": {
            "path": ".nddev-junie-cli-runtime/home/.local/share/junie/current",
            "target": ".nddev-junie-cli-runtime/home/.local/share/junie/versions/validator",
        },
    }


def with_fake_software(manager: Any, callback: Any) -> None:
    metadata = fake_software_metadata()
    original_begin = manager.begin_software_transaction
    original_commit = manager.commit_software_transaction
    original_current = manager.current_software_metadata
    original_state = manager.software_state
    manager.begin_software_transaction = lambda target, *, repair: manager.SoftwareTransaction(
        metadata=metadata,
        changed=False,
    )
    manager.commit_software_transaction = lambda transaction: None
    manager.current_software_metadata = lambda target: metadata
    manager.software_state = lambda target: {"state": "installed", "version": "validator"}
    try:
        callback()
    finally:
        manager.begin_software_transaction = original_begin
        manager.commit_software_transaction = original_commit
        manager.current_software_metadata = original_current
        manager.software_state = original_state


def validate_profile_switch_lifecycle(manager: Any) -> None:
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="nddev-junie-profile-cycle-") as raw:
            target = Path(raw) / "target"
            setup = manager.load_setup(DEFAULT_SETUP_ID)
            safe = manager.load_profile("safe")
            full_auto = manager.load_profile("full-auto")

            installed = manager.write_setup(target, setup, safe)
            if installed["setup_id"] != DEFAULT_SETUP_ID or installed["profile_id"] != "safe":
                raise ValueError("safe install did not record orthogonal setup/profile ids")
            stamp = manager.read_stamp(manager.validate_target(target, create=False))
            if stamp is None or stamp.get("schema_version") != manager.STAMP_SCHEMA:
                raise ValueError("new install did not write the current orthogonal stamp schema")

            full_auto_result = manager.write_setup(
                target,
                setup,
                full_auto,
                require_existing=True,
            )
            if full_auto_result["profile_id"] != "full-auto":
                raise ValueError("switch to full-auto did not update profile_id")
            if manager.read_stamp(target)["setup_id"] != DEFAULT_SETUP_ID:
                raise ValueError("profile switch changed the content setup id")

            safe_result = manager.write_setup(target, setup, safe, require_existing=True)
            if safe_result["profile_id"] != "safe":
                raise ValueError("switch back to safe did not update profile_id")

    with_fake_software(manager, run)


def legacy_managed_files(manager: Any, target: Path, setup_id: str) -> dict[str, bytes]:
    config = manager.canonical_json(
        {
            "brave": setup_id == "full-auto",
            "skill-default-locations": False,
            "agent-default-location": False,
            "mcp-default-locations": False,
            "command-default-locations": False,
            "model-default-locations": False,
        }
    )
    allowlist = manager.canonical_json(
        {
            "defaultBehavior": "ask",
            "allowReadonlyCommands": setup_id == "full-auto",
            "rules": {
                "fileEditing": {"rules": []},
                "executables": {"rules": []},
                "mcpTools": {"rules": []},
                "readOutsideProject": {"rules": []},
            },
        }
    )
    agents = (
        manager.MANAGED_BEGIN
        + "\n# Legacy NDDev Junie CLI Setup\n"
        + manager.MANAGED_END
        + "\n"
    ).encode("utf-8")
    return {"config.json": config, "allowlist.json": allowlist, "AGENTS.md": agents}


def write_legacy_stamp(manager: Any, target: Path, setup_id: str) -> None:
    target.mkdir(mode=0o700)
    software = fake_software_metadata()
    files = legacy_managed_files(manager, target, setup_id)
    for relative, data in files.items():
        manager.atomic_write(target / relative, data, target)
    managed = {
        relative: manager.managed_digest_for_bytes(relative, data, legacy=True)
        for relative, data in files.items()
    }
    stamp = {
        "schema_version": 1,
        "product_name": manager.PRODUCT_NAME,
        "build_version": "0.1.0",
        "setup_id": setup_id,
        "canonical_target": str(manager.validate_target(target, create=False)),
        "managed_files": managed,
        "software": software,
    }
    manager.atomic_write(manager.stamp_path(target), manager.canonical_json(stamp), target)


def validate_legacy_mapping_migration(manager: Any) -> None:
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="nddev-junie-legacy-migrate-") as raw:
            root = Path(raw)
            setup = manager.load_setup(DEFAULT_SETUP_ID)

            legacy_safe = root / "legacy-safe"
            write_legacy_stamp(manager, legacy_safe, "safe")
            status = manager.status_payload(legacy_safe)
            if (
                status["setup_id"] != DEFAULT_SETUP_ID
                or status["legacy_setup_id"] != "safe"
                or status["launchable"]
                or not status["legacy"]
                or status["profile_id"] != "safe"
            ):
                raise ValueError("legacy safe status did not expose recovery-only profile mapping")
            expect_manager_failure(
                "legacy switch denied",
                lambda: manager.write_setup(
                    legacy_safe,
                    setup,
                    manager.load_profile("full-auto"),
                    require_existing=True,
                ),
            )
            migrated = manager.migrate_setup(legacy_safe, DEFAULT_SETUP_ID, None)
            if migrated["setup_id"] != DEFAULT_SETUP_ID or migrated["profile_id"] != "safe":
                raise ValueError("legacy safe did not migrate to nddev-builder/safe")
            migrated_status = manager.status_payload(legacy_safe)
            if migrated_status["legacy"] or not migrated_status["launchable"]:
                raise ValueError("migrated target did not become current launchable schema")

            legacy_balanced = root / "legacy-balanced"
            write_legacy_stamp(manager, legacy_balanced, "balanced")
            expect_manager_failure(
                "legacy balanced requires explicit profile",
                lambda: manager.migrate_setup(legacy_balanced, DEFAULT_SETUP_ID, None),
            )
            migrated_balanced = manager.migrate_setup(
                legacy_balanced,
                DEFAULT_SETUP_ID,
                "full-auto",
            )
            if migrated_balanced["profile_id"] != "full-auto":
                raise ValueError("legacy balanced explicit profile migration failed")

    with_fake_software(manager, run)


def main(argv: list[str] | None = None) -> int:
    parse_args(argv)
    version = load_version()
    build = load_json(ROOT / "build" / "version.json")
    manifest = load_json(ROOT / "build" / "manifest.json")
    contract = load_json(ROOT / "config" / "nddev-contract.json")
    baseline = load_json(ROOT / "references" / "junie-cli-baseline.json")
    ids = setup_ids()
    profiles = profile_ids()
    if build.get("build_version") != version or manifest.get("build_version") != version:
        raise ValueError("build version fields are not synchronized")
    if build.get("nddev_builder_projection_version") != version:
        raise ValueError("builder projection version must track build version")
    if manifest.get("nddev_builder_projection_version") != version:
        raise ValueError("manifest builder projection version must track build version")
    if contract.get("version_ref") != "build/version.json":
        raise ValueError("contract version_ref must point at build/version.json")
    if contract.get("manifest_ref") != "build/manifest.json":
        raise ValueError("contract manifest_ref must point at build/manifest.json")
    if contract["managed_state"]["stamp_schema"] != 3:
        raise ValueError("contract must declare current orthogonal stamp schema")
    if manifest.get("setup_ids") != ids or contract["setup_system"]["setup_ids"] != ids:
        raise ValueError("setup ids are not synchronized")
    if manifest.get("profile_ids") != profiles:
        raise ValueError("manifest profile ids are not synchronized")
    if contract["permission_profiles"]["profile_ids"] != profiles:
        raise ValueError("contract profile ids are not synchronized")
    if manifest.get("default_setup_id") != DEFAULT_SETUP_ID:
        raise ValueError("manifest default setup mismatch")
    if contract["setup_system"]["default_setup_id"] != DEFAULT_SETUP_ID:
        raise ValueError("contract default setup mismatch")
    if manifest.get("default_profile_id") != DEFAULT_PROFILE_ID:
        raise ValueError("manifest default profile mismatch")
    if contract["permission_profiles"]["default_profile_id"] != DEFAULT_PROFILE_ID:
        raise ValueError("contract default profile mismatch")
    if build.get("junie_cli_tested") != baseline["release"]["stable_version"]:
        raise ValueError("tested Junie CLI version differs from baseline release")
    validate_baseline(baseline, contract)
    validate_required_files()
    manager = load_manager()
    validate_production_source()
    validate_generated_files(manager, version)
    validate_builder_projection_invariant(manager)
    validate_manager_parse(manager)
    validate_adversarial_smokes(manager)
    validate_profile_switch_lifecycle(manager)
    validate_legacy_mapping_migration(manager)
    validate_workflows()
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"validate_public_contracts.py: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
