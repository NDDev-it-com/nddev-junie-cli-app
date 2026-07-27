#!/usr/bin/env python3
"""Validate public nddev-junie-cli-app release contracts."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
SHARED_CI_COMMIT = "2ccb80e96f5771b6a6b4eae63a4f47e232906dc7"
SHARED_CI_VERSION = "0.12.0"
SETUP_ORDER = ["safe", "full-auto"]
DEFAULT_SETUP_ID = "full-auto"
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
        "setups/safe/setup.json",
        "setups/full-auto/setup.json",
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
        files = manager.desired_files(target, setup)
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
        raise ValueError("default generated setup must be full-auto")
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


def validate_manager_parse(manager: Any) -> None:
    manager.parse_args(["plan", "--target", "/tmp/target"])
    manager.parse_args(["install", "--target", "/tmp/target"])
    manager.parse_args(["switch", "--setup", "safe", "--target", "/tmp/target"])
    manager.parse_args(["migrate", "--target", "/tmp/target"])
    manager.parse_args(["launch", "--target", "/tmp/target", "--", "--version"])


def main(argv: list[str] | None = None) -> int:
    parse_args(argv)
    version = load_version()
    build = load_json(ROOT / "build" / "version.json")
    manifest = load_json(ROOT / "build" / "manifest.json")
    contract = load_json(ROOT / "config" / "nddev-contract.json")
    baseline = load_json(ROOT / "references" / "junie-cli-baseline.json")
    ids = setup_ids()
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
    if contract["managed_state"]["stamp_schema"] != 2:
        raise ValueError("contract must declare stamp schema 2")
    if manifest.get("setup_ids") != ids or contract["setup_system"]["setup_ids"] != ids:
        raise ValueError("setup ids are not synchronized")
    if manifest.get("default_setup_id") != DEFAULT_SETUP_ID:
        raise ValueError("manifest default setup mismatch")
    if contract["setup_system"]["default_setup_id"] != DEFAULT_SETUP_ID:
        raise ValueError("contract default setup mismatch")
    if build.get("junie_cli_tested") != baseline["release"]["stable_version"]:
        raise ValueError("tested Junie CLI version differs from baseline release")
    validate_baseline(baseline, contract)
    validate_required_files()
    manager = load_manager()
    validate_generated_files(manager, version)
    validate_manager_parse(manager)
    validate_workflows()
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"validate_public_contracts.py: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
