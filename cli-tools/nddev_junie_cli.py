#!/usr/bin/env python3
"""Transactional setup manager for an explicit Junie CLI target."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()
PRODUCT_NAME = "nddev-junie-cli-app"
SETUP_ROOT = ROOT / "setups"
BUILDER_ROOT = ROOT / "builder" / "nddev-builder"
SETUP_ORDER = ("safe", "balanced", "full-auto")
STAMP_NAME = "NDDEV-JUNIE-CLI-SETUP.json"
BACKUP_NAME = "NDDEV-JUNIE-CLI-BACKUP.json"
MANAGED_BEGIN = "<!-- BEGIN NDDEV-JUNIE-CLI MANAGED -->"
MANAGED_END = "<!-- END NDDEV-JUNIE-CLI MANAGED -->"
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700
MANAGED_MAX_BYTES = 8 * 1024 * 1024
METADATA_MAX_BYTES = 256 * 1024
CONTENT_MANAGED_PATHS = (
    "config.json",
    "allowlist.json",
    "AGENTS.md",
    "skills/nddev-builder/SKILL.md",
    "agents/nddev-builder.md",
    "mcp/mcp.json",
)
MERGED_MARKER_PATHS = {"AGENTS.md"}
MANAGED_JSON_KEYS = {
    "config.json": (
        "auto-update",
        "brave",
        "guidelines-location",
        "skill-locations",
        "skill-default-locations",
        "agent-locations",
        "agent-default-location",
        "mcp-locations",
        "mcp-default-locations",
        "command-locations",
        "command-default-locations",
        "model-default-locations",
    ),
    "allowlist.json": ("defaultBehavior", "allowReadonlyCommands", "rules"),
}
SETUP_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
PROVIDER_SECRET_NAMES = {
    "JUNIE_API_KEY",
    "JUNIE_ANTHROPIC_API_KEY",
    "JUNIE_OPENAI_API_KEY",
    "JUNIE_GOOGLE_API_KEY",
    "JUNIE_GROK_API_KEY",
    "JUNIE_OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GROK_API_KEY",
    "XAI_API_KEY",
    "OPENROUTER_API_KEY",
}


class JunieCliSetupError(Exception):
    """Safe user-facing lifecycle failure."""


def fail(message: str) -> NoReturn:
    raise JunieCliSetupError(message)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_absolute_target(raw: str) -> Path:
    target = Path(raw)
    if not target.is_absolute():
        fail("target must be an absolute path")
    if target.name in ("", ".", ".."):
        fail("target must name a directory")
    return target


def stat_existing(path: Path, label: str) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        fail(f"{label} must not be a symlink")
    return info


def require_real_directory(path: Path, label: str) -> os.stat_result:
    info = stat_existing(path, label)
    if info is None:
        fail(f"{label} is missing")
    if not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a directory")
    return info


def validate_target(target: Path, *, create: bool = False) -> Path:
    parent = target.parent
    parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    require_real_directory(parent, "target parent")
    info = stat_existing(target, "target")
    if info is None:
        if not create:
            return target.resolve(strict=False)
        target.mkdir(mode=OWNER_DIRECTORY_MODE)
        return target.resolve()
    if not stat.S_ISDIR(info.st_mode):
        fail("target must be a real directory")
    return target.resolve()


def backup_pool(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-junie-cli-backups"


def lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-junie-cli.lock"


@contextlib.contextmanager
def target_lock(target: Path):
    target.parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    require_real_directory(target.parent, "target parent")
    path = lock_path(target)
    try:
        path.mkdir(mode=OWNER_DIRECTORY_MODE)
    except FileExistsError:
        fail(f"target is locked: {path}")
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.rmdir()


def safe_target_path(target: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        fail(f"invalid managed path: {relative}")
    return target / candidate


def ensure_real_parent(path: Path, target: Path) -> None:
    relative_parent = path.relative_to(target).parent
    current = target
    for part in relative_parent.parts:
        current = current / part
        info = stat_existing(current, f"managed directory {current}")
        if info is None:
            current.mkdir(mode=OWNER_DIRECTORY_MODE)
            continue
        if not stat.S_ISDIR(info.st_mode):
            fail(f"managed parent is not a directory: {current}")


def require_existing_managed_file(
    path: Path, label: str, *, max_bytes: int
) -> os.stat_result | None:
    info = stat_existing(path, label)
    if info is None:
        return None
    if not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular file")
    if info.st_nlink != 1:
        fail(f"{label} must not be a hardlink")
    if info.st_size > max_bytes:
        fail(f"{label} is too large")
    return info


def read_existing_file(path: Path, *, max_bytes: int, label: str) -> bytes | None:
    require_existing_managed_file(path, label, max_bytes=max_bytes)
    if not path.exists():
        return None
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        fail(f"{label} is too large")
    return data


def atomic_write(path: Path, data: bytes, target: Path) -> None:
    ensure_real_parent(path, target)
    require_existing_managed_file(path, str(path), max_bytes=MANAGED_MAX_BYTES)
    temporary = path.with_name(f".{path.name}.nddev.tmp.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OWNER_FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
        path.chmod(OWNER_FILE_MODE)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise


def read_json_file(path: Path, *, max_bytes: int, label: str) -> dict[str, Any]:
    data = read_existing_file(path, max_bytes=max_bytes, label=label)
    if data is None:
        fail(f"{label} is missing")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"{label} is invalid JSON: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must contain a JSON object")
    return value


def parse_optional_json(data: bytes | None, label: str) -> dict[str, Any]:
    if data is None:
        return {}
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"{label} is invalid JSON: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must contain a JSON object")
    return value


def load_setup(setup_id: str) -> dict[str, Any]:
    if not SETUP_ID_PATTERN.fullmatch(setup_id):
        fail(f"invalid setup id: {setup_id}")
    path = SETUP_ROOT / setup_id / "setup.json"
    if not path.is_file():
        fail(f"unknown setup: {setup_id}")
    setup = read_json_file(path, max_bytes=METADATA_MAX_BYTES, label=f"setup {setup_id}")
    if setup.get("id") != setup_id:
        fail(f"setup id mismatch in {path}")
    return setup


def list_setups() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for setup_id in SETUP_ORDER:
        setup = load_setup(setup_id)
        items.append(
            {
                "id": setup["id"],
                "display_name": setup["display_name"],
                "description": setup["description"],
                "brave": setup["brave"],
                "allow_readonly_commands": setup["allow_readonly_commands"],
                "nddev_builder_default": setup["nddev_builder_default"],
            }
        )
    return items


def extract_managed_block(text: str) -> str | None:
    begin = text.find(MANAGED_BEGIN)
    if begin < 0:
        return None
    end = text.find(MANAGED_END, begin)
    if end < 0:
        return None
    end += len(MANAGED_END)
    if end < len(text) and text[end : end + 1] == "\n":
        end += 1
    return text[begin:end]


def merge_managed_block(existing: bytes | None, block: str) -> bytes:
    text = existing.decode("utf-8") if existing else ""
    current = extract_managed_block(text)
    if current is None:
        prefix = text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix:
            prefix += "\n"
        return (prefix + block).encode("utf-8")
    return text.replace(current, block).encode("utf-8")


def merge_json_object(existing: bytes | None, desired: dict[str, Any], label: str) -> bytes:
    value = parse_optional_json(existing, label)
    for key, item in desired.items():
        value[key] = item
    return canonical_json(value)


def render_config(target: Path, setup: dict[str, Any]) -> dict[str, Any]:
    canonical = validate_target(target, create=False)
    return {
        "brave": bool(setup["brave"]),
        "auto-update": false_json(),
        "guidelines-location": str((canonical / "AGENTS.md").resolve()),
        "skill-locations": [str((canonical / "skills").resolve())],
        "skill-default-locations": false_json(),
        "agent-locations": [str((canonical / "agents").resolve())],
        "agent-default-location": false_json(),
        "mcp-locations": [str((canonical / "mcp").resolve())],
        "mcp-default-locations": false_json(),
        "command-locations": [str((canonical / "commands").resolve())],
        "command-default-locations": false_json(),
        "model-default-locations": false_json(),
    }


def false_json() -> bool:
    return False


def render_agents_block(setup: dict[str, Any]) -> str:
    return (
        f"{MANAGED_BEGIN}\n"
        "\n"
        "# NDDev Junie CLI Setup\n"
        "\n"
        f"This target is managed as the `{setup['id']}` setup by nddev-junie-cli-app.\n"
        "Use the managed nddev-builder skill for setup artifact changes. Use only current\n"
        "Junie CLI surfaces confirmed in the public baseline: guidelines, skills, agents,\n"
        "MCP configuration, action allowlist, and explicit config locations.\n"
        "\n"
        f"{MANAGED_END}\n"
    )


def builder_source(relative: str) -> bytes:
    path = BUILDER_ROOT / relative
    if not path.is_file():
        fail(f"builder source missing: {relative}")
    data = path.read_bytes()
    if len(data) > MANAGED_MAX_BYTES:
        fail(f"builder source too large: {relative}")
    return data


def desired_files(target: Path, setup: dict[str, Any]) -> dict[str, bytes]:
    existing_config = read_existing_file(
        target / "config.json", max_bytes=MANAGED_MAX_BYTES, label="config.json"
    )
    existing_allowlist = read_existing_file(
        target / "allowlist.json", max_bytes=MANAGED_MAX_BYTES, label="allowlist.json"
    )
    existing_agents = read_existing_file(
        target / "AGENTS.md", max_bytes=MANAGED_MAX_BYTES, label="AGENTS.md"
    )
    return {
        "config.json": merge_json_object(existing_config, render_config(target, setup), "config.json"),
        "allowlist.json": merge_json_object(
            existing_allowlist, dict(setup["allowlist"]), "allowlist.json"
        ),
        "AGENTS.md": merge_managed_block(existing_agents, render_agents_block(setup)),
        "skills/nddev-builder/SKILL.md": builder_source("skills/nddev-builder/SKILL.md"),
        "agents/nddev-builder.md": builder_source("agents/nddev-builder.md"),
        "mcp/mcp.json": canonical_json({"mcpServers": {}}),
    }


def managed_digest_for_bytes(relative: str, data: bytes) -> str:
    if relative in MERGED_MARKER_PATHS:
        block = extract_managed_block(data.decode("utf-8"))
        if block is None:
            return ""
        return sha256_bytes(block.encode("utf-8"))
    if relative in MANAGED_JSON_KEYS:
        value = parse_optional_json(data, relative)
        subset = {key: value.get(key) for key in MANAGED_JSON_KEYS[relative] if key in value}
        return sha256_bytes(canonical_json(subset))
    return sha256_bytes(data)


def current_managed_digest(target: Path, relative: str) -> str | None:
    data = read_existing_file(
        safe_target_path(target, relative), max_bytes=MANAGED_MAX_BYTES, label=relative
    )
    if data is None:
        return None
    digest = managed_digest_for_bytes(relative, data)
    return digest or None


def stamp_path(target: Path) -> Path:
    return target / STAMP_NAME


def read_stamp(target: Path) -> dict[str, Any] | None:
    path = stamp_path(target)
    if not path.exists():
        return None
    stamp = read_json_file(path, max_bytes=METADATA_MAX_BYTES, label=STAMP_NAME)
    if stamp.get("product_name") != PRODUCT_NAME:
        fail("stamp belongs to another product")
    canonical = str(validate_target(target, create=False))
    if stamp.get("canonical_target") != canonical:
        fail("stamp is bound to a different canonical target")
    return stamp


def drift_for_stamp(target: Path, stamp: dict[str, Any]) -> list[str]:
    drift: list[str] = []
    managed = stamp.get("managed_files")
    if not isinstance(managed, dict):
        fail("stamp managed_files is invalid")
    for relative, expected in managed.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            fail("stamp managed file digest is invalid")
        current = current_managed_digest(target, relative)
        if current != expected:
            drift.append(relative)
    return drift


def status_payload(target: Path) -> dict[str, Any]:
    canonical = validate_target(target, create=False)
    if not target.exists():
        return {
            "state": "absent",
            "managed": False,
            "canonical_target": str(canonical),
            "setup_id": None,
            "drift": [],
        }
    require_real_directory(target, "target")
    stamp = read_stamp(target)
    if stamp is None:
        return {
            "state": "unmanaged",
            "managed": False,
            "canonical_target": str(canonical),
            "setup_id": None,
            "drift": [],
        }
    return {
        "state": "managed",
        "managed": True,
        "canonical_target": str(canonical),
        "setup_id": stamp["setup_id"],
        "build_version": stamp["build_version"],
        "drift": drift_for_stamp(target, stamp),
        "managed_files": sorted(stamp["managed_files"]),
    }


def snapshot_files(target: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for relative in (*CONTENT_MANAGED_PATHS, STAMP_NAME):
        snapshot[relative] = read_existing_file(
            safe_target_path(target, relative), max_bytes=MANAGED_MAX_BYTES, label=relative
        )
    return snapshot


def restore_snapshot(target: Path, snapshot: dict[str, bytes | None]) -> None:
    for relative, data in snapshot.items():
        path = safe_target_path(target, relative)
        if data is None:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        atomic_write(path, data, target)
    prune_empty_managed_dirs(target)


def choose_backup_slot(pool: Path) -> int:
    pool.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    for slot in range(10):
        if not (pool / str(slot)).exists():
            return slot
    return min(range(10), key=lambda item: (pool / str(item)).stat().st_mtime_ns)


def create_backup(target: Path, stamp: dict[str, Any]) -> int:
    pool = backup_pool(target)
    slot = choose_backup_slot(pool)
    slot_dir = pool / str(slot)
    if slot_dir.exists():
        shutil.rmtree(slot_dir)
    slot_dir.mkdir(mode=OWNER_DIRECTORY_MODE)
    files: dict[str, Any] = {}
    for relative in (*CONTENT_MANAGED_PATHS, STAMP_NAME):
        data = read_existing_file(
            safe_target_path(target, relative), max_bytes=MANAGED_MAX_BYTES, label=relative
        )
        files[relative] = None if data is None else base64.b64encode(data).decode("ascii")
    envelope = {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "slot": slot,
        "canonical_target": str(validate_target(target, create=False)),
        "source_setup_id": stamp["setup_id"],
        "created_at": int(time.time()),
        "files": files,
    }
    atomic_write(slot_dir / BACKUP_NAME, canonical_json(envelope), slot_dir)
    return slot


def build_stamp(target: Path, setup_id: str, files: dict[str, bytes]) -> dict[str, Any]:
    managed = {
        relative: managed_digest_for_bytes(relative, data) for relative, data in files.items()
    }
    return {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "setup_id": setup_id,
        "canonical_target": str(validate_target(target, create=False)),
        "managed_files": managed,
    }


def write_setup(
    target: Path, setup: dict[str, Any], *, require_existing: bool = False
) -> dict[str, Any]:
    with target_lock(target):
        validate_target(target, create=True)
        current = read_stamp(target)
        if require_existing and current is None:
            fail("switch requires an already managed target")
        if current is not None:
            drift = drift_for_stamp(target, current)
            if drift:
                fail(f"managed target has drift: {', '.join(drift)}")
        files = desired_files(target, setup)
        desired_stamp = build_stamp(target, setup["id"], files)
        changed = [
            relative
            for relative, data in files.items()
            if current_managed_digest(target, relative) != managed_digest_for_bytes(relative, data)
        ]
        backup_slot = None
        if current is not None and current["setup_id"] != setup["id"]:
            backup_slot = create_backup(target, current)
        snapshot = snapshot_files(target)
        try:
            for relative, data in files.items():
                atomic_write(safe_target_path(target, relative), data, target)
            atomic_write(stamp_path(target), canonical_json(desired_stamp), target)
        except BaseException:
            restore_snapshot(target, snapshot)
            raise
        return {
            "setup_id": setup["id"],
            "changed": changed,
            "backup_slot": backup_slot,
            "target": str(validate_target(target, create=False)),
        }


def restore_backup(target: Path, slot: int) -> dict[str, Any]:
    if slot < 0 or slot > 9:
        fail("backup slot must be between 0 and 9")
    with target_lock(target):
        validate_target(target, create=True)
        envelope_path = backup_pool(target) / str(slot) / BACKUP_NAME
        envelope = read_json_file(envelope_path, max_bytes=METADATA_MAX_BYTES, label=BACKUP_NAME)
        if envelope.get("product_name") != PRODUCT_NAME:
            fail("backup belongs to another product")
        if envelope.get("canonical_target") != str(validate_target(target, create=False)):
            fail("backup is bound to a different canonical target")
        files = envelope.get("files")
        if not isinstance(files, dict):
            fail("backup files are invalid")
        snapshot = snapshot_files(target)
        try:
            for relative in (*CONTENT_MANAGED_PATHS, STAMP_NAME):
                encoded = files.get(relative)
                path = safe_target_path(target, relative)
                if encoded is None:
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
                    continue
                if not isinstance(encoded, str):
                    fail("backup file payload is invalid")
                atomic_write(path, base64.b64decode(encoded.encode("ascii")), target)
            prune_empty_managed_dirs(target)
        except BaseException:
            restore_snapshot(target, snapshot)
            raise
        restored_stamp = read_stamp(target)
        return {
            "setup_id": None if restored_stamp is None else restored_stamp["setup_id"],
            "backup_slot": slot,
            "target": str(validate_target(target, create=False)),
        }


def remove_setup(target: Path) -> dict[str, Any]:
    with target_lock(target):
        validate_target(target, create=False)
        stamp = read_stamp(target)
        if stamp is None:
            return {"removed_setup_id": None, "target": str(validate_target(target, create=False))}
        drift = drift_for_stamp(target, stamp)
        if drift:
            fail(f"managed target has drift: {', '.join(drift)}")
        removed_setup_id = stamp["setup_id"]
        snapshot = snapshot_files(target)
        try:
            for relative in CONTENT_MANAGED_PATHS:
                if relative in MANAGED_JSON_KEYS:
                    remove_managed_json_keys(target, relative)
                elif relative in MERGED_MARKER_PATHS:
                    remove_managed_block_from_target(target, relative)
                else:
                    with contextlib.suppress(FileNotFoundError):
                        safe_target_path(target, relative).unlink()
            with contextlib.suppress(FileNotFoundError):
                stamp_path(target).unlink()
            prune_empty_managed_dirs(target)
        except BaseException:
            restore_snapshot(target, snapshot)
            raise
        return {
            "removed_setup_id": removed_setup_id,
            "target": str(validate_target(target, create=False)),
        }


def remove_managed_json_keys(target: Path, relative: str) -> None:
    path = safe_target_path(target, relative)
    data = read_existing_file(path, max_bytes=MANAGED_MAX_BYTES, label=relative)
    if data is None:
        return
    value = parse_optional_json(data, relative)
    for key in MANAGED_JSON_KEYS[relative]:
        value.pop(key, None)
    if value:
        atomic_write(path, canonical_json(value), target)
    else:
        path.unlink()


def remove_managed_block_from_target(target: Path, relative: str) -> None:
    path = safe_target_path(target, relative)
    data = read_existing_file(path, max_bytes=MANAGED_MAX_BYTES, label=relative)
    if data is None:
        return
    text = data.decode("utf-8")
    block = extract_managed_block(text)
    if block is None:
        return
    updated = text.replace(block, "")
    if updated.strip():
        atomic_write(path, updated.encode("utf-8"), target)
    else:
        path.unlink()


def prune_empty_managed_dirs(target: Path) -> None:
    candidates: set[Path] = set()
    for relative in CONTENT_MANAGED_PATHS:
        directory = safe_target_path(target, relative).parent
        while directory != target and target in directory.parents:
            candidates.add(directory)
            directory = directory.parent
    directories = sorted(candidates, key=lambda item: len(item.parts), reverse=True)
    for directory in directories:
        with contextlib.suppress(OSError):
            directory.rmdir()


def plan_payload(target: Path, setup: dict[str, Any]) -> dict[str, Any]:
    status = status_payload(target)
    operation = "install"
    backup_required = False
    if status["managed"]:
        operation = "update" if status["setup_id"] == setup["id"] else "switch"
        backup_required = status["setup_id"] != setup["id"]
    return {
        "operation": operation,
        "setup_id": setup["id"],
        "target": str(validate_target(target, create=False)),
        "current_setup_id": status["setup_id"],
        "drift": status["drift"],
        "backup_required": backup_required,
        "mutates": False,
    }


def launch(target: Path, child_args: list[str]) -> int:
    status = status_payload(target)
    if not status["managed"]:
        fail("launch requires a managed target")
    if status["drift"]:
        fail(f"managed target has drift: {', '.join(status['drift'])}")
    canonical = validate_target(target, create=False)
    runtime = canonical / ".nddev-junie-cli-runtime"
    home = runtime / "home"
    data = runtime / "data"
    cache = runtime / "cache"
    for directory in (home, data, cache):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    child_env: dict[str, str] = {
        "HOME": str(home),
        "JUNIE_DATA": str(data),
        "JUNIE_CONFIG_LOCATION": str((canonical / "config.json").resolve()),
        "JUNIE_CONFIG_DEFAULT_LOCATIONS": "false",
        "JUNIE_SKILL_LOCATIONS": str((canonical / "skills").resolve()),
        "JUNIE_SKILL_DEFAULT_LOCATIONS": "false",
        "JUNIE_AGENT_LOCATIONS": str((canonical / "agents").resolve()),
        "JUNIE_AGENT_DEFAULT_LOCATIONS": "false",
        "JUNIE_MCP_LOCATIONS": str((canonical / "mcp").resolve()),
        "JUNIE_MCP_DEFAULT_LOCATIONS": "false",
        "JUNIE_EXTENSIONS_DEFAULT_LOCATION": str((canonical / "extensions").resolve()),
        "JUNIE_GUIDELINES_FILENAME": str((canonical / "AGENTS.md").resolve()),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    for name in ("TERM", "COLORTERM", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SYSTEMROOT"):
        value = os.environ.get(name)
        if value:
            child_env[name] = value
    for name in PROVIDER_SECRET_NAMES:
        child_env.pop(name, None)
    command = ["junie"]
    if "--skip-update-check" not in child_args:
        command.append("--skip-update-check")
    command.extend(child_args)
    try:
        completed = subprocess.run(command, env=child_env, check=False)
    except FileNotFoundError:
        fail("junie command was not found on PATH")
    return int(completed.returncode)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true")
    for name in ("status", "remove"):
        command = subparsers.add_parser(name)
        command.add_argument("--target", required=True)
        command.add_argument("--json", action="store_true")
    for name in ("plan", "install", "switch"):
        command = subparsers.add_parser(name)
        command.add_argument("--setup", required=True)
        command.add_argument("--target", required=True)
        command.add_argument("--json", action="store_true")
    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup", required=True, type=int)
    restore.add_argument("--target", required=True)
    restore.add_argument("--json", action="store_true")
    launch_parser = subparsers.add_parser("launch")
    launch_parser.add_argument("--target", required=True)
    launch_parser.add_argument("child_args", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def emit(payload: dict[str, Any], *, as_json: bool) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "list":
        items = list_setups()
        emit({"setups": [item["id"] for item in items], "items": items}, as_json=args.json)
        return 0
    if args.command == "status":
        emit(status_payload(require_absolute_target(args.target)), as_json=args.json)
        return 0
    if args.command == "plan":
        target = require_absolute_target(args.target)
        emit(plan_payload(target, load_setup(args.setup)), as_json=args.json)
        return 0
    if args.command == "install":
        target = require_absolute_target(args.target)
        emit(write_setup(target, load_setup(args.setup)), as_json=args.json)
        return 0
    if args.command == "switch":
        target = require_absolute_target(args.target)
        emit(write_setup(target, load_setup(args.setup), require_existing=True), as_json=args.json)
        return 0
    if args.command == "restore":
        target = require_absolute_target(args.target)
        emit(restore_backup(target, args.backup), as_json=args.json)
        return 0
    if args.command == "remove":
        target = require_absolute_target(args.target)
        emit(remove_setup(target), as_json=args.json)
        return 0
    if args.command == "launch":
        child_args = list(args.child_args)
        if child_args[:1] == ["--"]:
            child_args = child_args[1:]
        return launch(require_absolute_target(args.target), child_args)
    fail(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return dispatch(args)
    except JunieCliSetupError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
