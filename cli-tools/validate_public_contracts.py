#!/usr/bin/env python3
"""Validate public nddev-junie-cli-app release contracts."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib.util
import io
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
PUBLIC_SUPPORTED_PLATFORM_ORDER = [
    "macos-arm64",
    "macos-x64",
    "ubuntu-glibc-arm64",
    "ubuntu-glibc-x64",
]
PUBLIC_SUPPORTED_PLATFORMS = set(PUBLIC_SUPPORTED_PLATFORM_ORDER)
OFFICIAL_ARTIFACT_PLATFORM_ORDER = [
    "linux-aarch64",
    "linux-amd64",
    "macos-aarch64",
    "macos-amd64",
]
OFFICIAL_ARTIFACT_PLATFORMS = set(OFFICIAL_ARTIFACT_PLATFORM_ORDER)
ARTIFACT_PLATFORM_MAP = {
    "macos-arm64": "macos-aarch64",
    "macos-x64": "macos-amd64",
    "ubuntu-glibc-arm64": "linux-aarch64",
    "ubuntu-glibc-x64": "linux-amd64",
}
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
    ".claude",
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
RELEASE_PATH_TYPES = {
    "AGENTS.md": "file",
    "LICENSE": "file",
    "README.md": "file",
    "VERSION": "file",
    ".claude": "directory",
    ".gds": "directory",
    ".github": "directory",
    "build": "directory",
    "cli-tools": "directory",
    "config": "directory",
    "builder": "directory",
    "profiles": "directory",
    "setups": "directory",
    "references": "directory",
    "docs": "directory",
}
PRIVATE_ARTIFACT_MARKERS = {
    ".agents",
    ".codex",
    ".junie",
    ".pytest_cache",
    ".serena",
    "__pycache__",
    "tests",
    "validation",
}
BOOTSTRAP_SNAPSHOT_MAX_CHILDREN = 1024
BOOTSTRAP_SNAPSHOT_MAX_FILE_BYTES = 1024 * 1024


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
    spec = importlib.util.spec_from_file_location(
        "nddev_junie_cli_public", ROOT / "cli-tools" / "nddev_junie_cli.py"
    )
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


def tracked_paths() -> set[str] | None:
    if not (ROOT / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"git tracked path scan failed: {exc}") from exc
    try:
        return {item.decode("utf-8") for item in completed.stdout.split(b"\0") if item}
    except UnicodeDecodeError as exc:
        raise ValueError(f"git tracked path scan returned non-UTF-8 paths: {exc}") from exc


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


def require_release_paths_tracked(
    paths: list[str],
    tracked: set[str] | None,
    label: str,
) -> None:
    if tracked is None:
        return
    missing = [path for path in paths if not path_is_tracked(path, tracked)]
    if missing:
        raise ValueError(
            f"release workflow {label} paths must exist and be tracked: {', '.join(missing)}"
        )


def reject_private_artifact_marker(path: Path, label: str) -> None:
    relative_parts = path.relative_to(ROOT).parts
    for part in relative_parts:
        if part in PRIVATE_ARTIFACT_MARKERS:
            raise ValueError(f"release workflow {label} contains private marker {part}")


def require_release_path_shape(path: str, label: str) -> None:
    expected_type = RELEASE_PATH_TYPES.get(path)
    if expected_type is None:
        raise ValueError(f"release workflow {label} has no type contract for {path}")
    full = ROOT / path
    try:
        info = full.lstat()
    except OSError as exc:
        raise ValueError(f"release workflow {label} path is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"release workflow {label} path must not be a symlink: {path}")
    if expected_type == "file":
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"release workflow {label} path must be a file: {path}")
        reject_private_artifact_marker(full, label)
        return
    if expected_type != "directory":
        raise ValueError(f"release workflow {label} type contract is invalid for {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"release workflow {label} path must be a directory: {path}")
    reject_private_artifact_marker(full, label)
    for child in full.rglob("*"):
        reject_private_artifact_marker(child, label)
        try:
            child_info = child.lstat()
        except OSError as exc:
            raise ValueError(
                f"release workflow {label} path disappeared during validation: "
                f"{child.relative_to(ROOT)}"
            ) from exc
        if stat.S_ISLNK(child_info.st_mode):
            raise ValueError(
                f"release workflow {label} must not contain symlinks: {child.relative_to(ROOT)}"
            )


def validate_release_paths_exist_and_safe(paths: list[str], label: str) -> None:
    for path in paths:
        require_release_path_shape(path, label)


def require_marker_token(text: str, token: str, label: str) -> None:
    if text.splitlines().count(token) != 1:
        raise ValueError(f"release archive public marker missing or duplicated {label}")


def validate_public_artifact_marker(contract: dict[str, Any]) -> None:
    marker = ROOT / ".gds" / "repository.yaml"
    if not marker.is_file():
        raise ValueError("release archive must include .gds/repository.yaml")
    siblings = sorted(item.name for item in marker.parent.iterdir())
    if siblings != ["repository.yaml"]:
        raise ValueError(".gds must not contain generated policy copies")
    text = marker.read_text(encoding="utf-8")
    owner, name = str(contract["github_repository"]).split("/", 1)
    required_tokens = (
        ("schema_version: 1", "schema version"),
        (f'  display_name: "{contract["product_name"]}"', "repository display name"),
        ('    - "module"', "module role"),
        ('  type: "github"', "GitHub provider type"),
        ('  installation: "installation:github-organization"', "GitHub installation"),
        (f'  owner: "{owner}"', "GitHub owner"),
        (f'  name: "{name}"', "GitHub repository name"),
        ('  visibility_contract: "public"', "public visibility"),
        ('  data_classification: "public"', "public data classification"),
        ('  contract: "public"', "public module contract"),
        ('      - "python3 cli-tools/validate_public_contracts.py"', "test command"),
        ('    - "test"', "required test"),
    )
    for item in required_tokens:
        if isinstance(item, tuple):
            token, label = item
        else:
            token = item
            label = item
        require_marker_token(text, token, label)


def validate_claude_import_bridge() -> None:
    directory = ROOT / ".claude"
    bridge = directory / "CLAUDE.md"
    try:
        directory_info = directory.lstat()
        bridge_info = bridge.lstat()
    except OSError as exc:
        raise ValueError(".claude/CLAUDE.md import bridge is missing") from exc
    if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
        raise ValueError(".claude must be a real directory")
    if stat.S_ISLNK(bridge_info.st_mode) or not stat.S_ISREG(bridge_info.st_mode):
        raise ValueError(".claude/CLAUDE.md must be a regular file")
    if sorted(item.name for item in directory.iterdir()) != ["CLAUDE.md"]:
        raise ValueError(".claude must contain only the Claude import bridge")
    if bridge.read_bytes() != b"@../AGENTS.md\n":
        raise ValueError(".claude/CLAUDE.md must exactly import ../AGENTS.md")
    if not (ROOT / "AGENTS.md").is_file():
        raise ValueError(".claude/CLAUDE.md import target is missing")


def validate_release_runtime_subset(archive_paths: list[str], runtime_paths: list[str]) -> None:
    missing = sorted(path for path in runtime_paths if not path_covered(path, archive_paths))
    if missing:
        raise ValueError(
            "release workflow runtime_paths must be covered by archive_paths: " + ", ".join(missing)
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
    validate_release_paths_exist_and_safe(archive_paths, "archive_paths")
    validate_release_paths_exist_and_safe(runtime_paths, "runtime_paths")
    validate_public_artifact_marker(contract)
    validate_release_runtime_subset(archive_paths, runtime_paths)
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
        ".claude/CLAUDE.md",
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


def validate_supported_platform_scope(
    baseline: dict[str, Any], contract: dict[str, Any], manifest: dict[str, Any]
) -> None:
    release = baseline["release"]
    runtime = contract["runtime_compatibility"]
    for label, owner in (
        ("baseline release", release),
        ("contract runtime_compatibility", runtime),
        ("manifest", manifest),
    ):
        if owner.get("supported_platforms") != PUBLIC_SUPPORTED_PLATFORM_ORDER:
            raise ValueError(f"{label}: supported platforms must be macOS and Ubuntu only")
        if owner.get("artifact_platform_map") != ARTIFACT_PLATFORM_MAP:
            raise ValueError(f"{label}: artifact platform map is not synchronized")
        unsupported = owner.get("unsupported_platforms")
        if not isinstance(unsupported, dict):
            raise ValueError(f"{label}: unsupported platform scope must be documented")
        for key in ("windows", "non-ubuntu-linux", "linux-musl", "unsupported-architecture"):
            if key not in unsupported:
                raise ValueError(f"{label}: unsupported platform scope is missing {key}")
    if runtime.get("official_artifact_platforms") != OFFICIAL_ARTIFACT_PLATFORM_ORDER:
        raise ValueError("contract official artifact platforms are not synchronized")
    if manifest.get("official_artifact_platforms") != OFFICIAL_ARTIFACT_PLATFORM_ORDER:
        raise ValueError("manifest official artifact platforms are not synchronized")
    ubuntu_support = release.get("ubuntu_support")
    if ubuntu_support != runtime.get("ubuntu_support"):
        raise ValueError("baseline and contract Ubuntu support policy differ")
    if ubuntu_support != manifest.get("ubuntu_support"):
        raise ValueError("manifest Ubuntu support policy differs from the contract")
    if not isinstance(ubuntu_support, dict):
        raise ValueError("Ubuntu support policy must be an object")
    if ubuntu_support.get("distribution_detection") != "platform.freedesktop_os_release ID=ubuntu":
        raise ValueError("Ubuntu distribution detection owner is not documented")
    if ubuntu_support.get("ubuntu_release_floor") is not None:
        raise ValueError("Ubuntu release floor must remain null until upstream publishes one")
    if ubuntu_support.get("glibc_version_floor") is not None:
        raise ValueError("glibc version floor must remain null until upstream publishes one")
    if ubuntu_support.get("official_floor") != "no-official-floor":
        raise ValueError("Ubuntu/glibc floor policy must be no-official-floor")
    if ubuntu_support.get("binary_abi") != "glibc":
        raise ValueError("Ubuntu binary ABI policy must be glibc")
    if any(str(platform_id).startswith("linux-") for platform_id in runtime["supported_platforms"]):
        raise ValueError("public supported-platform contract must not expose generic Linux")
    if set(release["exact_artifacts"]) != OFFICIAL_ARTIFACT_PLATFORMS:
        raise ValueError("baseline must preserve only official macOS/linux artifact keys")
    if set(release["exact_artifact_hashes"]) != OFFICIAL_ARTIFACT_PLATFORMS:
        raise ValueError("baseline exact hashes must preserve only official artifact keys")


def validate_baseline(
    baseline: dict[str, Any], contract: dict[str, Any], manifest: dict[str, Any]
) -> None:
    if baseline["release"]["channel"] != "release":
        raise ValueError("baseline channel must be release")
    if baseline["runtime"]["command"] != "junie":
        raise ValueError("baseline command must be junie")
    validate_supported_platform_scope(baseline, contract, manifest)
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


def expect_platform_failure(manager: Any, label: str, **kwargs: Any) -> None:
    try:
        manager.current_platform_id(**kwargs)
    except manager.JunieCliSetupError:
        return
    raise ValueError(f"{label}: unsupported platform was accepted")


def validate_platform_detection(manager: Any) -> None:
    accepted = {
        "macos arm64": (
            {"system": "Darwin", "machine": "arm64"},
            "macos-arm64",
            "macos-aarch64",
        ),
        "macos x64": (
            {"system": "Darwin", "machine": "x86_64"},
            "macos-x64",
            "macos-amd64",
        ),
        "ubuntu arm64": (
            {
                "system": "Linux",
                "machine": "aarch64",
                "os_release": {"ID": "ubuntu", "NAME": "Ubuntu", "VERSION_ID": "24.04"},
                "libc": ("glibc", "2.39"),
            },
            "ubuntu-glibc-arm64",
            "linux-aarch64",
        ),
        "ubuntu x64": (
            {
                "system": "Linux",
                "machine": "x86_64",
                "os_release": {"ID": "ubuntu", "NAME": "Ubuntu Server", "VERSION_ID": "22.04"},
                "libc": ("glibc", "2.35"),
            },
            "ubuntu-glibc-x64",
            "linux-amd64",
        ),
    }
    for label, (kwargs, expected_host, expected_platform) in accepted.items():
        actual_host = manager.current_host_id(**kwargs)
        if actual_host != expected_host:
            raise ValueError(f"{label}: detected host {actual_host}, expected {expected_host}")
        actual_platform = manager.current_platform_id(**kwargs)
        if actual_platform != expected_platform:
            raise ValueError(
                f"{label}: detected artifact platform {actual_platform}, expected {expected_platform}"
            )
    for label, kwargs in {
        "debian x64": {
            "system": "Linux",
            "machine": "x86_64",
            "os_release": {"ID": "debian", "ID_LIKE": "debian"},
            "libc": ("glibc", "2.36"),
        },
        "alpine musl": {
            "system": "Linux",
            "machine": "x86_64",
            "os_release": {"ID": "alpine"},
            "libc": ("musl", "1.2.5"),
        },
        "ubuntu musl": {
            "system": "Linux",
            "machine": "x86_64",
            "os_release": {"ID": "ubuntu", "VERSION_ID": "24.04"},
            "libc": ("musl", "1.2.5"),
        },
        "unknown linux": {
            "system": "Linux",
            "machine": "x86_64",
            "os_release": {},
            "libc": ("glibc", "2.39"),
        },
        "windows": {"system": "Windows", "machine": "AMD64"},
        "unsupported arch": {"system": "Darwin", "machine": "armv7l"},
    }.items():
        expect_platform_failure(manager, label, **kwargs)


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
            "generated config uses unsupported keys: " + ", ".join(present_forbidden_config_keys)
        )
    if "hooks" not in config:
        raise ValueError("managed config must enable the deterministic builder hook")
    allowlist = json.loads(
        files[".nddev-junie-cli-runtime/home/.junie/allowlist.json"].decode("utf-8")
    )
    if allowlist.get("defaultBehavior") != "ask":
        raise ValueError("managed allowlist must remain ask-first")
    marketplace = json.loads(
        files["extensions/nddev-builder-marketplace/.junie-extension/marketplace.json"].decode(
            "utf-8"
        )
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


def expect_os_failure(label: str, callback: Any) -> None:
    try:
        callback()
    except OSError:
        return
    raise ValueError(f"{label}: expected OS-level denial")


def hash_bounded_regular_file(
    path: Path,
    expected_info: os.stat_result,
    *,
    max_bytes: int = BOOTSTRAP_SNAPSHOT_MAX_FILE_BYTES,
) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            info.st_dev != expected_info.st_dev
            or info.st_ino != expected_info.st_ino
            or not stat.S_ISREG(info.st_mode)
        ):
            raise ValueError(f"bootstrap snapshot file changed while opening: {path}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"bootstrap snapshot file is too large: {path}")
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def path_identity_snapshot(path: Path) -> tuple[str, tuple[tuple[str, Any], ...]]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "regular"
    else:
        kind = "other"
    item: dict[str, Any] = {
        "name": path.name,
        "device": info.st_dev,
        "inode": info.st_ino,
        "kind": kind,
        "owner": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
        "nlink": info.st_nlink,
        "size": info.st_size,
    }
    if kind == "regular":
        item["sha256"] = hash_bounded_regular_file(path, info)
    return item["name"], tuple(sorted(item.items()))


def real_bootstrap_product_root_snapshot(
    manager: Any,
) -> tuple[Path, bool, tuple[tuple[str, tuple[tuple[str, Any], ...]], ...]]:
    root = manager.bootstrap_lock_product_root_path(manager.bootstrap_lock_system_root())
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return root, False, ()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"real bootstrap product root is not a real directory: {root}")
    snapshot = [path_identity_snapshot(root)]
    children = sorted(root.iterdir(), key=lambda item: item.name)
    if len(children) > BOOTSTRAP_SNAPSHOT_MAX_CHILDREN:
        raise ValueError(f"real bootstrap product root has too many children: {root}")
    for child in children:
        snapshot.append(path_identity_snapshot(child))
    return root, True, tuple(snapshot)


@contextlib.contextmanager
def injected_bootstrap_lock_root(manager: Any):
    real_root, existed_before, entries_before = real_bootstrap_product_root_snapshot(manager)
    original_resolver = manager.bootstrap_lock_system_root
    with tempfile.TemporaryDirectory(prefix="nddev-junie-bootstrap-locks-") as raw:
        system_root = Path(raw) / "system-tmp"
        system_root.mkdir(mode=0o700)
        os.chmod(system_root, 0o1777)

        def fake_system_root() -> Path:
            info = system_root.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("injected bootstrap root must be a real directory")
            mode = stat.S_IMODE(info.st_mode)
            if not (mode & stat.S_ISVTX) or not (mode & 0o002):
                raise ValueError("injected bootstrap root must be sticky and writable")
            return system_root.resolve(strict=True)

        manager.bootstrap_lock_system_root = fake_system_root
        try:
            yield system_root
        finally:
            manager.bootstrap_lock_system_root = original_resolver
    if not existed_before and real_root.exists():
        raise ValueError(f"validator created a real system bootstrap artifact: {real_root}")
    if existed_before:
        _, _, entries_after = real_bootstrap_product_root_snapshot(manager)
        if entries_after != entries_before:
            raise ValueError(f"validator changed real system bootstrap artifacts: {real_root}")


def read_child_message(fd: int, label: str) -> str:
    chunks = bytearray()
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > 8192:
            raise ValueError(f"{label}: child message is too large")
    return chunks.decode("utf-8")


def wait_child_success(pid: int, label: str) -> None:
    waited, status_code = os.waitpid(pid, 0)
    if waited != pid:
        raise ValueError(f"{label}: waited for unexpected child")
    if status_code != 0:
        raise ValueError(f"{label}: child exited with status {status_code}")


def child_write_and_exit(fd: int, message: str, code: int) -> None:
    with contextlib.suppress(OSError):
        os.write(fd, message.encode("utf-8"))
    os._exit(code)


def validate_production_source() -> None:
    source = (ROOT / "cli-tools" / "nddev_junie_cli.py").read_text(encoding="utf-8")
    forbidden_literals = [
        "ALLOW_TEST",
        "TEST_INSTALLER",
        "INSTALLER_URL",
        "INSTALLER_SHA256",
        "UPDATE_INFO_URL",
        "VERIFY_ARTIFACT_SHA256",
        'INSTALL_TIMEOUT_SECONDS"',
        'PROBE_TIMEOUT_SECONDS"',
        "test_override_enabled",
        "env_timeout_seconds",
        "fixture override",
        "artifact fixture",
        "NDDEV_JUNIE_BOOTSTRAP_LOCK_ROOT",
        "JUNIE_BOOTSTRAP_LOCK_ROOT",
        "BOOTSTRAP_LOCK_ROOT_OVERRIDE",
        "LOCK_ROOT_OVERRIDE",
    ]
    present = sorted(literal for literal in forbidden_literals if literal in source)
    if present:
        raise ValueError(
            "production manager exposes test/source/timeout switch literals: " + ", ".join(present)
        )
    if 'os.environ.get("NDDEV_' in source or "os.environ['NDDEV_" in source:
        raise ValueError("production manager must not read NDDEV_* environment overrides")
    if "tempfile.gettempdir" in source or 'os.environ.get("TMPDIR"' in source:
        raise ValueError("bootstrap lock must not derive from ambient temp environment")
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


def validate_bootstrap_lock_adversarial(manager: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-bootstrap-adversarial-") as raw:
        root = Path(raw)
        target = root / "target"
        target.mkdir(mode=0o700)
        canonical = manager.validate_target_identity_for_lock(target)
        system_root = manager.bootstrap_lock_system_root()
        product_root = manager.bootstrap_lock_product_root_path(system_root)

        external = system_root / "external-lock-root"
        external.mkdir(mode=0o700)
        product_root.symlink_to(external)
        try:
            expect_manager_failure(
                "bootstrap symlink product root",
                lambda: manager.target_lock(target, create_parent=False).__enter__(),
            )
        finally:
            product_root.unlink()

        product_root.mkdir(mode=0o700)
        os.chmod(product_root, 0o777)
        try:
            expect_manager_failure(
                "bootstrap shared-mode product root",
                lambda: manager.target_lock(target, create_parent=False).__enter__(),
            )
        finally:
            os.chmod(product_root, 0o700)
            product_root.rmdir()

        product_root = manager.ensure_bootstrap_lock_product_root()
        lock_file = product_root / f"{manager.bootstrap_lock_digest(canonical)}.lock"
        lock_file.symlink_to(external)
        try:
            expect_manager_failure(
                "bootstrap symlink lock file",
                lambda: manager.target_lock(target, create_parent=False).__enter__(),
            )
        finally:
            lock_file.unlink()

        lock_file.write_bytes(b"{}\n")
        os.chmod(lock_file, 0o644)
        try:
            expect_manager_failure(
                "bootstrap shared-mode lock file",
                lambda: manager.target_lock(target, create_parent=False).__enter__(),
            )
        finally:
            os.chmod(lock_file, 0o600)
            lock_file.unlink()

        bad_binding = dict(manager.bootstrap_lock_binding(canonical))
        bad_binding["canonical_target"] = str(canonical.parent / "other-target")
        lock_file.write_bytes(manager.canonical_json(bad_binding))
        os.chmod(lock_file, 0o600)
        try:
            expect_manager_failure(
                "bootstrap lock binding mismatch",
                lambda: manager.target_lock(target, create_parent=False).__enter__(),
            )
        finally:
            lock_file.unlink()

        with manager.target_lock(target, create_parent=False):
            if not lock_file.is_file():
                raise ValueError("bootstrap lock file was not materialized")
            if stat.S_IMODE(lock_file.stat().st_mode) != manager.OWNER_FILE_MODE:
                raise ValueError("bootstrap lock file mode is not 0600")
        if not lock_file.is_file():
            raise ValueError("bootstrap lock file was unlinked on normal release")
        binding = json.loads(lock_file.read_text(encoding="utf-8"))
        if binding != manager.bootstrap_lock_binding(canonical):
            raise ValueError("bootstrap lock binding changed after release")


def validate_bootstrap_lock_persistent_handover(manager: Any) -> None:
    if not hasattr(os, "fork"):
        raise ValueError("bootstrap lock handover regression requires POSIX fork")
    with tempfile.TemporaryDirectory(prefix="nddev-junie-bootstrap-handover-") as raw:
        target = Path(raw) / "target"
        target.mkdir(mode=0o700)
        canonical = manager.validate_target_identity_for_lock(target)
        lock_path = manager.bootstrap_lock_path(canonical)

        def spawn_holder(label: str) -> tuple[int, int, int]:
            ready_read, ready_write = os.pipe()
            release_read, release_write = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(ready_read)
                os.close(release_write)
                handle = None
                try:
                    handle = manager.acquire_bootstrap_lock(canonical)
                    info = lock_path.stat()
                    os.write(
                        ready_write,
                        json.dumps({"inode": info.st_ino, "device": info.st_dev}).encode("utf-8"),
                    )
                except Exception as exc:
                    child_write_and_exit(ready_write, f"ERROR {label}: {exc}", 1)
                finally:
                    with contextlib.suppress(OSError):
                        os.close(ready_write)
                try:
                    os.read(release_read, 1)
                finally:
                    if handle is not None:
                        manager.release_bootstrap_lock(handle)
                    os._exit(0)
            os.close(ready_write)
            os.close(release_read)
            return pid, ready_read, release_write

        first_pid, first_ready, first_release = spawn_holder("first")
        first_message = read_child_message(first_ready, "first holder")
        os.close(first_ready)
        first_identity = json.loads(first_message)
        os.write(first_release, b"x")
        os.close(first_release)
        wait_child_success(first_pid, "first holder")

        second_pid, second_ready, second_release = spawn_holder("second")
        second_message = read_child_message(second_ready, "second holder")
        os.close(second_ready)
        try:
            second_identity = json.loads(second_message)
            if second_identity != first_identity:
                raise ValueError("bootstrap lock inode changed across handover")

            contender_read, contender_write = os.pipe()
            contender_pid = os.fork()
            if contender_pid == 0:
                os.close(contender_read)
                try:
                    handle = manager.acquire_bootstrap_lock(canonical)
                except Exception as exc:
                    if exc.__class__.__name__ == "JunieCliSetupError":
                        child_write_and_exit(contender_write, "blocked", 0)
                    child_write_and_exit(contender_write, f"ERROR contender: {exc}", 1)
                else:
                    manager.release_bootstrap_lock(handle)
                    child_write_and_exit(contender_write, "acquired", 2)
            os.close(contender_write)
            contender_message = read_child_message(contender_read, "contender")
            os.close(contender_read)
            wait_child_success(contender_pid, "contender")
            if contender_message != "blocked":
                raise ValueError("third process acquired bootstrap lock while second held it")
        finally:
            with contextlib.suppress(OSError):
                os.write(second_release, b"x")
            with contextlib.suppress(OSError):
                os.close(second_release)
            wait_child_success(second_pid, "second holder")
        final_info = lock_path.stat()
        if (
            final_info.st_ino != first_identity["inode"]
            or final_info.st_dev != first_identity["device"]
        ):
            raise ValueError("bootstrap lock inode changed after all releases")


def validate_dual_lock_persistent_handover(manager: Any) -> None:
    if not hasattr(os, "fork"):
        raise ValueError("dual lock handover regression requires POSIX fork")
    with tempfile.TemporaryDirectory(prefix="nddev-junie-dual-lock-handover-") as raw:
        target = Path(raw) / "target"
        target.mkdir(mode=0o700)
        canonical = manager.validate_target_identity_for_lock(target)
        internal_file = canonical / manager.LOCK_DIR_NAME / manager.LOCK_FILE_NAME
        external_file = manager.bootstrap_lock_path(canonical)

        def spawn_holder(label: str) -> tuple[int, int, int]:
            ready_read, ready_write = os.pipe()
            release_read, release_write = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(ready_read)
                os.close(release_write)
                try:
                    with manager.target_lock(canonical, create_parent=False):
                        identity = {
                            "external": {
                                "inode": external_file.stat().st_ino,
                                "device": external_file.stat().st_dev,
                            },
                            "internal": {
                                "inode": internal_file.stat().st_ino,
                                "device": internal_file.stat().st_dev,
                            },
                        }
                        os.write(ready_write, json.dumps(identity).encode("utf-8"))
                        os.close(ready_write)
                        os.read(release_read, 1)
                except Exception as exc:
                    child_write_and_exit(ready_write, f"ERROR {label}: {exc}", 1)
                finally:
                    with contextlib.suppress(OSError):
                        os.close(ready_write)
                    with contextlib.suppress(OSError):
                        os.close(release_read)
                os._exit(0)
            os.close(ready_write)
            os.close(release_read)
            return pid, ready_read, release_write

        first_pid, first_ready, first_release = spawn_holder("first")
        first_identity = json.loads(read_child_message(first_ready, "first dual holder"))
        os.close(first_ready)
        os.write(first_release, b"x")
        os.close(first_release)
        wait_child_success(first_pid, "first dual holder")

        second_pid, second_ready, second_release = spawn_holder("second")
        second_identity = json.loads(read_child_message(second_ready, "second dual holder"))
        os.close(second_ready)
        try:
            if second_identity != first_identity:
                raise ValueError("dual lock inode changed across handover")

            contender_read, contender_write = os.pipe()
            contender_pid = os.fork()
            if contender_pid == 0:
                os.close(contender_read)
                try:
                    with manager.target_lock(canonical, create_parent=False):
                        child_write_and_exit(contender_write, "acquired", 2)
                except Exception as exc:
                    if exc.__class__.__name__ == "JunieCliSetupError":
                        child_write_and_exit(contender_write, "blocked", 0)
                    child_write_and_exit(contender_write, f"ERROR contender: {exc}", 1)
            os.close(contender_write)
            contender_message = read_child_message(contender_read, "dual contender")
            os.close(contender_read)
            wait_child_success(contender_pid, "dual contender")
            if contender_message != "blocked":
                raise ValueError("third process acquired dual lock while second held it")
        finally:
            with contextlib.suppress(OSError):
                os.write(second_release, b"x")
            with contextlib.suppress(OSError):
                os.close(second_release)
            wait_child_success(second_pid, "second dual holder")
        if not internal_file.is_file() or not external_file.is_file():
            raise ValueError("persistent dual lock files were removed after handover")
        final_identity = {
            "external": {
                "inode": external_file.stat().st_ino,
                "device": external_file.stat().st_dev,
            },
            "internal": {
                "inode": internal_file.stat().st_ino,
                "device": internal_file.stat().st_dev,
            },
        }
        if final_identity != first_identity:
            raise ValueError("dual lock inode changed after all releases")


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

        bad_lock = root / "bad-lock"
        bad_lock.mkdir(mode=0o700)
        bad_lock_parent = bad_lock / manager.LOCK_DIR_NAME
        bad_lock_parent.mkdir(mode=0o777)
        try:
            expect_manager_failure(
                "shared-mode lock parent",
                lambda: manager.target_lock(bad_lock, create_parent=False).__enter__(),
            )
        finally:
            os.chmod(bad_lock_parent, 0o700)

        stale_lock = root / "stale-lock"
        stale_lock.mkdir(mode=0o700)
        stale_parent = stale_lock / manager.LOCK_DIR_NAME
        stale_parent.mkdir(mode=0o700)
        stale_file = stale_parent / manager.LOCK_FILE_NAME
        stale_file.write_bytes(b"stale\n")
        os.chmod(stale_file, 0o600)
        os.chmod(stale_parent, 0o500)
        with manager.target_lock(stale_lock, create_parent=False):
            if not stale_file.is_file():
                raise ValueError("stale flock file was not recovered")
        if not stale_file.is_file() or stat.S_IMODE(stale_parent.stat().st_mode) != 0o700:
            raise ValueError("recovered stale internal lock was not preserved safely")

        prelocked = root / "prelocked"
        prelocked.mkdir(mode=0o700)
        lock_context = manager.target_lock(prelocked, create_parent=False)
        with lock_context:
            expect_manager_failure(
                "already flocked target",
                lambda: manager.target_lock(prelocked, create_parent=False).__enter__(),
            )
            if not (prelocked / manager.LOCK_DIR_NAME / manager.LOCK_FILE_NAME).is_file():
                raise ValueError("active flock file is missing")

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
        manager.safe_rmtree_private_directory(canonical, "sticky-temp validator target")


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


def fake_launch_runtime_metadata(
    manager: Any,
    target: Path,
    *,
    shim_bytes: bytes = b"#!/bin/sh\nexit 0\n",
) -> dict[str, Any]:
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
    shim.write_bytes(shim_bytes)
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
        launch_image = manager.runtime_launch_image(canonical)
        launch_image_bin = manager.runtime_launch_image_bin(canonical)

        def run() -> None:
            manager.write_setup(canonical, setup, safe)
            original_popen = manager.subprocess.Popen
            original_snapshot = manager.snapshot_live_junie_home
            original_guard = manager.require_live_junie_home_unchanged
            seen = {"child": False, "guard": False}

            class Process:
                def wait(self) -> int:
                    return 0

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

            def fake_popen(
                command: list[str],
                *,
                cwd: Path,
                env: dict[str, str],
            ) -> Process:
                seen["child"] = True
                if cwd != canonical:
                    raise ValueError("launch child cwd escaped target")
                if Path(command[0]) != launch_image_bin:
                    raise ValueError("launch did not execute the target-owned launch image")
                if env["HOME"] != str(manager.runtime_home(canonical)):
                    raise ValueError("launch child HOME escaped runtime home")
                if not lock.is_dir():
                    raise ValueError("child did not run under the target lock")
                if stat.S_IMODE(lock.stat().st_mode) != manager.OWNER_READ_EXECUTE_DIRECTORY_MODE:
                    raise ValueError("lock parent was not write-protected")
                if stat.S_IMODE(launch_image.stat().st_mode) != (
                    manager.OWNER_READ_EXECUTE_DIRECTORY_MODE
                ):
                    raise ValueError("launch image directory was not write-protected")
                expect_os_failure(
                    "child lock file unlink",
                    lambda: (lock / manager.LOCK_FILE_NAME).unlink(),
                )
                expect_os_failure("child lock parent rmdir", lambda: lock.rmdir())
                expect_os_failure("child launcher unlink", lambda: launch_image_bin.unlink())
                replacement = manager.runtime_tmp(canonical) / "replacement"
                replacement.write_text("replacement\n", encoding="utf-8")
                expect_os_failure(
                    "child launcher replace",
                    lambda: replacement.replace(launch_image_bin),
                )
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
                if stat.S_IMODE(lock.stat().st_mode) != manager.OWNER_READ_EXECUTE_DIRECTORY_MODE:
                    raise ValueError("blocked mutation weakened the launch lock parent")
                return Process()

            manager.subprocess.Popen = fake_popen
            manager.snapshot_live_junie_home = fake_snapshot
            manager.require_live_junie_home_unchanged = fake_guard
            try:
                result = manager.launch(canonical, ["--version"])
            finally:
                manager.subprocess.Popen = original_popen
                manager.snapshot_live_junie_home = original_snapshot
                manager.require_live_junie_home_unchanged = original_guard
            if result != 0 or not seen["child"] or not seen["guard"]:
                raise ValueError("launch lock regression did not execute the child and guard")
            if not (lock / manager.LOCK_FILE_NAME).is_file():
                raise ValueError("persistent launch lock file was removed after child completion")
            if stat.S_IMODE(lock.stat().st_mode) != manager.OWNER_DIRECTORY_MODE:
                raise ValueError("persistent launch lock parent mode was not restored")
            switched = manager.write_setup(canonical, setup, full_auto, require_existing=True)
            if switched["profile_id"] != "full-auto":
                raise ValueError("lifecycle mutation remained blocked after launch completed")

        with_fake_software(manager, run, metadata=metadata)


def validate_external_lock_survives_internal_lock_rename(manager: Any) -> None:
    if not hasattr(os, "fork"):
        raise ValueError("internal lock rename regression requires POSIX fork")
    with tempfile.TemporaryDirectory(prefix="nddev-junie-internal-lock-rename-") as raw:
        target = Path(raw) / "target"
        target.mkdir(mode=0o700)
        canonical = manager.validate_target(target, create=False)
        metadata = fake_launch_runtime_metadata(manager, canonical)
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        safe = manager.load_profile("safe")
        full_auto = manager.load_profile("full-auto")
        lock = canonical / manager.LOCK_DIR_NAME
        renamed_lock = canonical / f"{manager.LOCK_DIR_NAME}.renamed"

        def run() -> None:
            manager.write_setup(canonical, setup, safe)
            ready_read, ready_write = os.pipe()
            release_read, release_write = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(ready_read)
                os.close(release_write)
                original_popen = manager.subprocess.Popen
                original_snapshot = manager.snapshot_live_junie_home
                original_guard = manager.require_live_junie_home_unchanged

                class Process:
                    def wait(self) -> int:
                        os.read(release_read, 1)
                        if renamed_lock.exists() and not lock.exists():
                            renamed_lock.rename(lock)
                        return 0

                def fake_popen(
                    command: list[str],
                    *,
                    cwd: Path,
                    env: dict[str, str],
                ) -> Process:
                    if cwd != canonical or env["HOME"] != str(manager.runtime_home(canonical)):
                        raise ValueError("launch child escaped the managed target")
                    if not lock.is_dir():
                        raise ValueError("internal lock was missing before child rename")
                    lock.rename(renamed_lock)
                    os.write(ready_write, b"renamed")
                    os.close(ready_write)
                    return Process()

                manager.subprocess.Popen = fake_popen
                manager.snapshot_live_junie_home = lambda: "validator-live-home"
                manager.require_live_junie_home_unchanged = lambda snapshot, *, context: None
                try:
                    result = manager.launch(canonical, ["--version"])
                    if result != 0:
                        raise ValueError(f"launch returned {result}")
                except Exception as exc:
                    child_write_and_exit(ready_write, f"ERROR launch: {exc}", 1)
                finally:
                    manager.subprocess.Popen = original_popen
                    manager.snapshot_live_junie_home = original_snapshot
                    manager.require_live_junie_home_unchanged = original_guard
                    with contextlib.suppress(OSError):
                        os.close(release_read)
                os._exit(0)

            os.close(ready_write)
            os.close(release_read)
            try:
                message = read_child_message(ready_read, "internal lock rename child")
                if message != "renamed":
                    raise ValueError(message or "internal lock rename child did not signal")
                if lock.exists() or not renamed_lock.is_dir():
                    raise ValueError("internal lock parent was not renamed during launch")
                expect_manager_failure(
                    "switch while internal lock parent renamed",
                    lambda: manager.write_setup(
                        canonical,
                        setup,
                        full_auto,
                        require_existing=True,
                    ),
                )
                expect_manager_failure(
                    "remove while internal lock parent renamed",
                    lambda: manager.remove_setup(canonical),
                )
                expect_manager_failure(
                    "install while internal lock parent renamed",
                    lambda: manager.write_setup(canonical, setup, safe),
                )
            finally:
                with contextlib.suppress(OSError):
                    os.write(release_write, b"x")
                with contextlib.suppress(OSError):
                    os.close(release_write)
                os.close(ready_read)
                wait_child_success(pid, "internal lock rename launch")
            if renamed_lock.exists():
                raise ValueError("renamed internal lock parent was not restored")
            if not (lock / manager.LOCK_FILE_NAME).is_file():
                raise ValueError("persistent internal lock file was removed after launch")

        with_fake_software(manager, run, metadata=metadata)


def validate_verified_launcher_handoff(manager: Any) -> None:
    script = b"""#!/bin/sh
