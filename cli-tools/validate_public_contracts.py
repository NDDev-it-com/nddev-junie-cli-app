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
import shutil
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
PYTHON_REQUIRES = ">=3.9"
REQUIRED_BUILD_VERSION_KEYS = {
    "schema_version",
    "build_version",
    "junie_cli_channel",
    "junie_cli_tested",
    "nddev_builder_projection_version",
    "python_requires",
    "runtime_baseline_ref",
    "shared_ci",
}
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
    "windows-aarch64",
    "windows-amd64",
]
OFFICIAL_ARTIFACT_PLATFORMS = set(OFFICIAL_ARTIFACT_PLATFORM_ORDER)
UNSUPPORTED_OFFICIAL_ARTIFACT_PLATFORMS = {
    "windows": ["windows-aarch64", "windows-amd64"],
}
WINDOWS_OFFICIAL_ARTIFACT_OBSERVATIONS = {
    "windows-aarch64": {
        "size": 301279985,
        "sha256": "51e69bfadd097f657a58a47a9757626fc9627b312451e2ff25643d310c601e28",
    },
    "windows-amd64": {
        "size": 267185689,
        "sha256": "bddf9833c15a8fe2e9afba43315e7a1333a49d01cefd4e5c93822170d40de07d",
    },
}
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
    for label, owner in (
        ("baseline release", release),
        ("contract runtime_compatibility", runtime),
        ("manifest", manifest),
    ):
        if owner.get("unsupported_official_artifact_platforms") != (
            UNSUPPORTED_OFFICIAL_ARTIFACT_PLATFORMS
        ):
            raise ValueError(f"{label}: unsupported official artifact map is not synchronized")
    if set(release["exact_artifacts"]) != OFFICIAL_ARTIFACT_PLATFORMS:
        raise ValueError("baseline must preserve every official artifact key")
    if set(release["exact_artifact_hashes"]) != OFFICIAL_ARTIFACT_PLATFORMS:
        raise ValueError("baseline exact hashes must preserve every official artifact key")


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
        if platform_id not in OFFICIAL_ARTIFACT_PLATFORMS:
            raise ValueError("unknown official artifact platform is out of scope")
        if platform_id in WINDOWS_OFFICIAL_ARTIFACT_OBSERVATIONS:
            expected = WINDOWS_OFFICIAL_ARTIFACT_OBSERVATIONS[platform_id]
            if artifact["sha256"] != expected["sha256"] or artifact["size"] != expected["size"]:
                raise ValueError(f"{platform_id}: official Windows observation differs")
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
    reader_marker = object()
    original_reader = getattr(manager.platform, "freedesktop_os_release", reader_marker)
    original_paths = manager.OS_RELEASE_PATHS
    with tempfile.TemporaryDirectory(prefix="nddev-junie-os-release-") as raw:
        os_release = Path(raw) / "os-release"
        os_release.write_text(
            'NAME="Ubuntu Server"\nID=ubuntu\nVERSION_ID="24.04"\n',
            encoding="utf-8",
        )
        manager.OS_RELEASE_PATHS = (os_release,)
        with contextlib.suppress(AttributeError):
            delattr(manager.platform, "freedesktop_os_release")
        try:
            detected = manager.current_host_id(
                system="Linux",
                machine="x86_64",
                libc=("glibc", "2.39"),
            )
        finally:
            manager.OS_RELEASE_PATHS = original_paths
            if original_reader is not reader_marker:
                manager.platform.freedesktop_os_release = original_reader
        if detected != "ubuntu-glibc-x64":
            raise ValueError("Python 3.9 os-release fallback did not accept Ubuntu/glibc x64")


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
    manager.parse_args(["update", "--target", "/tmp/target"])
    manager.parse_args(
        ["switch", "--setup", "nddev-builder", "--profile", "safe", "--target", "/tmp/target"]
    )
    manager.parse_args(["migrate", "--target", "/tmp/target"])
    manager.parse_args(
        ["migrate", "--setup", "nddev-builder", "--profile", "full-auto", "--target", "/tmp/target"]
    )
    manager.parse_args(["software-status", "--target", "/tmp/target"])
    manager.parse_args(["install-cli", "--target", "/tmp/target"])
    manager.parse_args(["update-cli", "--target", "/tmp/target"])
    manager.parse_args(["remove-cli", "--target", "/tmp/target"])
    manager.parse_args(["launch", "--target", "/tmp/target", "--", "--version"])


def validate_json_argument_boundary(manager: Any) -> None:
    for label, argv in {
        "invalid choice": ["does-not-exist", "--json"],
        "missing target": ["status", "--json"],
        "bad backup": ["restore", "--backup", "bad", "--target", "/tmp/target", "--json"],
        "update identity override": [
            "update",
            "--profile",
            "safe",
            "--target",
            "/tmp/target",
            "--json",
        ],
    }.items():
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = manager.main(argv)
        if code != 2:
            raise ValueError(f"{label}: expected rc 2, got {code}")
        if stderr.getvalue():
            raise ValueError(f"{label}: JSON parse boundary wrote usage stderr")
        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        if len(lines) != 1:
            raise ValueError(f"{label}: expected exactly one JSON error line")
        payload = json.loads(lines[0])
        if sorted(payload) != ["error"] or not isinstance(payload["error"], str):
            raise ValueError(f"{label}: JSON error payload is invalid")


