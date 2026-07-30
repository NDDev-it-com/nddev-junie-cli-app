#!/usr/bin/env python3
"""Validate static public artifacts for nddev-junie-cli-app."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
REQUIRED_WORKFLOWS = {
    "actionlint.yml",
    "codeql.yml",
    "dependency-review.yml",
    "release.yml",
    "scorecard.yml",
    "secret-scan.yml",
    "zizmor.yml",
}
FORBIDDEN_RAW_OBSERVATION_FIELDS = {
    "observed_at",
    "exact_artifact_hashes",
    "unsupported_official_artifact_platforms",
}
FORBIDDEN_RAW_MANAGER_MARKERS = {
    "OFFICIAL_UNSUPPORTED_ARTIFACT_PLATFORMS",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    return parser.parse_args(argv)


def load_json(relative: str) -> dict[str, Any]:
    value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{relative} must contain a JSON object")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_versions() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    build = load_json("build/version.json")
    manifest = load_json("build/manifest.json")
    contract = load_json("config/nddev-contract.json")
    baseline = load_json("references/junie-cli-baseline.json")
    require(bool(SEMVER.fullmatch(version)), "VERSION must be semantic")
    require(build.get("build_version") == version, "build version mismatch")
    require(manifest.get("build_version") == version, "manifest version mismatch")
    projection = build.get("nddev_builder_projection_version")
    require(
        projection == manifest.get("nddev_builder_projection_version"),
        "builder projection version mismatch",
    )
    runtime = build.get("junie_cli_tested")
    require(runtime == manifest.get("junie_cli_tested"), "manifest runtime mismatch")
    require(runtime == baseline["release"]["exact_version"], "baseline runtime mismatch")
    require(
        runtime == contract["runtime_compatibility"]["tested_release"],
        "contract runtime mismatch",
    )
    require(
        runtime == contract["software_lifecycle"]["version"],
        "software runtime mismatch",
    )
    require(contract.get("version_ref") == "build/version.json", "version_ref mismatch")
    require(contract.get("manifest_ref") == "build/manifest.json", "manifest_ref mismatch")
    return manifest, contract, baseline


def validate_catalog(manifest: dict[str, Any], contract: dict[str, Any]) -> None:
    setup_ids = manifest.get("setup_ids")
    profile_ids = manifest.get("profile_ids")
    require(
        setup_ids == contract["setup_system"]["setup_ids"],
        "setup catalog mismatch",
    )
    require(
        profile_ids == contract["permission_profiles"]["profile_ids"],
        "profile catalog mismatch",
    )
    require(
        manifest.get("default_setup_id")
        == contract["setup_system"]["default_setup_id"],
        "default setup mismatch",
    )
    require(
        manifest.get("default_profile_id")
        == contract["permission_profiles"]["default_profile_id"],
        "default profile mismatch",
    )
    require(isinstance(setup_ids, list) and setup_ids, "setup catalog missing")
    require(isinstance(profile_ids, list) and profile_ids, "profile catalog missing")
    for setup_id in setup_ids:
        setup = load_json(f"setups/{setup_id}/setup.json")
        require(setup.get("id") == setup_id, f"setup id mismatch: {setup_id}")
        projection = setup.get("content_projection")
        require(
            isinstance(projection, dict)
            and projection
            and all(value is True for value in projection.values()),
            f"content setup projection invalid: {setup_id}",
        )
    for profile_id in profile_ids:
        profile = load_json(f"profiles/{profile_id}/profile.json")
        require(profile.get("id") == profile_id, f"profile id mismatch: {profile_id}")
        require(
            isinstance(profile.get("brave"), bool),
            f"profile brave posture missing: {profile_id}",
        )
        require(
            isinstance(profile.get("allowlist"), dict),
            f"profile allowlist missing: {profile_id}",
        )


def validate_runtime_integrity(
    manifest: dict[str, Any], contract: dict[str, Any], baseline: dict[str, Any]
) -> None:
    compatibility = contract["runtime_compatibility"]
    release = baseline["release"]
    for key in (
        "supported_platforms",
        "artifact_platform_map",
        "ubuntu_support",
    ):
        require(manifest.get(key) == compatibility.get(key), f"{key} mismatch")
    require(
        set(manifest["official_artifact_platforms"])
        == set(release["exact_artifacts"]),
        "official artifact platform mismatch",
    )
    require(
        set(manifest["unsupported_platforms"])
        == set(compatibility["unsupported_platforms"]),
        "unsupported platform mismatch",
    )
    artifacts = release["exact_artifacts"]
    require(
        set(artifacts) == set(manifest["official_artifact_platforms"]),
        "artifact platform closure mismatch",
    )
    for platform_id, artifact in artifacts.items():
        digest = artifact.get("sha256")
        require(
            isinstance(digest, str) and len(digest) == 64,
            f"invalid artifact digest: {platform_id}",
        )
        int(digest, 16)
        require(artifact.get("size", 0) > 0, f"invalid artifact size: {platform_id}")
    software = contract["software_lifecycle"]
    require(
        software["installer_sha256"] == release["installer"]["sha256"],
        "installer digest mismatch",
    )
    int(software["installer_sha256"], 16)
    require(
        software["installer_url"] == release["installer"]["url"],
        "installer URL mismatch",
    )
    for label, value in (
        ("manifest", manifest),
        ("contract", contract),
        ("baseline", baseline),
    ):
        serialized = json.dumps(value, sort_keys=True)
        for field in FORBIDDEN_RAW_OBSERVATION_FIELDS:
            require(
                f'"{field}"' not in serialized,
                f"{label} contains raw observation field {field}",
            )


def validate_builder_projection(version: str, contract: dict[str, Any]) -> None:
    toolkit = contract["builder_toolkit"]
    build = load_json("build/version.json")
    require(
        build.get("nddev_builder_projection_version") == version,
        "builder version mismatch",
    )
    root = ROOT / "builder/nddev-builder"
    require(root.is_dir(), "builder source root missing")
    projections = toolkit.get("direct_projection")
    require(isinstance(projections, dict) and projections, "direct projections missing")
    for relative in ("hooks/nddev-builder-context.py",):
        require((root / relative).is_file(), f"missing builder projection: {relative}")
    for relative in ("skills", "agents", "commands"):
        projection_root = root / relative
        require(
            projection_root.is_dir()
            and any(path.is_file() for path in projection_root.rglob("*")),
            f"empty builder projection: {relative}",
        )
    marketplace = toolkit.get("local_extension_marketplace")
    require(
        isinstance(marketplace, dict)
        and marketplace.get("external_marketplace_published") is None
        and marketplace.get("installed_state_generated") is False,
        "local extension marketplace boundary mismatch",
    )
    manager_source = (ROOT / "cli-tools/nddev_junie_cli.py").read_text(encoding="utf-8")
    require(
        marketplace.get("marketplace_manifest")
        == "extensions/nddev-builder-marketplace/.junie-extension/marketplace.json"
        and 'return "extensions/nddev-builder-marketplace"' in manager_source
        and '/.junie-extension/marketplace.json"' in manager_source,
        "missing generated local extension marketplace projection",
    )
    require(
        marketplace.get("extension_root")
        == "extensions/nddev-builder-marketplace/extensions/nddev-builder"
        and '/extensions/nddev-builder/guidelines/AGENTS.md"' in manager_source,
        "missing generated local extension root projection",
    )
    for relative in ("AGENTS.md", "mcp/mcp.json"):
        require(
            relative in manager_source,
            f"missing generated builder projection: {relative}",
        )


def validate_static_source() -> None:
    path = ROOT / "cli-tools/nddev_junie_cli.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    functions = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    require({"parse_args", "main"} <= functions, "manager parse_args/main missing")
    for marker in (
        "NDDEV_JUNIE_CLI_TEST",
        "BOOTSTRAP_ROOT_OVERRIDE",
        "TEST_INSTALLER",
        "FAIL_AFTER",
    ):
        require(marker not in source, f"public manager contains test marker {marker}")
    for marker in FORBIDDEN_RAW_MANAGER_MARKERS:
        require(marker not in source, f"public manager contains raw observation marker {marker}")


def validate_release_surface(manifest: dict[str, Any]) -> None:
    agents = ROOT / "AGENTS.md"
    require(stat.S_ISREG(agents.lstat().st_mode), "AGENTS.md must be a regular file")
    for name in REQUIRED_WORKFLOWS:
        require(
            (ROOT / ".github/workflows" / name).is_file(),
            f"missing workflow {name}",
        )
    for relative in (
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
    ):
        require((ROOT / relative).exists(), f"missing release path {relative}")
    bridge_root = ROOT / ".claude"
    bridge = bridge_root / "CLAUDE.md"
    require(
        stat.S_ISDIR(bridge_root.lstat().st_mode),
        "Claude bridge root must be a directory",
    )
    require(
        sorted(path.name for path in bridge_root.iterdir()) == ["CLAUDE.md"],
        "Claude bridge directory must contain only CLAUDE.md",
    )
    require(stat.S_ISREG(bridge.lstat().st_mode), "Claude bridge must be a regular file")
    require(bridge.read_bytes() == b"@../AGENTS.md\n", "Claude bridge mismatch")
    validator = manifest.get("public_validator")
    require(
        validator == "cli-tools/validate_public_contracts.py",
        "public validator manifest binding mismatch",
    )
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    require(bool(hashlib.sha256(manifest_bytes).hexdigest()), "manifest digest failed")


def main(argv: list[str] | None = None) -> int:
    parse_args(argv)
    try:
        manifest, contract, baseline = validate_versions()
        validate_catalog(manifest, contract)
        validate_runtime_integrity(manifest, contract, baseline)
        validate_builder_projection(
            manifest["nddev_builder_projection_version"], contract
        )
        validate_static_source()
        validate_release_surface(manifest)
    except Exception as exc:
        print(f"validate_public_contracts.py: FAIL: {exc}", file=sys.stderr)
        return 1
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