printf 'home' > "$HOME/guard-home" || exit 31
printf 'tmp' > "$TMPDIR/guard-tmp" || exit 32
printf 'config' > "$XDG_CONFIG_HOME/guard-config" || exit 33
printf 'cache' > "$XDG_CACHE_HOME/guard-cache" || exit 34
printf 'state' > "$XDG_STATE_HOME/guard-state" || exit 35
printf 'data' > "$JUNIE_DATA/guard-data" || exit 36
printf 'log' > "$JUNIE_LOG_DIR/guard-log" || exit 37
printf 'project' > "./guard-project" || exit 38
rm "$0" 2>/dev/null && exit 40
printf 'replace\n' > "$TMPDIR/replacement" || exit 41
mv "$TMPDIR/replacement" "$0" 2>/dev/null && exit 42
exit 23
"""
    with tempfile.TemporaryDirectory(prefix="nddev-junie-launch-handoff-") as raw:
        target = Path(raw) / "target"
        target.mkdir(mode=0o700)
        canonical = manager.validate_target(target, create=False)
        metadata = fake_launch_runtime_metadata(manager, canonical, shim_bytes=script)
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        safe = manager.load_profile("safe")

        def run() -> None:
            manager.write_setup(canonical, setup, safe)
            original_snapshot = manager.snapshot_live_junie_home
            original_guard = manager.require_live_junie_home_unchanged
            manager.snapshot_live_junie_home = lambda: "validator-live-home"
            manager.require_live_junie_home_unchanged = lambda snapshot, *, context: None
            try:
                result = manager.launch(canonical, ["--version"])
            finally:
                manager.snapshot_live_junie_home = original_snapshot
                manager.require_live_junie_home_unchanged = original_guard
            if result != 23:
                raise ValueError("verified launcher did not run under path protection")
            if manager.runtime_bin(canonical).read_bytes() != script:
                raise ValueError("source shim was replaced during launch")
            if manager.runtime_launch_image_bin(canonical).read_bytes() != script:
                raise ValueError("launch image path was replaced during launch")
            expected_writes = {
                manager.runtime_home(canonical) / "guard-home": b"home",
                manager.runtime_tmp(canonical) / "guard-tmp": b"tmp",
                manager.runtime_home(canonical) / ".config" / "guard-config": b"config",
                manager.runtime_home(canonical) / ".cache" / "guard-cache": b"cache",
                manager.runtime_home(canonical) / ".local" / "state" / "guard-state": b"state",
                manager.runtime_data(canonical) / "guard-data": b"data",
                manager.runtime_data(canonical) / "logs" / "guard-log": b"log",
                canonical / "guard-project": b"project",
            }
            for path, expected in expected_writes.items():
                if path.read_bytes() != expected:
                    raise ValueError(f"launched stub could not write runtime state: {path}")
            writable_directories = (
                manager.runtime_root(canonical),
                manager.runtime_home(canonical),
                manager.runtime_home(canonical) / ".local",
                manager.runtime_home(canonical) / ".local" / "bin",
                manager.runtime_home(canonical) / ".local" / "share",
                manager.runtime_data(canonical),
                manager.runtime_data(canonical) / "versions",
                manager.runtime_data(canonical) / "versions" / "validator",
                manager.runtime_data(canonical) / "logs",
                manager.runtime_cache(canonical),
                manager.runtime_tmp(canonical),
                manager.runtime_home(canonical) / ".config",
                manager.runtime_home(canonical) / ".cache",
                manager.runtime_home(canonical) / ".local" / "state",
            )
            for directory in writable_directories:
                if not (stat.S_IMODE(directory.stat().st_mode) & stat.S_IWUSR):
                    raise ValueError(f"runtime directory was left unwritable: {directory}")
            if stat.S_IMODE(manager.runtime_launch_image(canonical).stat().st_mode) != (
                manager.OWNER_DIRECTORY_MODE
            ):
                raise ValueError("launch image directory mode was not restored")

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
        manager.MANAGED_BEGIN + "\n# Legacy NDDev Junie CLI Setup\n" + manager.MANAGED_END + "\n"
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
    if contract["safety"].get("lock_uses_fcntl_flock") is not True:
        raise ValueError("contract must declare fcntl flock locking")
    if contract["safety"].get("external_bootstrap_lock_outside_target") is not True:
        raise ValueError("contract must declare external bootstrap lock outside target")
    if contract["safety"].get("external_bootstrap_lock_not_in_child_env") is not True:
        raise ValueError("contract must declare bootstrap lock is not exposed to child env")
    if "never ambient TMPDIR" not in contract["safety"].get(
        "external_bootstrap_lock_fixed_system_temp", ""
    ):
        raise ValueError("contract must declare fixed system temp bootstrap lock root")
    if contract["safety"].get("launch_write_protects_verified_executable_paths") is not True:
        raise ValueError("contract must declare verified executable path protection")
    if contract["safety"].get("launch_keeps_runtime_state_writable") is not True:
        raise ValueError("contract must declare writable runtime state during launch")
    if contract["managed_state"].get("lock_file") != "<target>/.nddev-junie-cli.lock/lock":
        raise ValueError("contract must declare the target lock file")
    if (
        contract["managed_state"].get("bootstrap_lock_root")
        != "<resolved fixed system temp>/nddev-junie-cli-app-bootstrap-locks-uid-<uid>"
    ):
        raise ValueError("contract must declare the bootstrap lock root")
    if (
        contract["managed_state"].get("bootstrap_lock_file")
        != "sha256(product namespace plus canonical absolute target).lock"
    ):
        raise ValueError("contract must declare the bootstrap lock file binding")
    if contract["managed_state"].get("bootstrap_lock_file_mode") != "0600":
        raise ValueError("contract must declare bootstrap lock file mode")
    if contract["managed_state"].get("bootstrap_lock_persistent") is not True:
        raise ValueError("contract must declare persistent bootstrap lock files")
    if contract["managed_state"].get("target_lock_persistent") is not True:
        raise ValueError("contract must declare persistent target lock files")
    if (
        contract["managed_state"].get("launch_image")
        != "<target>/.nddev-junie-cli-runtime/launch-image/junie"
    ):
        raise ValueError("contract must declare the target launch image")
    if contract["managed_state"].get("lock_file_mode") != "0600":
        raise ValueError("contract must declare target lock file mode")
    if contract["managed_state"].get("lock_parent_held_mode") != "0500":
        raise ValueError("contract must declare held lock parent mode")
    if "lock_scope" not in contract.get("runtime_launch", {}):
        raise ValueError("contract runtime_launch must describe launch lock scope")
    if "pre_child_artifact_revalidation" not in contract.get("runtime_launch", {}):
        raise ValueError("contract runtime_launch must describe artifact revalidation")
    if "verified_path_handoff" not in contract.get("runtime_launch", {}):
        raise ValueError("contract runtime_launch must describe verified path handoff")
    if "runtime_state_writable" not in contract.get("runtime_launch", {}):
        raise ValueError("contract runtime_launch must describe writable runtime state")
    if "tamper_boundary" not in contract.get("runtime_launch", {}):
        raise ValueError("contract runtime_launch must describe tamper boundary")
    if "launch_lock" not in manifest.get("runtime_isolation", {}):
        raise ValueError("manifest runtime_isolation must describe launch lock scope")
    launch_lock_text = manifest.get("runtime_isolation", {}).get("launch_lock", "")
    if (
        "external bootstrap" not in launch_lock_text
        or "persistent target-internal" not in launch_lock_text
    ):
        raise ValueError("manifest launch lock must describe persistent dual locking")
    if "pre_child_artifact_revalidation" not in manifest.get("runtime_isolation", {}):
        raise ValueError("manifest runtime_isolation must describe artifact revalidation")
    if "verified_path_handoff" not in manifest.get("runtime_isolation", {}):
        raise ValueError("manifest runtime_isolation must describe verified path handoff")
    if "runtime_state_writable" not in manifest.get("runtime_isolation", {}):
        raise ValueError("manifest runtime_isolation must describe writable runtime state")
    if "same_uid_boundary" not in manifest.get("runtime_isolation", {}):
        raise ValueError("manifest runtime_isolation must describe same-UID boundary")
    if build.get("junie_cli_tested") != baseline["release"]["stable_version"]:
        raise ValueError("tested Junie CLI version differs from baseline release")
    validate_baseline(baseline, contract, manifest)
    validate_required_files()
    validate_claude_import_bridge()
    manager = load_manager()
    validate_platform_detection(manager)
    validate_production_source()
    with injected_bootstrap_lock_root(manager):
        validate_generated_files(manager, version)
        validate_builder_projection_invariant(manager)
        validate_manager_parse(manager)
        validate_bootstrap_lock_adversarial(manager)
        validate_bootstrap_lock_persistent_handover(manager)
        validate_dual_lock_persistent_handover(manager)
        validate_adversarial_smokes(manager)
        validate_profile_switch_lifecycle(manager)
        validate_launch_lock_concurrency(manager)
        validate_external_lock_survives_internal_lock_rename(manager)
        validate_verified_launcher_handoff(manager)
        validate_legacy_mapping_migration(manager)
        validate_restore_backup_fail_closed(manager)
    validate_workflows()
    validate_release_workflow(version, contract, manifest)
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"validate_public_contracts.py: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