def validate_unsupported_host_preflight(manager: Any) -> None:
    commands = {
        "status": ["status", "--target"],
        "plan": ["plan", "--target"],
        "install": ["install", "--target"],
        "switch": ["switch", "--profile", "safe", "--target"],
        "update": ["update", "--target"],
        "migrate": ["migrate", "--target"],
        "restore": ["restore", "--backup", "0", "--target"],
        "remove": ["remove", "--target"],
        "software-status": ["software-status", "--target"],
        "install-cli": ["install-cli", "--target"],
        "update-cli": ["update-cli", "--target"],
        "remove-cli": ["remove-cli", "--target"],
        "launch": ["launch", "--target"],
    }
    unsupported = {
        "windows": {
            "system": "Windows",
            "machine": "AMD64",
            "os_release": {},
            "libc": ("", ""),
        },
        "debian": {
            "system": "Linux",
            "machine": "x86_64",
            "os_release": {"ID": "debian"},
            "libc": ("glibc", "2.36"),
        },
        "alpine": {
            "system": "Linux",
            "machine": "x86_64",
            "os_release": {"ID": "alpine"},
            "libc": ("musl", "1.2.5"),
        },
    }
    original_system = manager.platform.system
    original_machine = manager.platform.machine
    original_os_release = getattr(manager.platform, "freedesktop_os_release", None)
    original_libc = manager.platform.libc_ver
    original_urlopen = manager.urllib.request.urlopen
    try:
        for platform_label, model in unsupported.items():
            manager.platform.system = lambda model=model: model["system"]
            manager.platform.machine = lambda model=model: model["machine"]
            manager.platform.freedesktop_os_release = lambda model=model: dict(model["os_release"])
            manager.platform.libc_ver = lambda model=model: model["libc"]
            network_calls: list[str] = []

            def blocked_urlopen(*args: Any, **kwargs: Any) -> Any:
                network_calls.append(repr(args[0] if args else ""))
                raise AssertionError("unsupported host reached network")

            manager.urllib.request.urlopen = blocked_urlopen
            for command_label, prefix in commands.items():
                with tempfile.TemporaryDirectory(
                    prefix=f"nddev-junie-unsupported-{platform_label}-{command_label}-"
                ) as raw:
                    root = Path(raw)
                    target = root / "missing-parent" / "target"
                    argv = [*prefix, str(target), "--json"]
                    if command_label == "launch":
                        argv.extend(["--", "--version"])
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    product_root = manager.bootstrap_lock_product_root_path(
                        manager.bootstrap_lock_system_root()
                    )
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        code = manager.main(argv)
                    if code != 2:
                        raise ValueError(
                            f"{platform_label} {command_label}: expected preflight rc 2"
                        )
                    if stderr.getvalue():
                        raise ValueError(
                            f"{platform_label} {command_label}: preflight wrote stderr"
                        )
                    payload = json.loads(stdout.getvalue())
                    if sorted(payload) != ["error"]:
                        raise ValueError(
                            f"{platform_label} {command_label}: invalid JSON error payload"
                        )
                    if target.parent.exists():
                        raise ValueError(
                            f"{platform_label} {command_label}: target parent was inspected/created"
                        )
                    if product_root.exists():
                        raise ValueError(
                            f"{platform_label} {command_label}: bootstrap lock root was created"
                        )
            if network_calls:
                raise ValueError(f"{platform_label}: unsupported preflight reached network")
    finally:
        manager.platform.system = original_system
        manager.platform.machine = original_machine
        if original_os_release is None:
            with contextlib.suppress(AttributeError):
                delattr(manager.platform, "freedesktop_os_release")
        else:
            manager.platform.freedesktop_os_release = original_os_release
        manager.platform.libc_ver = original_libc
        manager.urllib.request.urlopen = original_urlopen


