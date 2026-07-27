#!/usr/bin/env python3
"""Validate public nddev-junie-cli-app release contracts."""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import importlib.util
import json
import os
import shlex
import stat
import subprocess
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
RELEASE_ARCHIVE_PATHS = [
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "VERSION",
    ".gds",
    ".github",
    "build",
    "cli-tools",
    "config",
    "builder",
    "profiles",
    "setups",
    "references",
    "docs",
]
RELEASE_RUNTIME_PATHS = [
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "VERSION",
    "build",
    "cli-tools",
    "config",
    "builder",
    "profiles",
    "setups",
    "references",
    "docs",
]


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


def tracked_paths() -> set[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        item.decode("utf-8")
        for item in completed.stdout.split(b"\0")
        if item
    }


def release_input_paths(value: str, label: str) -> list[str]:
    paths = value.split()
    if not paths:
        raise ValueError(f"release workflow {label} must not be empty")
    if len(paths) != len(set(paths)):
        raise ValueError(f"release workflow {label} contains duplicate paths")
    for path in paths:
        if path.startswith("/") or ".." in Path(path).parts:
            raise ValueError(f"release workflow {label} contains an unsafe path: {path}")
    return paths


def parse_release_inputs(text: str) -> dict[str, str]:
    lines = text.splitlines()
    inputs: dict[str, str] = {}
    index = 0
    in_with = False
    while index < len(lines):
        line = lines[index]
        if line == "    with:":
            in_with = True
            index += 1
            continue
        if not in_with:
            index += 1
            continue
        if not line.startswith("      "):
            break
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            index += 1
            continue
        key, value = stripped.split(":", 1)
        value = value.strip()
        if value == ">-":
            index += 1
            parts: list[str] = []
            while index < len(lines) and lines[index].startswith("        "):
                parts.append(lines[index].strip())
                index += 1
            inputs[key] = " ".join(part for part in parts if part)
            continue
        inputs[key] = value
        index += 1
    return inputs


def path_is_tracked(path: str, tracked: set[str]) -> bool:
    full = ROOT / path
    if not full.exists():
        return False
    if full.is_file():
        return path in tracked
    if full.is_dir():
        prefix = f"{path}/"
        return any(item.startswith(prefix) for item in tracked)
    return False


def require_release_paths_tracked(paths: list[str], tracked: set[str], label: str) -> None:
    missing = [path for path in paths if not path_is_tracked(path, tracked)]
    if missing:
        raise ValueError(
            f"release workflow {label} paths must exist and be tracked: {', '.join(missing)}"
        )


def path_covered(required: str, roots: list[str]) -> bool:
    return any(required == root or required.startswith(f"{root}/") for root in roots)


def validate_release_runtime_closure(
    runtime_paths: list[str],
    contract: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    required = {
        "AGENTS.md",
        "LICENSE",
        "README.md",
        "VERSION",
        "build",
        "builder",
        "cli-tools",
        "config",
        "docs",
        "profiles",
        "references",
        "setups",
        str(contract["manifest_ref"]),
        str(contract["version_ref"]),
        str(manifest["manager"]),
        str(manifest["contract"]),
        str(manifest["runtime_baseline"]),
        str(contract["setup_system"]["catalog_root"]),
        str(contract["permission_profiles"]["catalog_root"]),
    }
    missing = sorted(path for path in required if not path_covered(path, runtime_paths))
    if missing:
        raise ValueError(
            "release workflow runtime_paths do not cover contract runtime paths: "
            + ", ".join(missing)
        )


def validate_release_workflow(
    version: str,
    contract: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    path = ROOT / ".github" / "workflows" / "release.yml"
    text = path.read_text(encoding="utf-8")
    if '- "[0-9]+.[0-9]+.[0-9]+"' not in text:
        raise ValueError("release workflow must use numeric stable release tags")
    if "\npermissions: {}\n" not in text:
        raise ValueError("release workflow must set top-level permissions to {}")
    expected_permissions = {
        "contents: write",
        "id-token: write",
        "attestations: write",
        "artifact-metadata: write",
    }
    for permission in expected_permissions:
        if text.count(permission) != 1:
            raise ValueError(f"release workflow missing job permission: {permission}")
    inputs = parse_release_inputs(text)
    if inputs.get("version") != "${{ github.ref_name }}":
        raise ValueError("release workflow must pass version from github.ref_name")
    if inputs.get("package_name") != contract["product_name"]:
        raise ValueError("release workflow package_name must match the product")
    archive_paths = release_input_paths(inputs.get("archive_paths", ""), "archive_paths")
    runtime_paths = release_input_paths(inputs.get("runtime_paths", ""), "runtime_paths")
    if archive_paths != RELEASE_ARCHIVE_PATHS:
        raise ValueError("release workflow archive_paths are not synchronized")
    if runtime_paths != RELEASE_RUNTIME_PATHS:
        raise ValueError("release workflow runtime_paths are not synchronized")
    tracked = tracked_paths()
    require_release_paths_tracked(archive_paths, tracked, "archive_paths")
    require_release_paths_tracked(runtime_paths, tracked, "runtime_paths")
    validate_release_runtime_closure(runtime_paths, contract, manifest)
    if any(value == version for value in inputs.values()):
        raise ValueError("release workflow must not hardcode the current build version")


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


def fake_software_metadata(
    *,
    shim_sha256: str = "validator",
    binary_size: int = 0,
    binary_sha256: str = "validator",
) -> dict[str, Any]:
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
            "sha256": shim_sha256,
        },
        "binary": {
            "path": ".nddev-junie-cli-runtime/home/.local/share/junie/versions/validator/junie",
            "mode": "0700",
            "size": binary_size,
            "sha256": binary_sha256,
        },
        "current_link": {
            "path": ".nddev-junie-cli-runtime/home/.local/share/junie/current",
            "target": ".nddev-junie-cli-runtime/home/.local/share/junie/versions/validator",
        },
    }


