#!/usr/bin/env python3
"""Validate public nddev-junie-cli-app release contracts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SHARED_CI_COMMIT = "2ccb80e96f5771b6a6b4eae63a4f47e232906dc7"
SHARED_CI_VERSION = "0.12.0"
REQUIRED_WORKFLOWS = {
    "actionlint.yml": ".github/workflows/actionlint.yml",
    "codeql.yml": ".github/workflows/public-codeql.yml",
    "dependency-review.yml": ".github/workflows/public-dependency-review.yml",
    "release.yml": ".github/workflows/release-supply-chain.yml",
    "scorecard.yml": ".github/workflows/public-scorecard-json.yml",
    "secret-scan.yml": ".github/workflows/secret-scan.yml",
    "zizmor.yml": ".github/workflows/zizmor-sarif.yml",
}
SETUP_ORDER = ["safe", "balanced", "full-auto"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    return parser.parse_args(argv)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


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


def main(argv: list[str] | None = None) -> int:
    parse_args(argv)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    build = load_json(ROOT / "build" / "version.json")
    manifest = load_json(ROOT / "build" / "manifest.json")
    contract = load_json(ROOT / "config" / "nddev-contract.json")
    baseline = load_json(ROOT / "references" / "junie-cli-baseline.json")
    ids = setup_ids()
    if version != "0.1.0":
        raise ValueError("VERSION must be 0.1.0")
    if build.get("build_version") != version or manifest.get("build_version") != version:
        raise ValueError("build version fields are not synchronized")
    if contract.get("version_ref") != "build/version.json":
        raise ValueError("contract version_ref must point at build/version.json")
    if contract.get("manifest_ref") != "build/manifest.json":
        raise ValueError("contract manifest_ref must point at build/manifest.json")
    if "skeleton" in contract:
        raise ValueError("contract must not expose skeleton status")
    if manifest.get("setup_ids") != ids or contract["setup_system"]["setup_ids"] != ids:
        raise ValueError("setup ids are not synchronized")
    if build.get("junie_cli_tested") != baseline["release"]["stable_version"]:
        raise ValueError("tested Junie CLI version differs from baseline release")
    if baseline["release"]["channel"] != "release":
        raise ValueError("baseline channel must be release")
    if baseline["runtime"]["command"] != "junie":
        raise ValueError("baseline command must be junie")
    if contract["plugin_marketplace"]["external_marketplace_published"] is not None:
        raise ValueError("external marketplace must remain null until published")
    if contract["plugin_marketplace"]["marketplace_manifest"] is not None:
        raise ValueError("marketplace manifest must remain null until published")
    for relative in (
        "builder/nddev-builder/skills/nddev-builder/SKILL.md",
        "builder/nddev-builder/agents/nddev-builder.md",
        "cli-tools/nddev_junie_cli.py",
    ):
        if not (ROOT / relative).is_file():
            raise ValueError(f"missing required public path {relative}")
    validate_workflows()
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"validate_public_contracts.py: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