def validate_target_observation_after_seeded_product_lock(manager: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-lock-order-") as raw:
        root = Path(raw)
        target = root / "missing-parent" / "target"
        product_root = manager.bootstrap_lock_product_root_path(
            manager.bootstrap_lock_system_root()
        )
        product = manager.open_bootstrap_global_lock(create=True, exclusive=True)
        manager.release_bootstrap_global_lock(product)
        product_coordination_entered = {"value": False}
        original_resolver = manager.resolve_parent_allowing_missing
        original_stat_existing = manager.stat_existing
        original_open_product = manager.open_bootstrap_global_lock

        def guarded_open_product(*args: Any, **kwargs: Any) -> Any:
            handle = original_open_product(*args, **kwargs)
            if handle is not None:
                product_coordination_entered["value"] = True
            return handle

        def guarded_resolver(path: Path) -> Path:
            if path == target.parent and not product_coordination_entered["value"]:
                raise AssertionError("target parent resolved before seeded product coordination")
            return original_resolver(path)

        def guarded_stat(path: Path, label: str) -> os.stat_result | None:
            if label.startswith("target") and not product_coordination_entered["value"]:
                raise AssertionError(f"{label} was inspected before seeded product coordination")
            return original_stat_existing(path, label)

        manager.open_bootstrap_global_lock = guarded_open_product
        manager.resolve_parent_allowing_missing = guarded_resolver
        manager.stat_existing = guarded_stat
        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = manager.main(["status", "--target", str(target), "--json"])
            if code != 0:
                raise ValueError(f"status under lexical-order guard failed: {stdout.getvalue()}")
            if stderr.getvalue():
                raise ValueError("status under lexical-order guard wrote stderr")
            if target.parent.exists():
                raise ValueError("seeded product-coordination status created the target parent")
        finally:
            manager.open_bootstrap_global_lock = original_open_product
            manager.resolve_parent_allowing_missing = original_resolver
            manager.stat_existing = original_stat_existing
            if product_root.exists():
                shutil.rmtree(product_root)


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


def manager_main_json(manager: Any, argv: list[str], *, expected: int = 0) -> dict[str, Any]:
    args = list(argv)
    if "--json" not in args:
        args.append("--json")
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = manager.main(args)
    if code != expected:
        raise ValueError(
            f"manager returned {code}, expected {expected}: {args!r}; "
            f"stdout={stdout.getvalue()!r} stderr={stderr.getvalue()!r}"
        )
    if stderr.getvalue():
        raise ValueError(f"manager wrote stderr for {args!r}: {stderr.getvalue()!r}")
    try:
        payload = json.loads(stdout.getvalue())
    except json.JSONDecodeError as exc:
        raise ValueError(f"manager did not emit JSON for {args!r}: {stdout.getvalue()!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError("manager JSON payload must be an object")
    return payload


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
    if "os.link" in source or "linkat" in source:
        raise ValueError("bootstrap anchor publication must not use hard-link aliases")
    publication_source = source.split("def atomic_rename_no_replace", 1)[-1].split(
        "def lock_file_path", 1
    )[0]
    if "os.replace" in publication_source:
        raise ValueError("bootstrap anchor publication must not replace final anchors")


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


def validate_bootstrap_lock_atomic_publication(manager: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-bootstrap-atomic-") as raw:
        root = Path(raw)
        product_root = manager.bootstrap_lock_product_root_path(
            manager.bootstrap_lock_system_root()
        )
        product = manager.open_bootstrap_global_lock(create=True, exclusive=True)
        manager.release_bootstrap_global_lock(product)
        product_anchor = product_root / manager.BOOTSTRAP_GLOBAL_LOCK_NAME
        product_anchor_before = exact_path_snapshot(product_anchor)
        existing_target = root / "existing-target"
        existing_target.mkdir(mode=0o700)
        existing_canonical = manager.validate_target_identity_for_lock(existing_target)
        existing_handle = manager.acquire_bootstrap_lock(existing_canonical)
        manager.release_bootstrap_lock(existing_handle)
        existing_marker = manager.bootstrap_lock_path(existing_canonical)
        marker_before = exact_path_snapshot(existing_marker)

        second_handle = manager.acquire_bootstrap_lock(existing_canonical)
        manager.release_bootstrap_lock(second_handle)
        if exact_path_snapshot(existing_marker) != marker_before:
            raise ValueError("bootstrap acquire rewrote a pre-existing marker")

        fault_cases: tuple[tuple[str, str], ...] = (
            ("create", "os.open"),
            ("fchmod", "os.fchmod"),
            ("binding-write", "write_all_fd"),
            ("fsync", "os.fsync"),
            ("no-replace-publish", "atomic_rename_no_replace"),
            ("parent-fsync", "fsync_directory"),
        )
        for label, target_name in fault_cases:
            target = root / f"new-{label}"
            target.mkdir(mode=0o700)
            canonical = manager.validate_target_identity_for_lock(target)
            marker = product_root / manager.bootstrap_lock_file_name(canonical)
            marker_name = marker.name
            before_root = exact_tree_snapshot(product_root)
            injected = {"value": False}
            if target_name == "os.open":
                original = manager.os.open

                def fail_open(path: Any, flags: int, mode: int = 0o777, *args: Any) -> int:
                    if (
                        Path(path).name.startswith(f".{marker_name}.nddev.tmp.")
                        and not injected["value"]
                    ):
                        injected["value"] = True
                        raise OSError("bootstrap marker create fault")
                    return original(path, flags, mode, *args)

                manager.os.open = fail_open
            elif target_name == "os.fchmod":
                original = manager.os.fchmod

                def fail_fchmod(fd: int, mode: int) -> None:
                    if not injected["value"]:
                        injected["value"] = True
                        raise OSError("bootstrap marker fchmod fault")
                    original(fd, mode)

                manager.os.fchmod = fail_fchmod
            elif target_name == "write_all_fd":
                original = manager.write_all_fd

                def fail_binding_write(fd: int, data: bytes) -> None:
                    if not injected["value"]:
                        injected["value"] = True
                        os.write(fd, data[: max(1, len(data) // 2)])
                        raise OSError("bootstrap marker binding write fault")
                    original(fd, data)

                manager.write_all_fd = fail_binding_write
            elif target_name == "os.fsync":
                original = manager.os.fsync

                def fail_fsync(fd: int) -> None:
                    if not injected["value"]:
                        injected["value"] = True
                        raise OSError("bootstrap marker fsync fault")
                    original(fd)

                manager.os.fsync = fail_fsync
            elif target_name == "atomic_rename_no_replace":
                original = manager.atomic_rename_no_replace

                def fail_publish(source: Path, destination: Path) -> None:
                    if not injected["value"]:
                        injected["value"] = True
                        raise OSError("bootstrap marker no-replace publish fault")
                    original(source, destination)

                manager.atomic_rename_no_replace = fail_publish
            else:
                original = manager.fsync_directory

                def fail_parent_fsync(path: Path) -> None:
                    if path == product_root and not injected["value"]:
                        injected["value"] = True
                        raise OSError("bootstrap marker parent fsync fault")
                    original(path)

                manager.fsync_directory = fail_parent_fsync
            try:
                try:
                    manager.acquire_bootstrap_lock(canonical)
                except OSError:
                    pass
                except Exception as exc:
                    if exc.__class__.__name__ != "JunieCliSetupError":
                        raise ValueError(
                            f"bootstrap {label} fault raised unexpected {exc!r}"
                        ) from exc
                else:
                    raise ValueError(f"bootstrap {label} fault did not raise")
            finally:
                if target_name == "os.open":
                    manager.os.open = original
                elif target_name == "os.fchmod":
                    manager.os.fchmod = original
                elif target_name == "write_all_fd":
                    manager.write_all_fd = original
                elif target_name == "os.fsync":
                    manager.os.fsync = original
                elif target_name == "atomic_rename_no_replace":
                    manager.atomic_rename_no_replace = original
                else:
                    manager.fsync_directory = original
            if not injected["value"]:
                raise ValueError(f"bootstrap {label} fault was not injected")
            if target_name == "fsync_directory":
                marker_fd = manager.open_existing_bootstrap_lock_file(marker)
                try:
                    manager.ensure_bootstrap_lock_binding(
                        marker_fd,
                        manager.bootstrap_lock_binding(canonical),
                    )
                finally:
                    os.close(marker_fd)
                if [path for path in product_root.rglob("*") if ".nddev.tmp." in path.name]:
                    raise ValueError("bootstrap parent-fsync fault left temp residue")
            elif exact_tree_snapshot(product_root) != before_root:
                raise ValueError(f"bootstrap {label} fault changed product root graph")
            if exact_path_snapshot(product_anchor) != product_anchor_before:
                raise ValueError(f"bootstrap {label} fault changed product anchor")
            if exact_path_snapshot(existing_marker) != marker_before:
                raise ValueError(f"bootstrap {label} fault changed pre-existing marker")

        blocked = manager.acquire_bootstrap_lock(existing_canonical)
        try:
            expect_manager_failure(
                "bootstrap target lock acquisition",
                lambda: manager.acquire_bootstrap_lock(existing_canonical),
            )
            if exact_path_snapshot(existing_marker) != marker_before:
                raise ValueError("bootstrap lock acquisition failure changed marker identity")
            other_target = root / "different-target"
            other_target.mkdir(mode=0o700)
            other_canonical = manager.validate_target_identity_for_lock(other_target)
            other = manager.acquire_bootstrap_lock(other_canonical)
            manager.release_bootstrap_lock(other)
        finally:
            manager.release_bootstrap_lock(blocked)

        handoff_target = root / "handoff-target"
        handoff_target.mkdir(mode=0o700)
        handoff_canonical = manager.validate_target_identity_for_lock(handoff_target)
        handoff_marker = product_root / manager.bootstrap_lock_file_name(handoff_canonical)
        original_release_global = manager.release_bootstrap_global_lock
        injected_handoff = {"value": False}

        def fail_handoff_once(handle: Any) -> None:
            original_release_global(handle)
            if not injected_handoff["value"]:
                injected_handoff["value"] = True
                raise OSError("bootstrap handoff fault")

        manager.release_bootstrap_global_lock = fail_handoff_once
        try:
            try:
                manager.acquire_bootstrap_lock(handoff_canonical)
            except OSError:
                pass
            else:
                raise ValueError("bootstrap handoff fault did not raise")
        finally:
            manager.release_bootstrap_global_lock = original_release_global
        if not injected_handoff["value"]:
            raise ValueError("bootstrap handoff fault was not injected")
        handoff_fd = manager.open_existing_bootstrap_lock_file(handoff_marker)
        try:
            manager.ensure_bootstrap_lock_binding(
                handoff_fd,
                manager.bootstrap_lock_binding(handoff_canonical),
            )
        finally:
            os.close(handoff_fd)
        if [path for path in product_root.rglob("*") if ".nddev.tmp." in path.name]:
            raise ValueError("bootstrap handoff fault left temp residue")


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
        managed.write_bytes(b"old\n")
        original_fsync = manager.os.fsync
        fsync_calls = {"count": 0}

        def fail_parent_fsync_once(fd: int) -> None:
            fsync_calls["count"] += 1
            if fsync_calls["count"] == 2:
                raise OSError("parent fsync fault")
            original_fsync(fd)

        manager.os.fsync = fail_parent_fsync_once
        try:
            try:
                manager.atomic_write(managed, b"new\n", marker_target)
            except OSError:
                pass
            else:
                raise ValueError("atomic write parent fsync fault did not raise")
        finally:
            manager.os.fsync = original_fsync
        if managed.read_bytes() != b"old\n":
            raise ValueError("atomic write parent fsync rollback changed destination bytes")
        if list(marker_target.glob(".config.json.nddev.tmp.*")):
            raise ValueError("atomic write parent fsync rollback left temporary sibling")
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

    validate_sticky_temp_target_cleanup(manager)


def validate_sticky_temp_target_cleanup(manager: Any) -> None:
    sticky_parent = Path("/tmp")
    if not sticky_parent.is_dir() or not (
        stat.S_IMODE(sticky_parent.stat().st_mode) & stat.S_ISVTX
    ):
        return
    target = sticky_parent / f"nddev-junie-validator-{os.getpid()}-{id(manager)}"
    canonical: Path | None = None
    try:
        with manager.target_lock(target, create_parent=True):
            canonical = manager.validate_target(target, create=False)
            if stat.S_IMODE(canonical.lstat().st_mode) & 0o077:
                raise ValueError("sticky-temp target was not created private")
    finally:
        manager.safe_rmtree_private_directory_if_exists(
            canonical if canonical is not None else target.resolve(strict=False),
            "sticky-temp validator target",
        )


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

    def fake_commit(target: Path, transaction: Any, snapshot: Any) -> bool:
        if snapshot is not None:
            manager.cleanup_transaction_preserve_dir(snapshot)
        return False

    manager.commit_software_transaction = fake_commit
    manager.current_software_metadata = lambda target: metadata
    manager.software_state = lambda target: {"state": "installed", "version": "validator"}
    try:
        callback()
    finally:
        manager.begin_software_transaction = original_begin
        manager.commit_software_transaction = original_commit
        manager.current_software_metadata = original_current
        manager.software_state = original_state


def install_fake_software_via_cli(
    manager: Any,
    target: Path,
    *,
    shim_bytes: bytes = b"#!/bin/sh\nexit 0\n",
) -> dict[str, Any]:
    metadata_holder: dict[str, Any] = {}
    original_begin = manager.begin_software_transaction
    original_commit = manager.commit_software_transaction

    def fake_begin(target_path: Path, *, repair: bool) -> Any:
        metadata = fake_launch_runtime_metadata(manager, target_path, shim_bytes=shim_bytes)
        metadata_holder["metadata"] = metadata
        return manager.SoftwareTransaction(metadata=metadata, changed=True)

    def fake_commit(target_path: Path, transaction: Any, snapshot: Any) -> bool:
        if snapshot is not None:
            manager.cleanup_transaction_preserve_dir(snapshot)
        return False

    manager.begin_software_transaction = fake_begin
    manager.commit_software_transaction = fake_commit
    try:
        result = manager.install_or_update_cli(target, repair=False, command="install-cli")
    finally:
        manager.begin_software_transaction = original_begin
        manager.commit_software_transaction = original_commit
    if not metadata_holder:
        raise ValueError("fake software transaction did not run")
    return result


def fake_launch_runtime_metadata(
    manager: Any,
    target: Path,
    *,
    shim_bytes: bytes = b"#!/bin/sh\nexit 0\n",
) -> dict[str, Any]:
    baseline = manager.load_baseline()
    version = baseline["release"]["exact_version"]
    platform_id = manager.current_platform_id()
    artifact = dict(baseline["release"]["exact_artifacts"][platform_id])
    artifact["platform"] = platform_id
    runtime = manager.runtime_root(target)
    home = manager.runtime_home(target)
    data = manager.runtime_data(target)
    version_dir = data / "versions" / version
    binary_parent = version_dir / "junie" / "bin"
    for directory in (
        runtime,
        home,
        home / ".local",
        home / ".local" / "bin",
        data,
        data / "versions",
        version_dir,
        version_dir / "junie",
        binary_parent,
    ):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    shim = manager.runtime_bin(target)
    binary = binary_parent / "junie"
    shim.write_bytes(shim_bytes)
    binary.write_bytes(b"validator Junie binary\n")
    os.chmod(shim, manager.OWNER_EXECUTABLE_MODE)
    os.chmod(binary, manager.OWNER_EXECUTABLE_MODE)
    current = data / "current"
    if current.exists() or current.is_symlink():
        current.unlink()
    current.symlink_to(version_dir)
    manager.atomic_write(
        manager.runtime_receipt_path(target),
        manager.canonical_json(
            {
                "schema_version": 1,
                "product_name": manager.PRODUCT_NAME,
                "build_version": manager.VERSION,
                "version": version,
                "platform": platform_id,
                "installer": baseline["release"]["installer"],
                "update_info": baseline["release"]["update_info"],
                "artifact": artifact,
                "artifact_verification": {
                    "size_verified": True,
                    "sha256_verified": True,
                    "method": "validator",
                },
                "installer_output_sha256": "0" * 64,
            }
        ),
        target,
    )
    return manager.current_software_metadata(target)


def validate_profile_switch_lifecycle(manager: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-profile-cycle-") as raw:
        target = Path(raw) / "target"
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        safe = manager.load_profile("safe")
        full_auto = manager.load_profile("full-auto")

        installed = manager.write_setup(target, setup, safe)
        if installed["setup_id"] != DEFAULT_SETUP_ID or installed["profile_id"] != "safe":
            raise ValueError("safe install did not record orthogonal setup/profile ids")
        if installed["software"] != {"state": "absent"} or installed["software_changed"]:
            raise ValueError("setup install ambiguously mutated target-owned software")
        stamp = manager.read_stamp(manager.validate_target(target, create=False))
        if stamp is None or stamp.get("schema_version") != manager.STAMP_SCHEMA:
            raise ValueError("new install did not write the current orthogonal stamp schema")

        before_target = exact_tree_snapshot(target)
        before_backup = exact_tree_snapshot(manager.backup_pool(target))
        before_software = manager.software_state(target)
        noop_install = manager.write_setup(target, setup, safe)
        if noop_install["changed"] or noop_install["backup_slot"] is not None:
            raise ValueError("identical install did not report a true no-op")
        if noop_install["software_changed"]:
            raise ValueError("identical install reported software mutation")
        if manager.software_state(target) != before_software:
            raise ValueError("identical install changed software identity")
        if exact_tree_snapshot(target) != before_target:
            raise ValueError("identical install changed target inode, mtime, mode, or bytes")
        if exact_tree_snapshot(manager.backup_pool(target)) != before_backup:
            raise ValueError("identical install changed the backup pool")
        noop_update = manager.update_setup(target, None, None)
        if noop_update["changed"] or noop_update["backup_slot"] is not None:
            raise ValueError("identical update did not report a true no-op")
        if noop_update["software_changed"]:
            raise ValueError("identical update reported software mutation")
        if manager.software_state(target) != before_software:
            raise ValueError("identical update changed software identity")
        if exact_tree_snapshot(target) != before_target:
            raise ValueError("identical update changed target inode, mtime, mode, or bytes")
        if exact_tree_snapshot(manager.backup_pool(target)) != before_backup:
            raise ValueError("identical update changed the backup pool")
        expect_manager_failure(
            "update profile override rejected",
            lambda: manager.update_setup(target, None, "full-auto"),
        )

        software = install_fake_software_via_cli(manager, target)
        if software["software"].get("state") != "installed":
            raise ValueError("install-cli did not publish installed software state")

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


def validate_software_remove_and_noop_lifecycle(manager: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-software-remove-") as raw:
        target = Path(raw) / "target"
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        safe = manager.load_profile("safe")
        manager.write_setup(target, setup, safe)
        install_fake_software_via_cli(manager, target)
        before_update = exact_tree_snapshot(target)
        update = manager.install_or_update_cli(target, repair=True, command="update-cli")
        if update["software_changed"]:
            raise ValueError("already-current update-cli reported software mutation")
        if exact_tree_snapshot(target) != before_update:
            raise ValueError("already-current update-cli changed target identity")

        allowlist = target / manager.RUNTIME_ALLOWLIST_RELATIVE
        allowlist_before = exact_path_snapshot(allowlist)
        removed = manager.remove_cli(target)
        if not removed["software_changed"]:
            raise ValueError("remove-cli did not report software removal")
        if removed["software"] != {"state": "absent"}:
            raise ValueError("remove-cli did not return absent software state")
        if manager.software_state(target) != {"state": "absent"}:
            raise ValueError("remove-cli did not leave absent software state")
        if not manager.runtime_contains_only_setup_projection(target):
            raise ValueError("remove-cli did not preserve only setup-owned runtime projection")
        if exact_path_snapshot(allowlist) != allowlist_before:
            raise ValueError("remove-cli changed setup-owned runtime allowlist identity")
        status = manager_main_json(manager, ["status", "--target", str(target)])
        if status.get("drift"):
            raise ValueError(f"remove-cli left setup drift: {status['drift']}")
        if temporary_residue(target):
            raise ValueError("remove-cli left transaction residue")

    with tempfile.TemporaryDirectory(prefix="nddev-junie-software-remove-fault-") as raw:
        target = Path(raw) / "target"
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        safe = manager.load_profile("safe")
        manager.write_setup(target, setup, safe)
        install_fake_software_via_cli(manager, target)
        before_fault = exact_tree_snapshot(target)
        original_write = manager.write_stamp_software_state
        observed_removed = {"value": False}

        def fail_after_removed_state(
            target_path: Path, current: dict[str, Any], software: dict[str, Any]
        ) -> dict[str, Any]:
            if manager.software_state(target_path) != {"state": "absent"}:
                raise ValueError("remove-cli fault did not observe absent software state")
            if not list(target_path.glob(f".{manager.RUNTIME_DIR_NAME}.removed.*")):
                raise ValueError("remove-cli fault did not stage removed runtime entries")
            observed_removed["value"] = True
            raise manager.JunieCliSetupError("post-remove validator fault")

        manager.write_stamp_software_state = fail_after_removed_state
        try:
            expect_manager_failure(
                "remove-cli post-remove fault", lambda: manager.remove_cli(target)
            )
        finally:
            manager.write_stamp_software_state = original_write
        if not observed_removed["value"]:
            raise ValueError("remove-cli post-remove fault was not injected")
        if exact_tree_snapshot(target) != before_fault:
            raise ValueError("remove-cli post-remove fault changed target identity")
        if temporary_residue(target):
            raise ValueError("remove-cli post-remove fault left transaction residue")


def validate_failed_first_install_restores_parent(manager: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-first-install-fault-") as raw:
        parent = Path(raw) / "targets"
        parent.mkdir(mode=0o700)
        target = parent / "junie"
        before_parent = exact_tree_snapshot(parent)
        product_root = manager.bootstrap_lock_product_root_path(
            manager.bootstrap_lock_system_root()
        )
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        safe = manager.load_profile("safe")
        original_atomic = manager.atomic_write
        failed = {"value": False}

        def fail_config_write(
            path: Path, data: bytes, target_path: Path, *, mode: int = 0o600
        ) -> None:
            if Path(path).name == "config.json" and not failed["value"]:
                failed["value"] = True
                raise manager.JunieCliSetupError("first install validator fault")
            original_atomic(path, data, target_path, mode=mode)

        manager.atomic_write = fail_config_write
        try:
            expect_manager_failure(
                "failed first setup install",
                lambda: manager.write_setup(target, setup, safe),
            )
        finally:
            manager.atomic_write = original_atomic
        if not failed["value"]:
            raise ValueError("first install fault was not injected")
        if target.exists():
            raise ValueError("failed first setup install left the target directory")
        if exact_tree_snapshot(parent) != before_parent:
            raise ValueError("failed first setup install changed parent identity")
        if product_root.exists():
            residue = [path.name for path in product_root.rglob("*") if ".nddev.tmp." in path.name]
            if residue:
                raise ValueError("failed first setup install left bootstrap temp residue")
            global_lock = product_root / manager.BOOTSTRAP_GLOBAL_LOCK_NAME
            if global_lock.exists():
                global_fd = manager.open_existing_bootstrap_lock_file(global_lock)
                try:
                    manager.ensure_bootstrap_lock_binding(
                        global_fd,
                        manager.bootstrap_global_lock_binding(),
                    )
                finally:
                    os.close(global_fd)
            canonical = manager.validate_target_identity_for_lock(target)
            target_anchor = product_root / manager.bootstrap_lock_file_name(canonical)
            if target_anchor.exists():
                target_fd = manager.open_existing_bootstrap_lock_file(target_anchor)
                try:
                    manager.ensure_bootstrap_lock_binding(
                        target_fd,
                        manager.bootstrap_lock_binding(canonical),
                    )
                finally:
                    os.close(target_fd)


def validate_read_commands_leave_target_unchanged(manager: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-read-locks-") as raw:
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        profile = manager.load_profile(DEFAULT_PROFILE_ID)
        commands = (
            ["status"],
            ["plan", "--setup", setup["id"], "--profile", profile["id"]],
            ["software-status"],
        )
        for preexisting_lock in (False, True):
            target = Path(raw) / ("prelocked" if preexisting_lock else "empty")
            target.mkdir(mode=0o700)
            if preexisting_lock:
                with manager.target_lock(target, create_parent=False):
                    pass
            before = exact_tree_snapshot(target)
            for command in commands:
                manager_main_json(manager, [*command, "--target", str(target)])
                if exact_tree_snapshot(target) != before:
                    raise ValueError(
                        f"{command[0]} changed target lock artifacts or metadata "
                        f"(preexisting_lock={preexisting_lock})"
                    )
            product_root = manager.bootstrap_lock_product_root_path(
                manager.bootstrap_lock_system_root()
            )
            before_bootstrap = exact_tree_snapshot(product_root)
            missing = Path(raw) / f"missing-preexisting-{preexisting_lock}" / "target"
            for command in commands:
                manager_main_json(manager, [*command, "--target", str(missing)])
                if exact_tree_snapshot(product_root) != before_bootstrap:
                    raise ValueError(
                        f"{command[0]} left newly-created bootstrap coordination artifacts "
                        f"for a missing target (preexisting_lock={preexisting_lock})"
                    )
            if not preexisting_lock and (target / manager.LOCK_DIR_NAME).exists():
                raise ValueError("read command left a newly-created target lock directory")


def validate_launch_lock_concurrency(manager: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-junie-launch-lock-") as raw:
        target = Path(raw) / "target"
        target.mkdir(mode=0o700)
        canonical = manager.validate_target(target, create=False)
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        safe = manager.load_profile("safe")
        full_auto = manager.load_profile("full-auto")
        lock = canonical / manager.LOCK_DIR_NAME
        launch_image = manager.runtime_launch_image(canonical)
        launch_image_bin = manager.runtime_launch_image_bin(canonical)

        def run() -> None:
            manager.write_setup(canonical, setup, safe)
            install_fake_software_via_cli(manager, canonical)
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

        run()


def validate_external_lock_survives_internal_lock_rename(manager: Any) -> None:
    if not hasattr(os, "fork"):
        raise ValueError("internal lock rename regression requires POSIX fork")
    with tempfile.TemporaryDirectory(prefix="nddev-junie-internal-lock-rename-") as raw:
        target = Path(raw) / "target"
        target.mkdir(mode=0o700)
        canonical = manager.validate_target(target, create=False)
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        safe = manager.load_profile("safe")
        full_auto = manager.load_profile("full-auto")
        lock = canonical / manager.LOCK_DIR_NAME
        renamed_lock = canonical / f"{manager.LOCK_DIR_NAME}.renamed"

        def run() -> None:
            manager.write_setup(canonical, setup, safe)
            install_fake_software_via_cli(manager, canonical)
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

        run()


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
        version = manager.load_baseline()["release"]["exact_version"]
        setup = manager.load_setup(DEFAULT_SETUP_ID)
        safe = manager.load_profile("safe")

        def run() -> None:
            manager.write_setup(canonical, setup, safe)
            install_fake_software_via_cli(manager, canonical, shim_bytes=script)
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
                manager.runtime_data(canonical) / "versions" / version,
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

        run()


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


def tree_snapshot(root: Path) -> tuple[tuple[str, str, int, bytes | str | None], ...]:
    if not root.exists():
        return ((".", "absent", 0, None),)
    rows: list[tuple[str, str, int, bytes | str | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            rows.append((relative, "dir", mode, None))
        elif stat.S_ISREG(info.st_mode):
            rows.append((relative, "file", mode, path.read_bytes()))
        elif stat.S_ISLNK(info.st_mode):
            rows.append((relative, "symlink", mode, os.readlink(path)))
        else:
            rows.append((relative, "other", mode, None))
    return tuple(rows)


def exact_tree_snapshot(
    root: Path,
) -> tuple[tuple[str, str, int, int, int, bytes | str | None], ...]:
    if not root.exists():
        return ((".", "absent", 0, 0, 0, None),)
    rows: list[tuple[str, str, int, int, int, bytes | str | None]] = []
    for path in [root, *sorted(root.rglob("*"))]:
        relative = path.relative_to(root).as_posix()
        if relative == "":
            relative = "."
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        metadata = (mode, info.st_ino, info.st_mtime_ns)
        if stat.S_ISDIR(info.st_mode):
            rows.append((relative, "dir", *metadata, None))
        elif stat.S_ISREG(info.st_mode):
            rows.append((relative, "file", *metadata, path.read_bytes()))
        elif stat.S_ISLNK(info.st_mode):
            rows.append((relative, "symlink", *metadata, os.readlink(path)))
        else:
            rows.append((relative, "other", *metadata, None))
    return tuple(rows)


def exact_path_snapshot(path: Path) -> tuple[str, int, int, int, bytes | str | None]:
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode):
        return ("dir", mode, info.st_ino, info.st_mtime_ns, None)
    if stat.S_ISREG(info.st_mode):
        return ("file", mode, info.st_ino, info.st_mtime_ns, path.read_bytes())
    if stat.S_ISLNK(info.st_mode):
        return ("symlink", mode, info.st_ino, info.st_mtime_ns, os.readlink(path))
    return ("other", mode, info.st_ino, info.st_mtime_ns, None)


def temporary_residue(target: Path) -> list[str]:
    if not target.exists():
        return []
    markers = (
        ".nddev.tmp.",
        ".nddev-backup.",
        ".removed.",
        ".install-stage.",
        ".old.",
    )
    return sorted(
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if any(marker in path.name for marker in markers)
    )


def backup_transaction_residue(pool: Path) -> list[str]:
    if not pool.exists():
        return []
    return sorted(path.name for path in pool.iterdir() if ".nddev-backup." in path.name)


def validate_backup_transaction_fail_closed(manager: Any) -> None:
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="nddev-junie-backup-transaction-") as raw:
            target = Path(raw) / "target"
            setup = manager.load_setup(DEFAULT_SETUP_ID)
            safe = manager.load_profile("safe")
            full_auto = manager.load_profile("full-auto")
            manager.write_setup(target, setup, safe)
            for index in range(12):
                manager.write_setup(
                    target,
                    setup,
                    full_auto if index % 2 == 0 else safe,
                    require_existing=True,
                )
            pool = manager.backup_pool(manager.validate_target(target, create=False))
            if sorted(path.name for path in pool.iterdir()) != [str(index) for index in range(10)]:
                raise ValueError("backup transaction validator did not fill all slots")
            before_pool = tree_snapshot(pool)
            before_target = target_file_bytes(target)
            original_replace = manager.os.replace

            def fail_backup_publish(
                source: str | os.PathLike[str],
                destination: str | os.PathLike[str],
            ) -> None:
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    source_path.name.startswith(f".{manager.BACKUP_NAME}.nddev.tmp.")
                    and destination_path.name == manager.BACKUP_NAME
                    and destination_path.parent.parent.resolve() == pool.resolve()
                ):
                    raise OSError("backup publish fault")
                original_replace(source, destination)

            manager.os.replace = fail_backup_publish
            try:
                try:
                    manager.write_setup(target, setup, full_auto, require_existing=True)
                except OSError:
                    pass
                else:
                    raise ValueError("full-slot backup publish fault did not raise")
            finally:
                manager.os.replace = original_replace
            if tree_snapshot(pool) != before_pool:
                raise ValueError("backup publish fault changed the backup pool")
            if target_file_bytes(target) != before_target:
                raise ValueError("backup publish fault changed target bytes")
            if backup_transaction_residue(pool):
                raise ValueError("backup publish fault left transaction residue")

    run()


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

    run()


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

    def extra_slot_entry() -> None:
        with tempfile.TemporaryDirectory(prefix="nddev-junie-restore-extra-entry-") as raw:
            target = restore_corruption_target(manager, Path(raw))
            extra = manager.backup_pool(target) / "0" / "extra.json"
            extra.write_text("{}\n", encoding="utf-8")
            os.chmod(extra, manager.OWNER_FILE_MODE)
            before = target_file_bytes(target)
            expect_manager_failure(
                "backup slot extra entry", lambda: manager.restore_backup(target, 0)
            )
            if target_file_bytes(target) != before:
                raise ValueError("backup slot extra entry failure changed target bytes")

    extra_slot_entry()


def main(argv: list[str] | None = None) -> int:
    parse_args(argv)
    version = load_version()
    build = load_json(ROOT / "build" / "version.json")
    manifest = load_json(ROOT / "build" / "manifest.json")
    contract = load_json(ROOT / "config" / "nddev-contract.json")
    baseline = load_json(ROOT / "references" / "junie-cli-baseline.json")
    manager = load_manager()
    ids = setup_ids()
    profiles = profile_ids()
    if set(build) != REQUIRED_BUILD_VERSION_KEYS:
        raise ValueError(
            "build/version.json keys are not synchronized: "
            f"actual={sorted(build)}, expected={sorted(REQUIRED_BUILD_VERSION_KEYS)}"
        )
    if build.get("python_requires") != PYTHON_REQUIRES:
        raise ValueError("build/version.json python_requires must declare the Python 3.9 floor")
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
    if contract["managed_state"].get("bootstrap_product_anchor") != (
        "<bootstrap_lock_root>/global.lock"
    ):
        raise ValueError("contract must declare the product bootstrap anchor")
    if (
        contract["managed_state"].get("bootstrap_target_anchor")
        != "sha256(product namespace plus canonical absolute target).lock"
    ):
        raise ValueError("contract must declare the canonical target bootstrap anchor")
    if contract["managed_state"].get("bootstrap_lock_file_mode") != "0600":
        raise ValueError("contract must declare bootstrap lock file mode")
    if contract["managed_state"].get("bootstrap_product_anchor_persistent") is not True:
        raise ValueError("contract must declare the persistent product anchor")
    if contract["managed_state"].get("bootstrap_target_anchor_persistent") is not True:
        raise ValueError("contract must declare persistent canonical target anchors")
    publication_text = contract["managed_state"].get("bootstrap_anchor_publication", "")
    for phrase in (
        "fsynced temporary",
        "native atomic no-replace rename",
        "no hard-link alias",
        "never truncated",
        "replaced",
    ):
        if phrase not in publication_text:
            raise ValueError("contract must declare no-replace bootstrap anchor publication")
    coordination_text = contract["managed_state"].get("read_only_bootstrap_coordination", "")
    for phrase in ("create no product", "cold no-anchor", "double-check", "seeded reads"):
        if phrase not in coordination_text:
            raise ValueError("contract must declare read-only bootstrap coordination semantics")
    if contract["managed_state"].get("target_lock_persistent") is not True:
        raise ValueError("contract must declare persistent target lock files")
    cleanup_manifest = manifest.get("cleanup_journal", {})
    cleanup_expected = {
        "cleanup_directory": "<target>/.nddev-junie-cli-cleanup",
        "cleanup_journal": "<target>/NDDEV-JUNIE-CLI-CLEANUP.json",
        "cleanup_stage": "<target>/NDDEV-JUNIE-CLI-CLEANUP-STAGE.json",
        "cleanup_journal_schema": manager.CLEANUP_SCHEMA,
        "cleanup_stage_schema": manager.CLEANUP_STAGE_SCHEMA,
        "cleanup_journal_max_payloads": manager.CLEANUP_MAX_PAYLOADS,
        "cleanup_journal_max_entries": manager.CLEANUP_DIGEST_MAX_ENTRIES,
        "cleanup_journal_max_bytes": manager.CLEANUP_DIGEST_MAX_BYTES,
        "cleanup_journal_max_journal_bytes": manager.CLEANUP_JOURNAL_MAX_BYTES,
        "cleanup_stage_max_bytes": manager.CLEANUP_STAGE_MAX_BYTES,
    }
    for key, expected in cleanup_expected.items():
        if contract["managed_state"].get(key) != expected:
            raise ValueError(f"contract managed_state {key} does not match manager constants")
    if cleanup_manifest.get("directory") != manager.CLEANUP_DIR_NAME:
        raise ValueError("manifest cleanup journal directory does not match manager constant")
    if cleanup_manifest.get("journal") != manager.CLEANUP_JOURNAL_NAME:
        raise ValueError("manifest cleanup journal name does not match manager constant")
    if cleanup_manifest.get("stage") != manager.CLEANUP_STAGE_NAME:
        raise ValueError("manifest cleanup stage name does not match manager constant")
    if cleanup_manifest.get("schema") != manager.CLEANUP_SCHEMA:
        raise ValueError("manifest cleanup journal schema does not match manager constant")
    if cleanup_manifest.get("stage_schema") != manager.CLEANUP_STAGE_SCHEMA:
        raise ValueError("manifest cleanup stage schema does not match manager constant")
    if cleanup_manifest.get("max_payloads") != manager.CLEANUP_MAX_PAYLOADS:
        raise ValueError("manifest cleanup max_payloads does not match manager constant")
    if cleanup_manifest.get("max_entries") != manager.CLEANUP_DIGEST_MAX_ENTRIES:
        raise ValueError("manifest cleanup max_entries does not match manager constant")
    if cleanup_manifest.get("max_bytes") != manager.CLEANUP_DIGEST_MAX_BYTES:
        raise ValueError("manifest cleanup max_bytes does not match manager constant")
    if cleanup_manifest.get("max_journal_bytes") != manager.CLEANUP_JOURNAL_MAX_BYTES:
        raise ValueError("manifest cleanup max_journal_bytes does not match manager constant")
    if cleanup_manifest.get("max_stage_bytes") != manager.CLEANUP_STAGE_MAX_BYTES:
        raise ValueError("manifest cleanup max_stage_bytes does not match manager constant")
    stage_binding = cleanup_manifest.get("stage_binding", {})
    if stage_binding.get("source_anchor") != manager.CLEANUP_STAGE_SOURCE_ANCHOR:
        raise ValueError("manifest cleanup stage source anchor does not match manager constant")
    if stage_binding.get("source_parent_kind") != manager.CLEANUP_STAGE_SOURCE_PARENT_KIND:
        raise ValueError(
            "manifest cleanup stage source parent kind does not match manager constant"
        )
    if stage_binding.get("source_kind") != manager.CLEANUP_STAGE_SOURCE_KIND:
        raise ValueError("manifest cleanup stage source kind does not match manager constant")
    if stage_binding.get("destination_anchor") != manager.CLEANUP_STAGE_DESTINATION_ANCHOR:
        raise ValueError(
            "manifest cleanup stage destination anchor does not match manager constant"
        )
    if (
        stage_binding.get("destination_parent_kind")
        != manager.CLEANUP_STAGE_DESTINATION_PARENT_KIND
    ):
        raise ValueError(
            "manifest cleanup stage destination parent kind does not match manager constant"
        )
    if stage_binding.get("destination_kind") != manager.CLEANUP_STAGE_DESTINATION_KIND:
        raise ValueError("manifest cleanup stage destination kind does not match manager constant")
    if "bounded relative" not in contract["managed_state"].get("cleanup_stage_binding", ""):
        raise ValueError("contract must declare bounded relative cleanup stage bindings")
    if "source_parent_identity" not in contract["managed_state"].get("cleanup_stage_binding", ""):
        raise ValueError("contract must declare cleanup stage source parent identity binding")
    if stage_binding.get("target_kind") != manager.CLEANUP_STAGE_TARGET_KIND:
        raise ValueError("manifest cleanup stage target kind does not match manager constant")
    cleanup_publication = contract["managed_state"].get("cleanup_journal_publication", "")
    for phrase in (
        "complete immutable",
        "identity-bound",
        "native atomic no-replace rename",
        "read-only commands fail closed",
        "never repair or drain",
    ):
        if phrase not in cleanup_publication:
            raise ValueError("contract must declare cleanup journal publication semantics")
    if "top-level cleanup_pending" not in contract["managed_state"].get(
        "cleanup_pending_result", ""
    ):
        raise ValueError("contract must declare top-level cleanup_pending results")
    if contract["safety"].get("bootstrap_binding_atomic_publication") is not True:
        raise ValueError("contract must declare atomic bootstrap binding publication")
    if contract["safety"].get("bootstrap_anchor_no_replace_publication") is not True:
        raise ValueError("contract must declare no-replace bootstrap anchor publication")
    if contract["safety"].get("bootstrap_anchor_monotonic") is not True:
        raise ValueError("contract must declare monotonic bootstrap anchors")
    if contract["safety"].get("read_only_bootstrap_no_create") is not True:
        raise ValueError("contract must declare read-only no-create bootstrap coordination")
    if contract["safety"].get("cleanup_journal_no_replace_publication") is not True:
        raise ValueError("contract must declare no-replace cleanup journal publication")
    if contract["safety"].get("cleanup_read_only_no_recovery") is not True:
        raise ValueError("contract must declare read-only cleanup no-recovery behavior")
    if contract["safety"].get("cleanup_later_mutations_drain_first") is not True:
        raise ValueError("contract must declare mutation cleanup drain-first behavior")
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
    validate_platform_detection(manager)
    validate_production_source()
    with injected_bootstrap_lock_root(manager):
        validate_generated_files(manager, version)
        validate_builder_projection_invariant(manager)
        validate_manager_parse(manager)
        validate_json_argument_boundary(manager)
        validate_unsupported_host_preflight(manager)
        validate_target_observation_after_seeded_product_lock(manager)
        validate_bootstrap_lock_adversarial(manager)
        validate_bootstrap_lock_atomic_publication(manager)
        validate_bootstrap_lock_persistent_handover(manager)
        validate_dual_lock_persistent_handover(manager)
        validate_adversarial_smokes(manager)
        validate_profile_switch_lifecycle(manager)
        validate_launch_lock_concurrency(manager)
        validate_external_lock_survives_internal_lock_rename(manager)
        validate_verified_launcher_handoff(manager)
        validate_legacy_mapping_migration(manager)
        validate_backup_transaction_fail_closed(manager)
        validate_restore_backup_fail_closed(manager)
        validate_software_remove_and_noop_lifecycle(manager)
        validate_failed_first_install_restores_parent(manager)
        validate_read_commands_leave_target_unchanged(manager)
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