def with_fake_software(
    manager: Any,
    callback: Any,
    metadata: dict[str, Any] | None = None,
) -> None:
    metadata = fake_software_metadata() if metadata is None else metadata
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


def fake_launch_runtime_metadata(manager: Any, target: Path) -> dict[str, Any]:
    runtime = manager.runtime_root(target)
    home = manager.runtime_home(target)
    data = manager.runtime_data(target)
    version_dir = data / "versions" / "validator"
    for directory in (
        runtime,
        home,
        home / ".local",
        home / ".local" / "bin",
        data,
        data / "versions",
        version_dir,
    ):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    shim = manager.runtime_bin(target)
    binary = version_dir / "junie"
    shim.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.write_bytes(b"validator Junie binary\n")
    os.chmod(shim, manager.OWNER_EXECUTABLE_MODE)
    os.chmod(binary, manager.OWNER_EXECUTABLE_MODE)
    return fake_software_metadata(
        shim_sha256=manager.sha256_file_bounded(
            shim,
            max_bytes=manager.MANAGED_MAX_BYTES,
            label="validator Junie shim",
        ),
        binary_size=binary.stat().st_size,
        binary_sha256=manager.sha256_file_bounded(
            binary,
            max_bytes=manager.SOFTWARE_FILE_MAX_BYTES,
            label="validator Junie binary",
        ),
    )


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


def validate_launch_lock_concurrency(manager: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-launch-lock-") as raw:
        target = Path(raw) / "target"
        target.mkdir(mode=0o700)
        canonical = manager.validate_target(target, create=False)
        metadata = fake_launch_runtime_metadata(manager, canonical)
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        safe = manager.load_profile("safe")
        full_auto = manager.load_profile("full-auto")
        lock = canonical / manager.LOCK_DIR_NAME

        def run() -> None:
            manager.write_setup(canonical, setup, safe)
            original_run = manager.subprocess.run
            original_snapshot = manager.snapshot_live_junie_home
            original_guard = manager.require_live_junie_home_unchanged
            seen = {"child": False, "guard": False}

            class Completed:
                returncode = 0

            def fake_snapshot() -> str:
                if not lock.is_dir():
                    raise ValueError("launch snapshot did not run under the target lock")
                return "validator-live-home"

            def fake_guard(snapshot: str, *, context: str) -> None:
                if snapshot != "validator-live-home" or context != "Junie launch":
                    raise ValueError("launch guard arguments changed")
                if not lock.is_dir():
                    raise ValueError("post-launch guard did not run under the target lock")
                seen["guard"] = True

            def fake_run(
                command: list[str],
                *,
                cwd: Path,
                env: dict[str, str],
                check: bool,
            ) -> Completed:
                seen["child"] = True
                if cwd != canonical:
                    raise ValueError("launch child cwd escaped target")
                if Path(command[0]) != manager.runtime_bin(canonical):
                    raise ValueError("launch did not execute the target-owned shim")
                if env["HOME"] != str(manager.runtime_home(canonical)):
                    raise ValueError("launch child HOME escaped runtime home")
                if check is not False:
                    raise ValueError("launch must forward child exit status without check=True")
                if not lock.is_dir():
                    raise ValueError("child did not run under the target lock")
                expect_manager_failure(
                    "mutation while launch lock held",
                    lambda: manager.write_setup(
                        canonical,
                        setup,
                        full_auto,
                        require_existing=True,
                    ),
                )
                if not lock.is_dir():
                    raise ValueError("blocked mutation removed the launch lock")
                return Completed()

            manager.subprocess.run = fake_run
            manager.snapshot_live_junie_home = fake_snapshot
            manager.require_live_junie_home_unchanged = fake_guard
            try:
                result = manager.launch(canonical, ["--version"])
            finally:
                manager.subprocess.run = original_run
                manager.snapshot_live_junie_home = original_snapshot
                manager.require_live_junie_home_unchanged = original_guard
            if result != 0 or not seen["child"] or not seen["guard"]:
                raise ValueError("launch lock regression did not execute the child and guard")
            if lock.exists():
                raise ValueError("launch lock was not cleaned up after child completion")
            switched = manager.write_setup(canonical, setup, full_auto, require_existing=True)
            if switched["profile_id"] != "full-auto":
                raise ValueError("lifecycle mutation remained blocked after launch completed")

        with_fake_software(manager, run, metadata=metadata)


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


def target_file_bytes(target: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(target.rglob("*")):
        relative = str(path.relative_to(target))
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            files[relative] = ("SYMLINK:" + os.readlink(path)).encode("utf-8")
        elif stat.S_ISREG(info.st_mode):
            files[relative] = path.read_bytes()
    return files


def write_backup_envelope(manager: Any, target: Path, envelope: dict[str, Any]) -> None:
    path = manager.backup_pool(target) / "0" / manager.BACKUP_NAME
    manager.atomic_write(path, manager.canonical_json(envelope), target)


def restore_corruption_target(manager: Any, root: Path) -> Path:
    target = root / "target"
    setup = manager.load_setup(DEFAULT_SETUP_ID)
    manager.write_setup(target, setup, manager.load_profile("safe"))
    switched = manager.write_setup(
        target,
        setup,
        manager.load_profile("full-auto"),
        require_existing=True,
    )
    if switched["backup_slot"] != 0:
        raise ValueError("restore regression did not create backup slot 0")
    return manager.validate_target(target, create=False)


def read_backup_envelope(manager: Any, target: Path) -> dict[str, Any]:
    path = manager.backup_pool(target) / "0" / manager.BACKUP_NAME
    return json.loads(path.read_text(encoding="utf-8"))


def expect_restore_failure_byte_identical(
    manager: Any,
    label: str,
    mutate: Any,
    *,
    use_main: bool = False,
) -> None:
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix=f"nddev-junie-restore-{label}-") as raw:
            target = restore_corruption_target(manager, Path(raw))
            envelope = read_backup_envelope(manager, target)
            mutate(envelope)
            write_backup_envelope(manager, target, envelope)
            before = target_file_bytes(target)
            if use_main:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = manager.main(
                        [
                            "restore",
                            "--backup",
                            "0",
                            "--target",
                            str(target),
                            "--json",
                        ]
                    )
                if code != 2:
                    raise ValueError(f"{label}: restore did not return rc 2")
                payload = json.loads(stdout.getvalue())
                if not isinstance(payload.get("error"), str) or not payload["error"]:
                    raise ValueError(f"{label}: restore did not emit a JSON domain error")
            else:
                expect_manager_failure(label, lambda: manager.restore_backup(target, 0))
            after = target_file_bytes(target)
            if after != before:
                raise ValueError(f"{label}: failed restore changed target bytes")

    with_fake_software(manager, run)


def validate_restore_backup_fail_closed(manager: Any) -> None:
    def invalid_base64(envelope: dict[str, Any]) -> None:
        envelope["files"]["AGENTS.md"] = "!!!!"

    def non_ascii_payload(envelope: dict[str, Any]) -> None:
        envelope["files"]["AGENTS.md"] = "snowman: \u2603"

    def digest_mismatch(envelope: dict[str, Any]) -> None:
        envelope["files"]["AGENTS.md"] = base64.b64encode(b"tampered\n").decode("ascii")

    def invalid_path(envelope: dict[str, Any]) -> None:
        envelope["files"]["../escape"] = base64.b64encode(b"x\n").decode("ascii")

    expect_restore_failure_byte_identical(
        manager,
        "invalid backup base64",
        invalid_base64,
        use_main=True,
    )
    expect_restore_failure_byte_identical(
        manager,
        "non-ascii backup payload",
        non_ascii_payload,
    )
    expect_restore_failure_byte_identical(
        manager,
        "backup digest mismatch",
        digest_mismatch,
    )
    expect_restore_failure_byte_identical(
        manager,
        "invalid backup path",
        invalid_path,
    )


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
    if contract["safety"].get("launch_holds_target_lock_until_child_exit") is not True:
        raise ValueError("contract must declare launch lock scope")
    if contract["safety"].get("launch_blocks_lifecycle_mutations_while_child_runs") is not True:
        raise ValueError("contract must declare launch mutation blocking")
    if "lock_scope" not in contract.get("runtime_launch", {}):
        raise ValueError("contract runtime_launch must describe launch lock scope")
    if "pre_child_artifact_revalidation" not in contract.get("runtime_launch", {}):
        raise ValueError("contract runtime_launch must describe artifact revalidation")
    if "launch_lock" not in manifest.get("runtime_isolation", {}):
        raise ValueError("manifest runtime_isolation must describe launch lock scope")
    if "pre_child_artifact_revalidation" not in manifest.get("runtime_isolation", {}):
        raise ValueError("manifest runtime_isolation must describe artifact revalidation")
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
    validate_launch_lock_concurrency(manager)
    validate_legacy_mapping_migration(manager)
    validate_restore_backup_fail_closed(manager)
    validate_workflows()
    validate_release_workflow(version, contract, manifest)
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"validate_public_contracts.py: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
