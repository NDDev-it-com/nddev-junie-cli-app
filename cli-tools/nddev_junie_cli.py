#!/usr/bin/env python3
"""Transactional setup manager for an explicit Junie CLI target."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()
PRODUCT_NAME = "nddev-junie-cli-app"
SETUP_ROOT = ROOT / "setups"
PROFILE_ROOT = ROOT / "profiles"
BUILDER_ROOT = ROOT / "builder" / "nddev-builder"
BASELINE_PATH = ROOT / "references" / "junie-cli-baseline.json"
DEFAULT_SETUP_ID = "nddev-builder"
SETUP_ORDER = ("nddev-builder",)
DEFAULT_PROFILE_ID = "full-auto"
PROFILE_ORDER = ("safe", "full-auto")
LEGACY_SETUP_IDS = ("safe", "full-auto", "balanced")
STAMP_NAME = "NDDEV-JUNIE-CLI-SETUP.json"
BACKUP_NAME = "NDDEV-JUNIE-CLI-BACKUP.json"
STAMP_SCHEMA = 3
LEGACY_STAMP_SCHEMAS = (1, 2)
LEGACY_STAMP_SCHEMA = 1
RUNTIME_DIR_NAME = ".nddev-junie-cli-runtime"
RUNTIME_RECEIPT_NAME = "NDDEV-JUNIE-CLI-RUNTIME.json"
LOCK_DIR_NAME = ".nddev-junie-cli.lock"
BACKUP_DIR_NAME = ".nddev-junie-cli-backups"
MANAGED_BEGIN = "<!-- BEGIN NDDEV-JUNIE-CLI MANAGED -->"
MANAGED_END = "<!-- END NDDEV-JUNIE-CLI MANAGED -->"
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700
OWNER_EXECUTABLE_MODE = 0o700
SAFE_SUBPROCESS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
MANAGED_MAX_BYTES = 8 * 1024 * 1024
METADATA_MAX_BYTES = 256 * 1024
INSTALLER_MAX_BYTES = 2 * 1024 * 1024
UPDATE_INFO_MAX_BYTES = 8 * 1024 * 1024
SOFTWARE_FILE_MAX_BYTES = 1024 * 1024 * 1024
INSTALL_TIMEOUT_SECONDS = 900
PROBE_TIMEOUT_SECONDS = 30
BASE_CONTROL_MANAGED_PATHS = (
    "config.json",
    "AGENTS.md",
    "mcp/mcp.json",
)
RUNTIME_ALLOWLIST_RELATIVE = f"{RUNTIME_DIR_NAME}/home/.junie/allowlist.json"
MERGED_MARKER_PATHS = {"AGENTS.md"}
LEGACY_MANAGED_JSON_KEYS = {
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
TARGET_SCOPE_FLAGS = {
    "--project",
    "-p",
    "--cache-dir",
    "-c",
    "--config-location",
    "--config-default-locations",
    "--mcp-location",
    "--mcp-default-locations",
    "--skill-location",
    "--skill-default-locations",
    "--agent-location",
    "--agent-default-location",
    "--command-location",
    "--command-default-locations",
    "--extensions-default-location",
    "--guidelines-filename",
    "--model-location",
    "--model-default-locations",
    "--model",
    "--effort",
    "--provider",
    "--auth",
    "-a",
    "--auth-license",
    "--anthropic-api-key",
    "--google-api-key",
    "--grok-api-key",
    "--openai-api-key",
    "--openrouter-api-key",
}


class JunieCliSetupError(Exception):
    """Safe user-facing lifecycle failure."""


@dataclass
class FileSnapshot:
    exists: bool
    data: bytes | None = None
    mode: int | None = None


@dataclass
class TransactionSnapshot:
    target_existed: bool
    target_mode: int | None
    files: dict[str, FileSnapshot]


@dataclass
class SoftwareTransaction:
    metadata: dict[str, Any]
    changed: bool
    previous_home: Path | None = None
    new_home: Path | None = None


@dataclass
class RuntimeRemoveTransaction:
    changed: bool
    removed_root: Path | None = None


@dataclass(frozen=True)
class BackupRestorePlan:
    files: dict[str, bytes]
    stamp: dict[str, Any]
    relatives: list[str]


@dataclass(frozen=True)
class LiveJunieHomeSnapshot:
    exists: bool
    inode: int | None = None
    mode: int | None = None
    size: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    is_symlink: bool = False


@dataclass(frozen=True)
class LaunchFileSnapshot:
    label: str
    relative_path: str
    device: int
    inode: int
    size: int
    sha256: str
    max_bytes: int


@dataclass(frozen=True)
class LaunchPlan:
    command: list[str]
    child_env: dict[str, str]
    cwd: Path
    shim: LaunchFileSnapshot
    binary: LaunchFileSnapshot


@dataclass
class DirectoryTransaction:
    created: list[Path]

    def cleanup(self) -> None:
        for path in reversed(self.created):
            with contextlib.suppress(OSError):
                path.rmdir()


class RuntimeValidationError(JunieCliSetupError):
    """Structured target-runtime validation failure."""

    def __init__(self, message: str, *, code: str, repairable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.repairable = repairable


def fail(message: str) -> NoReturn:
    raise JunieCliSetupError(message)


def runtime_fail(message: str, *, code: str, repairable: bool) -> NoReturn:
    raise RuntimeValidationError(message, code=code, repairable=repairable)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file_bounded(path: Path, *, max_bytes: int, label: str) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                fail(f"{label} is too large")
            digest.update(chunk)
    return digest.hexdigest()


def java_option_quote(path: Path) -> str:
    value = str(path)
    if "\x00" in value or "\n" in value or "\r" in value:
        fail("Java runtime isolation path contains unsupported control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def java_isolation_options(home: Path, tmp: Path) -> str:
    return " ".join(
        (
            f"-Duser.home={java_option_quote(home)}",
            f"-Djava.io.tmpdir={java_option_quote(tmp)}",
        )
    )


def account_junie_home() -> Path:
    if os.name != "posix":
        fail("Junie live-state guard requires a POSIX account home")
    import pwd

    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".junie"
    except KeyError:
        fail("cannot resolve account home for Junie live-state guard")


def snapshot_live_junie_home() -> LiveJunieHomeSnapshot:
    path = account_junie_home()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return LiveJunieHomeSnapshot(exists=False)
    return LiveJunieHomeSnapshot(
        exists=True,
        inode=info.st_ino,
        mode=stat.S_IMODE(info.st_mode),
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
        is_symlink=stat.S_ISLNK(info.st_mode),
    )


def require_live_junie_home_unchanged(
    before: LiveJunieHomeSnapshot, *, context: str
) -> None:
    after = snapshot_live_junie_home()
    if after != before:
        fail(f"{context} changed the live account Junie home; target isolation failed")


def read_path_bounded(path: Path, *, max_bytes: int, label: str) -> bytes:
    info = stat_existing(path, label)
    if info is None:
        fail(f"{label} is missing")
    require_current_owner(info, label)
    if not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular file")
    if info.st_size > max_bytes:
        fail(f"{label} is too large")
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        fail(f"{label} is too large")
    return data


def read_url_bounded(url: str, *, max_bytes: int, label: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        fail(f"{label} must use HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": PRODUCT_NAME})
    try:
        response_context = urllib.request.urlopen(request, timeout=30)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        fail(f"{label} download failed: {exc}")
    with response_context as response:
        content_length = response.headers.get("Content-Length")
        if content_length is None:
            fail(f"{label} Content-Length is missing")
        try:
            declared = int(content_length)
        except ValueError:
            fail(f"{label} Content-Length is invalid")
        if declared > max_bytes:
            fail(f"{label} Content-Length is too large")
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    fail(f"{label} is too large")
                chunks.append(chunk)
        except (TimeoutError, OSError) as exc:
            fail(f"{label} download failed: {exc}")
        if total != declared:
            fail(f"{label} download size does not match Content-Length")
        return b"".join(chunks)


def load_baseline() -> dict[str, Any]:
    return read_json_file(BASELINE_PATH, max_bytes=METADATA_MAX_BYTES, label="baseline")


def installer_source(baseline: dict[str, Any]) -> tuple[str, str]:
    installer = baseline["release"]["installer"]
    return installer["url"], installer["sha256"]


def update_info_source(baseline: dict[str, Any]) -> str:
    return baseline["release"]["update_info"]


def current_platform_id() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux":
        os_name = "linux"
    elif system == "Darwin":
        os_name = "macos"
    else:
        fail(f"unsupported OS: {system}")
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    else:
        fail(f"unsupported architecture: {machine}")
    return f"{os_name}-{arch}"


def parse_update_info(data: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        fail(f"update-info is not UTF-8: {exc}")
    for index, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            fail(f"update-info line {index} is invalid JSON: {exc}")
        if not isinstance(value, dict):
            fail(f"update-info line {index} must be a JSON object")
        rows.append(value)
    return rows


def artifact_metadata_from_update_info(
    baseline: dict[str, Any], update_info_url: str
) -> dict[str, Any]:
    data = read_url_bounded(
        update_info_url, max_bytes=UPDATE_INFO_MAX_BYTES, label="update-info.jsonl"
    )
    rows = parse_update_info(data)
    version = baseline["release"]["exact_version"]
    expected = baseline["release"]["exact_artifacts"]
    matches = {row.get("platform"): row for row in rows if row.get("version") == version}
    missing = sorted(set(expected) - set(matches))
    if missing:
        fail(f"update-info is missing Junie {version} artifacts: {', '.join(missing)}")
    for platform_id, artifact in expected.items():
        row = matches[platform_id]
        common_expected = {
            "marketing": baseline["release"]["marketing_version"],
            "version": version,
            "platform": platform_id,
        }
        for key, expected_value in common_expected.items():
            if row.get(key) != expected_value:
                fail(f"update-info {platform_id} {key} does not match the baseline")
        exact_expected = {
            "downloadUrl": artifact["download_url"],
            "sha256": artifact["sha256"],
            "size": artifact["size"],
        }
        for key, expected_value in exact_expected.items():
            if row.get(key) != expected_value:
                fail(f"update-info {platform_id} {key} does not match the baseline")
    platform_id = current_platform_id()
    if platform_id not in expected:
        fail(f"Junie {version} artifact is not pinned for {platform_id}")
    selected_row = matches[platform_id]
    selected = {
        "download_url": selected_row["downloadUrl"],
        "sha256": selected_row["sha256"],
        "size": selected_row["size"],
    }
    selected["platform"] = platform_id
    return selected


def verify_artifact_binding(artifact: dict[str, Any]) -> dict[str, Any]:
    url = str(artifact["download_url"])
    expected_size = int(artifact["size"])
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        fail("Junie artifact URL must use HTTPS")
    if parsed.netloc.lower() != "github.com" or not parsed.path.startswith("/JetBrains/junie/"):
        fail("Junie artifact URL must point at the official JetBrains Junie release")
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": PRODUCT_NAME})
    try:
        response_context = urllib.request.urlopen(request, timeout=30)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        fail(f"Junie artifact HEAD failed: {exc}")
    with response_context as response:
        content_length = response.headers.get("Content-Length")
        if content_length is None:
            fail("Junie artifact Content-Length is missing")
        try:
            declared = int(content_length)
        except ValueError:
            fail("Junie artifact Content-Length is invalid")
        if declared != expected_size:
            fail("Junie artifact Content-Length does not match update-info")
    return {"size_verified": True, "sha256_verified": False, "method": "head"}


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


def current_user_id() -> int | None:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None
    return int(getuid())


def is_current_owner(info: os.stat_result) -> bool:
    uid = current_user_id()
    return uid is None or info.st_uid == uid


def require_current_owner(info: os.stat_result, label: str) -> None:
    if not is_current_owner(info):
        fail(f"{label} must be owned by the current user")


def lstat_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def ensure_directory_chain(path: Path, transaction: DirectoryTransaction, label: str) -> None:
    missing: list[Path] = []
    current = path
    while True:
        info = stat_existing(current, label)
        if info is not None:
            if not stat.S_ISDIR(info.st_mode):
                fail(f"{label} must be a directory")
            break
        missing.append(current)
        parent = current.parent
        if parent == current:
            fail(f"{label} parent is missing")
        current = parent
    require_safe_target_parent(current, label)
    for directory in reversed(missing):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE)
        transaction.created.append(directory)
        require_private_directory(directory, label)


def require_real_directory(path: Path, label: str) -> os.stat_result:
    info = stat_existing(path, label)
    if info is None:
        fail(f"{label} is missing")
    require_current_owner(info, label)
    if not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a directory")
    return info


def require_private_directory(path: Path, label: str) -> os.stat_result:
    info = require_real_directory(path, label)
    if stat.S_IMODE(info.st_mode) & 0o077:
        fail(f"{label} must be private")
    return info


def require_safe_target_parent(path: Path, label: str) -> os.stat_result:
    info = stat_existing(path, label)
    if info is None:
        fail(f"{label} is missing")
    if not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a directory")
    mode = stat.S_IMODE(info.st_mode)
    if is_current_owner(info) and not (mode & 0o022):
        return info
    if (mode & stat.S_ISVTX) and (mode & 0o002):
        return info
    fail(f"{label} must be current-user-owned private or sticky")


def resolve_parent_allowing_missing(path: Path) -> Path:
    missing: list[str] = []
    current = path
    while True:
        try:
            resolved = current.resolve(strict=True)
        except FileNotFoundError:
            if current.parent == current:
                fail(f"path parent is missing: {path}")
            missing.append(current.name)
            current = current.parent
            continue
        result = resolved
        for part in reversed(missing):
            result = result / part
        return result


def canonicalize_target_parent(target: Path) -> Path:
    return resolve_parent_allowing_missing(target.parent) / target.name


def ensure_private_directory(path: Path, target: Path, label: str) -> None:
    if target not in path.parents and path != target:
        fail(f"{label} escaped managed target")
    info = stat_existing(path, label)
    if info is None:
        path.mkdir(mode=OWNER_DIRECTORY_MODE)
        require_private_directory(path, label)
        return
    require_private_directory(path, label)


def safe_rmtree_private_directory(path: Path, label: str) -> None:
    require_private_directory(path, label)
    shutil.rmtree(path)


def safe_rmtree_private_directory_if_exists(path: Path, label: str) -> None:
    if lstat_exists(path):
        safe_rmtree_private_directory(path, label)


def chmod_private_directory(path: Path, mode: int, label: str) -> None:
    require_real_directory(path, label)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def trusted_executable(candidates: tuple[Path, ...], label: str) -> Path:
    uid = current_user_id()
    for candidate in candidates:
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve(strict=True)
            info = resolved.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
            continue
        if uid is not None and info.st_uid not in (0, uid):
            continue
        if stat.S_IMODE(info.st_mode) & 0o022:
            continue
        trusted = True
        for parent in resolved.parents:
            try:
                parent_info = parent.lstat()
            except OSError:
                trusted = False
                break
            parent_mode = stat.S_IMODE(parent_info.st_mode)
            if not stat.S_ISDIR(parent_info.st_mode):
                trusted = False
                break
            if uid is not None and parent_info.st_uid not in (0, uid):
                trusted = False
                break
            if parent_mode & 0o022:
                trusted = False
                break
        if trusted:
            return resolved
    fail(f"{label} executable is unavailable from trusted absolute paths")


def trusted_bash() -> Path:
    return trusted_executable((Path("/bin/bash"), Path("/usr/bin/bash")), "bash")


def trusted_python() -> Path:
    candidates = tuple(
        dict.fromkeys(
            [
                Path("/usr/bin/python3"),
                Path("/opt/homebrew/bin/python3"),
                Path("/usr/local/bin/python3"),
                Path(sys.executable),
            ]
        )
    )
    return trusted_executable(candidates, "python3")


def create_lock_directory(path: Path, transaction: DirectoryTransaction) -> None:
    try:
        path.mkdir(mode=OWNER_DIRECTORY_MODE)
    except FileExistsError:
        try:
            info = stat_existing(path, "target lock")
            if info is not None:
                require_current_owner(info, "target lock")
                if not stat.S_ISDIR(info.st_mode):
                    fail("target lock must be a directory")
                if stat.S_IMODE(info.st_mode) & 0o077:
                    fail("target lock must be private")
        finally:
            transaction.cleanup()
        fail(f"target is locked: {path}")
    except BaseException:
        transaction.cleanup()
        fail(f"target is locked: {path}")
    require_private_directory(path, "target lock")


def remove_lock_directory(path: Path) -> None:
    try:
        require_private_directory(path, "target lock")
        path.rmdir()
    except (FileNotFoundError, OSError, JunieCliSetupError):
        pass


def validate_target(
    target: Path, *, create: bool = False, transaction: DirectoryTransaction | None = None
) -> Path:
    target = canonicalize_target_parent(target)
    parent = target.parent
    if create:
        ensure_directory_chain(parent, transaction or DirectoryTransaction([]), "target parent")
    parent_info = stat_existing(parent, "target parent")
    if parent_info is None:
        if create:
            fail("target parent is missing")
        return target.resolve(strict=False)
    if not stat.S_ISDIR(parent_info.st_mode):
        fail("target parent must be a directory")
    require_safe_target_parent(parent, "target parent")
    info = stat_existing(target, "target")
    if info is None:
        if not create:
            return target.resolve(strict=False)
        try:
            target.mkdir(mode=OWNER_DIRECTORY_MODE)
        except FileExistsError:
            fail("target appeared during creation")
        if transaction is not None:
            transaction.created.append(target)
        require_private_directory(target, "target")
        return target.resolve()
    if not stat.S_ISDIR(info.st_mode):
        fail("target must be a real directory")
    require_private_directory(target, "target")
    return target.resolve()


def backup_pool(target: Path) -> Path:
    return target / BACKUP_DIR_NAME


def lock_path(target: Path) -> Path:
    return target / LOCK_DIR_NAME


def runtime_root(target: Path) -> Path:
    return target / RUNTIME_DIR_NAME


def runtime_home(target: Path) -> Path:
    return runtime_root(target) / "home"


def runtime_data(target: Path) -> Path:
    return runtime_home(target) / ".local" / "share" / "junie"


def runtime_bin(target: Path) -> Path:
    return runtime_home(target) / ".local" / "bin" / "junie"


def runtime_cache(target: Path) -> Path:
    return runtime_root(target) / "cache"


def runtime_tmp(target: Path) -> Path:
    return runtime_root(target) / "tmp"


def runtime_receipt_path(target: Path) -> Path:
    return runtime_root(target) / RUNTIME_RECEIPT_NAME


@contextlib.contextmanager
def target_lock(target: Path, *, create_parent: bool = False):
    transaction = DirectoryTransaction([])
    if create_parent:
        canonical = validate_target(target, create=True, transaction=transaction)
    else:
        canonical = validate_target(target, create=False)
        if not lstat_exists(canonical):
            fail("target is missing")
    path = lock_path(canonical)
    create_lock_directory(path, transaction)
    failed = False
    try:
        yield transaction
    except BaseException:
        failed = True
        raise
    finally:
        remove_lock_directory(path)
        if failed:
            transaction.cleanup()


def safe_target_path(target: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        fail(f"invalid managed path: {relative}")
    return target / candidate


def relative_to_target(target: Path, path: Path) -> str:
    try:
        return str(path.relative_to(target))
    except ValueError:
        fail(f"path escaped managed target: {path}")


def require_runtime_directory(path: Path, target: Path, label: str) -> os.stat_result:
    if target not in path.parents and path != target:
        fail(f"{label} escaped managed target")
    return require_real_directory(path, label)


def require_runtime_file(path: Path, target: Path, label: str) -> os.stat_result:
    if target not in path.parents:
        fail(f"{label} escaped managed target")
    info = stat_existing(path, label)
    if info is None:
        fail(f"{label} is missing")
    require_current_owner(info, label)
    if not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular file")
    if info.st_nlink != 1:
        fail(f"{label} must not be a hardlink")
    return info


def ensure_real_parent(path: Path, target: Path) -> None:
    relative_parent = path.relative_to(target).parent
    current = target
    for part in relative_parent.parts:
        current = current / part
        info = stat_existing(current, f"managed directory {current}")
        if info is None:
            current.mkdir(mode=OWNER_DIRECTORY_MODE)
            continue
        require_current_owner(info, f"managed directory {current}")
        if not stat.S_ISDIR(info.st_mode):
            fail(f"managed parent is not a directory: {current}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            fail(f"managed parent must be private: {current}")


def require_existing_managed_file(
    path: Path, label: str, *, max_bytes: int
) -> os.stat_result | None:
    info = stat_existing(path, label)
    if info is None:
        return None
    require_current_owner(info, label)
    if not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular file")
    if info.st_nlink != 1:
        fail(f"{label} must not be a hardlink")
    if info.st_size > max_bytes:
        fail(f"{label} is too large")
    return info


def read_existing_file(path: Path, *, max_bytes: int, label: str) -> bytes | None:
    require_existing_managed_file(path, label, max_bytes=max_bytes)
    if not lstat_exists(path):
        return None
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        fail(f"{label} is too large")
    return data


def atomic_write(path: Path, data: bytes, target: Path, *, mode: int = OWNER_FILE_MODE) -> None:
    ensure_real_parent(path, target)
    require_existing_managed_file(path, str(path), max_bytes=MANAGED_MAX_BYTES)
    temporary = path.with_name(f".{path.name}.nddev.tmp.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
        os.replace(temporary, path)
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
    setup = read_json_file(path, max_bytes=METADATA_MAX_BYTES, label=f"setup {setup_id}")
    if setup.get("id") != setup_id:
        fail(f"setup id mismatch in {path}")
    return setup


def load_profile(profile_id: str) -> dict[str, Any]:
    if not SETUP_ID_PATTERN.fullmatch(profile_id):
        fail(f"invalid profile id: {profile_id}")
    path = PROFILE_ROOT / profile_id / "profile.json"
    profile = read_json_file(path, max_bytes=METADATA_MAX_BYTES, label=f"profile {profile_id}")
    if profile.get("id") != profile_id:
        fail(f"profile id mismatch in {path}")
    return profile


def list_setups() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for setup_id in SETUP_ORDER:
        setup = load_setup(setup_id)
        items.append(
            {
                "id": setup["id"],
                "display_name": setup["display_name"],
                "description": setup["description"],
                "nddev_builder_default": setup["nddev_builder_default"],
                "content_projection": setup["content_projection"],
            }
        )
    return items


def list_profiles() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for profile_id in PROFILE_ORDER:
        profile = load_profile(profile_id)
        items.append(
            {
                "id": profile["id"],
                "display_name": profile["display_name"],
                "description": profile["description"],
                "brave": profile["brave"],
                "allow_readonly_commands": profile["allow_readonly_commands"],
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


def posix_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def read_builder_tree(relative_root: str) -> dict[str, bytes]:
    root = BUILDER_ROOT / relative_root
    if not root.is_dir():
        return {}
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = posix_relative(path, root)
        if any(part.startswith(".") for part in Path(relative).parts):
            continue
        info = stat_existing(path, f"builder source {relative_root}/{relative}")
        if info is None:
            continue
        require_current_owner(info, f"builder source {relative_root}/{relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            fail(f"builder source {relative_root}/{relative} must be a regular file")
        if info.st_nlink != 1:
            fail(f"builder source {relative_root}/{relative} must not be a hardlink")
        if info.st_size > MANAGED_MAX_BYTES:
            fail(f"builder source {relative_root}/{relative} is too large")
        files[relative] = read_path_bounded(
            path,
            max_bytes=MANAGED_MAX_BYTES,
            label=f"builder source {relative_root}/{relative}",
        )
    return files


def quoted(value: Path | str) -> str:
    return shlex.quote(str(value))


def render_config(target: Path, setup: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    canonical = validate_target(target, create=False)
    hook_command = (
        f"{quoted(trusted_python())} {quoted(canonical / 'hooks' / 'nddev-builder-context.py')} "
        f"--target {quoted(canonical)}"
    )
    return {
        "brave": bool(profile["brave"]),
        "skill-locations": [str((canonical / "skills").resolve())],
        "skill-default-locations": false_json(),
        "agent-locations": [str((canonical / "agents").resolve())],
        "agent-default-location": false_json(),
        "mcp-locations": [str((canonical / "mcp").resolve())],
        "mcp-default-locations": false_json(),
        "command-locations": [str((canonical / "commands").resolve())],
        "command-default-locations": false_json(),
        "model-default-locations": false_json(),
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Write|Edit|Read|Grep|Glob",
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command,
                            "timeout": 5,
                        }
                    ]
                }
            ]
        },
    }


def false_json() -> bool:
    return False


def render_agents_content(setup: dict[str, Any], profile: dict[str, Any]) -> str:
    return (
        "# NDDev Junie CLI Setup\n"
        "\n"
        f"This target is managed as the `{setup['id']}` setup with the "
        f"`{profile['id']}` permission profile by nddev-junie-cli-app.\n"
        "Use the managed nddev-builder skill for Junie setup artifact changes. Work only\n"
        "through the target-owned files passed by the manager's explicit Junie CLI flags\n"
        "and environment variables. Do not read or write live account Junie state.\n"
        "\n"
    )


def builder_source(relative: str) -> bytes:
    path = BUILDER_ROOT / relative
    return read_path_bounded(path, max_bytes=MANAGED_MAX_BYTES, label=f"builder source {relative}")


def render_extension_manifest() -> bytes:
    return canonical_json(
        {
            "name": "nddev-builder",
            "owner": {"name": "NDDev"},
            "metadata": {
                "description": "Local NDDev builder toolkit for Junie CLI setup artifacts.",
                "version": VERSION,
                "homepage": "https://github.com/NDDev-it-com/nddev-junie-cli-app",
            },
            "extensions": [
                {
                    "name": "nddev-builder",
                    "source": "./extensions/nddev-builder",
                    "description": "Creator and checker toolkit for native Junie CLI setup artifacts.",
                    "version": VERSION,
                    "author": {"name": "NDDev"},
                    "category": "tooling",
                    "keywords": ["junie", "setup", "skills", "mcp", "hooks"],
                }
            ],
        }
    )


def render_extension_json() -> bytes:
    return canonical_json(
        {
            "name": "nddev-builder",
            "description": "Creator and checker toolkit for native Junie CLI setup artifacts.",
        }
    )


def extension_prefix() -> str:
    return "extensions/nddev-builder-marketplace"


def copy_builder_projection(files: dict[str, bytes], source_root: str, target_root: str) -> None:
    for relative, data in read_builder_tree(source_root).items():
        files[f"{target_root}/{relative}"] = data


def desired_files(target: Path, setup: dict[str, Any], profile: dict[str, Any]) -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "config.json": canonical_json(render_config(target, setup, profile)),
        RUNTIME_ALLOWLIST_RELATIVE: canonical_json(dict(profile["allowlist"])),
        "AGENTS.md": render_agents_content(setup, profile).encode("utf-8"),
        "mcp/mcp.json": canonical_json({"mcpServers": {}}),
    }
    copy_builder_projection(files, "skills", "skills")
    copy_builder_projection(files, "agents", "agents")
    copy_builder_projection(files, "commands", "commands")
    copy_builder_projection(files, "hooks", "hooks")

    package = extension_prefix()
    files[f"{package}/.junie-extension/marketplace.json"] = render_extension_manifest()
    files[f"{package}/extensions/nddev-builder/extension.json"] = render_extension_json()
    files[f"{package}/extensions/nddev-builder/guidelines/AGENTS.md"] = files["AGENTS.md"]
    files[f"{package}/extensions/nddev-builder/mcp/.mcp.json"] = canonical_json(
        {"mcpServers": {}}
    )
    copy_builder_projection(
        files, "skills", f"{package}/extensions/nddev-builder/skills"
    )
    copy_builder_projection(
        files, "agents", f"{package}/extensions/nddev-builder/agents"
    )
    copy_builder_projection(
        files, "commands", f"{package}/extensions/nddev-builder/commands"
    )
    return files


def managed_digest_for_bytes(relative: str, data: bytes, *, legacy: bool = False) -> str:
    if legacy and relative in MERGED_MARKER_PATHS:
        block = extract_managed_block(data.decode("utf-8"))
        if block is None:
            return ""
        return sha256_bytes(block.encode("utf-8"))
    if legacy and relative in LEGACY_MANAGED_JSON_KEYS:
        value = parse_optional_json(data, relative)
        subset = {
            key: value.get(key) for key in LEGACY_MANAGED_JSON_KEYS[relative] if key in value
        }
        return sha256_bytes(canonical_json(subset))
    return sha256_bytes(data)


def current_managed_digest(target: Path, relative: str, *, legacy: bool = False) -> str | None:
    data = read_existing_file(
        safe_target_path(target, relative), max_bytes=MANAGED_MAX_BYTES, label=relative
    )
    if data is None:
        return None
    digest = managed_digest_for_bytes(relative, data, legacy=legacy)
    return digest or None


def runtime_lstat(path: Path, target: Path, label: str, *, repairable: bool) -> os.stat_result:
    if target not in path.parents and path != target:
        runtime_fail(f"{label} escaped managed target", code="escaped_target", repairable=False)
    try:
        info = path.lstat()
    except FileNotFoundError:
        runtime_fail(
            f"{label} is missing", code=f"{label_slug(label)}_missing", repairable=repairable
        )
    try:
        require_current_owner(info, label)
    except JunieCliSetupError as exc:
        runtime_fail(str(exc), code=f"{label_slug(label)}_owner", repairable=False)
    return info


def label_slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def runtime_directory(path: Path, target: Path, label: str, *, repairable: bool) -> os.stat_result:
    info = runtime_lstat(path, target, label, repairable=repairable)
    if stat.S_ISLNK(info.st_mode):
        runtime_fail(
            f"{label} must not be a symlink", code=f"{label_slug(label)}_symlink", repairable=False
        )
    if not stat.S_ISDIR(info.st_mode):
        runtime_fail(
            f"{label} must be a directory", code=f"{label_slug(label)}_type", repairable=False
        )
    return info


def runtime_private_directory(
    path: Path, target: Path, label: str, *, repairable: bool
) -> os.stat_result:
    info = runtime_directory(path, target, label, repairable=repairable)
    if stat.S_IMODE(info.st_mode) & 0o077:
        runtime_fail(f"{label} must be private", code=f"{label_slug(label)}_mode", repairable=False)
    return info


def runtime_regular_file(
    path: Path, target: Path, label: str, *, repairable: bool
) -> os.stat_result:
    info = runtime_lstat(path, target, label, repairable=repairable)
    if stat.S_ISLNK(info.st_mode):
        runtime_fail(
            f"{label} must not be a symlink", code=f"{label_slug(label)}_symlink", repairable=False
        )
    if not stat.S_ISREG(info.st_mode):
        runtime_fail(
            f"{label} must be a regular file", code=f"{label_slug(label)}_type", repairable=False
        )
    if info.st_nlink != 1:
        runtime_fail(
            f"{label} must not be a hardlink",
            code=f"{label_slug(label)}_hardlink",
            repairable=False,
        )
    return info


def require_runtime_parent_chain(path: Path, target: Path, label: str) -> None:
    if target not in path.parents:
        runtime_fail(f"{label} escaped managed target", code="escaped_target", repairable=False)
    current = target
    try:
        relative_parent = path.relative_to(target).parent
    except ValueError:
        runtime_fail(f"{label} escaped managed target", code="escaped_target", repairable=False)
    for part in relative_parent.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            runtime_fail(
                f"{label} parent is missing",
                code=f"{label_slug(label)}_parent_missing",
                repairable=True,
            )
        try:
            require_current_owner(info, f"{label} parent")
        except JunieCliSetupError as exc:
            runtime_fail(str(exc), code=f"{label_slug(label)}_parent_owner", repairable=False)
        if stat.S_ISLNK(info.st_mode):
            runtime_fail(
                f"{label} parent must not be a symlink",
                code=f"{label_slug(label)}_parent_symlink",
                repairable=False,
            )
        if not stat.S_ISDIR(info.st_mode):
            runtime_fail(
                f"{label} parent must be a directory",
                code=f"{label_slug(label)}_parent_type",
                repairable=False,
            )


def capture_launch_file_snapshot(
    target: Path,
    path: Path,
    label: str,
    *,
    max_bytes: int,
) -> LaunchFileSnapshot:
    require_runtime_parent_chain(path, target, label)
    info = runtime_regular_file(path, target, label, repairable=False)
    if not os.access(path, os.X_OK):
        runtime_fail(
            f"{label} is not executable",
            code=f"{label_slug(label)}_mode",
            repairable=False,
        )
    return LaunchFileSnapshot(
        label=label,
        relative_path=relative_to_target(target, path),
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        sha256=sha256_file_bounded(path, max_bytes=max_bytes, label=label),
        max_bytes=max_bytes,
    )


def revalidate_launch_file_snapshot(target: Path, snapshot: LaunchFileSnapshot) -> None:
    path = safe_target_path(target, snapshot.relative_path)
    require_runtime_parent_chain(path, target, snapshot.label)
    info = runtime_regular_file(path, target, snapshot.label, repairable=False)
    if not os.access(path, os.X_OK):
        runtime_fail(
            f"{snapshot.label} is not executable",
            code=f"{label_slug(snapshot.label)}_mode",
            repairable=False,
        )
    if (
        info.st_dev != snapshot.device
        or info.st_ino != snapshot.inode
        or info.st_size != snapshot.size
    ):
        runtime_fail(
            f"{snapshot.label} changed after launch preflight",
            code=f"{label_slug(snapshot.label)}_identity_changed",
            repairable=False,
        )
    current_sha256 = sha256_file_bounded(
        path,
        max_bytes=snapshot.max_bytes,
        label=snapshot.label,
    )
    if current_sha256 != snapshot.sha256:
        runtime_fail(
            f"{snapshot.label} digest changed after launch preflight",
            code=f"{label_slug(snapshot.label)}_digest_changed",
            repairable=False,
        )


def revalidate_launch_artifacts(plan: LaunchPlan) -> None:
    revalidate_launch_file_snapshot(plan.cwd, plan.shim)
    revalidate_launch_file_snapshot(plan.cwd, plan.binary)


def resolve_junie_binary(version_dir: Path, target: Path) -> Path:
    candidates = (
        version_dir / "Applications" / "junie.app" / "Contents" / "MacOS" / "junie",
        version_dir / "junie" / "bin" / "junie",
        version_dir / "junie",
    )
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        try:
            require_current_owner(info, "Junie binary")
        except JunieCliSetupError as exc:
            runtime_fail(str(exc), code="junie_binary_owner", repairable=False)
        if stat.S_ISLNK(info.st_mode):
            runtime_fail(
                "Junie binary must not be a symlink", code="junie_binary_symlink", repairable=False
            )
        if stat.S_ISREG(info.st_mode):
            return candidate
        runtime_fail(
            "Junie binary must be a regular file", code="junie_binary_type", repairable=False
        )
    runtime_fail(
        "Junie binary is missing from the installed version",
        code="junie_binary_missing",
        repairable=True,
    )


def ensure_current_symlink(target: Path, version: str) -> None:
    data = runtime_data(target)
    current = data / "current"
    desired = data / "versions" / version
    info = None
    with contextlib.suppress(FileNotFoundError):
        info = current.lstat()
    if info is not None:
        require_current_owner(info, "Junie current link")
        if stat.S_ISDIR(info.st_mode):
            safe_rmtree_private_directory(current, "Junie current directory")
        else:
            current.unlink()
    current.symlink_to(desired)


def read_runtime_receipt(target: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    path = runtime_receipt_path(target)
    runtime_regular_file(path, target, "runtime receipt", repairable=True)
    data = read_existing_file(path, max_bytes=METADATA_MAX_BYTES, label="runtime receipt")
    if data is None:
        runtime_fail("runtime receipt is missing", code="runtime_receipt_missing", repairable=True)
    try:
        receipt = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        runtime_fail(
            f"runtime receipt is invalid JSON: {exc}",
            code="runtime_receipt_json",
            repairable=True,
        )
    if not isinstance(receipt, dict):
        runtime_fail(
            "runtime receipt must be a JSON object", code="runtime_receipt_type", repairable=True
        )
    expected_common = {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "version": baseline["release"]["exact_version"],
        "installer": baseline["release"]["installer"],
        "update_info": baseline["release"]["update_info"],
    }
    for key, expected in expected_common.items():
        if receipt.get(key) != expected:
            runtime_fail(
                f"runtime receipt {key} does not match the baseline",
                code=f"runtime_receipt_{key}",
                repairable=True,
            )
    artifact = receipt.get("artifact")
    if not isinstance(artifact, dict):
        runtime_fail(
            "runtime receipt artifact is invalid", code="runtime_receipt_artifact", repairable=True
        )
    expected_artifact = dict(baseline["release"]["exact_artifacts"][current_platform_id()])
    expected_artifact["platform"] = current_platform_id()
    if artifact != expected_artifact:
        runtime_fail(
            "runtime receipt artifact does not match the baseline",
            code="runtime_receipt_artifact",
            repairable=True,
        )
    verification = receipt.get("artifact_verification")
    if not isinstance(verification, dict) or verification.get("size_verified") is not True:
        runtime_fail(
            "runtime receipt artifact verification is invalid",
            code="runtime_receipt_artifact_verification",
            repairable=True,
        )
    return receipt


def current_software_metadata(target: Path) -> dict[str, Any]:
    baseline = load_baseline()
    version = baseline["release"]["exact_version"]
    platform_id = current_platform_id()
    home = runtime_home(target)
    data = runtime_data(target)
    bin_path = runtime_bin(target)
    runtime_private_directory(runtime_root(target), target, "runtime root", repairable=False)
    runtime_private_directory(home, target, "runtime home", repairable=True)
    shim_info = runtime_regular_file(bin_path, target, "Junie shim", repairable=True)
    if not os.access(bin_path, os.X_OK):
        runtime_fail("Junie shim is not executable", code="junie_shim_mode", repairable=True)
    versions = data / "versions"
    version_dir = versions / version
    runtime_directory(data, target, "Junie data", repairable=True)
    runtime_directory(versions, target, "Junie versions", repairable=True)
    runtime_directory(version_dir, target, "Junie pinned version", repairable=True)
    current_link = data / "current"
    try:
        link_info = current_link.lstat()
    except FileNotFoundError:
        runtime_fail(
            "Junie current link is missing", code="junie_current_link_missing", repairable=True
        )
    try:
        require_current_owner(link_info, "Junie current link")
    except JunieCliSetupError as exc:
        runtime_fail(str(exc), code="junie_current_link_owner", repairable=False)
    if not stat.S_ISLNK(link_info.st_mode):
        runtime_fail(
            "Junie current link must be a symlink", code="junie_current_link_type", repairable=False
        )
    try:
        current_resolved = current_link.resolve(strict=True)
        version_resolved = version_dir.resolve(strict=True)
    except FileNotFoundError:
        runtime_fail(
            "Junie current link is dangling",
            code="junie_current_link_dangling",
            repairable=True,
        )
    if current_resolved != version_resolved:
        runtime_fail(
            "Junie current link does not point at the pinned version",
            code="junie_current_link_target",
            repairable=True,
        )
    receipt = read_runtime_receipt(target, baseline)
    artifact = receipt["artifact"]
    binary = resolve_junie_binary(version_dir, target)
    binary_info = runtime_regular_file(binary, target, "Junie binary", repairable=True)
    if not os.access(binary, os.X_OK):
        runtime_fail("Junie binary is not executable", code="junie_binary_mode", repairable=True)
    installer = baseline["release"]["installer"]
    return {
        "version": version,
        "platform": platform_id,
        "installer": {
            "url": installer["url"],
            "sha256": installer["sha256"],
        },
        "update_info": baseline["release"]["update_info"],
        "artifact": {
            "download_url": artifact["download_url"],
            "sha256": artifact["sha256"],
            "size": artifact["size"],
            "platform": artifact["platform"],
        },
        "artifact_verification": receipt["artifact_verification"],
        "receipt_sha256": sha256_file_bounded(
            runtime_receipt_path(target), max_bytes=METADATA_MAX_BYTES, label="runtime receipt"
        ),
        "home": relative_to_target(target, home),
        "shim": {
            "path": relative_to_target(target, bin_path),
            "mode": f"{stat.S_IMODE(shim_info.st_mode):04o}",
            "sha256": sha256_file_bounded(
                bin_path, max_bytes=MANAGED_MAX_BYTES, label="Junie shim"
            ),
        },
        "binary": {
            "path": relative_to_target(target, binary),
            "mode": f"{stat.S_IMODE(binary_info.st_mode):04o}",
            "size": binary_info.st_size,
            "sha256": sha256_file_bounded(
                binary, max_bytes=SOFTWARE_FILE_MAX_BYTES, label="Junie binary"
            ),
        },
        "current_link": {
            "path": relative_to_target(target, current_link),
            "target": relative_to_target(target, version_dir),
        },
    }


def software_state(target: Path) -> dict[str, Any]:
    try:
        runtime_root(target).lstat()
    except FileNotFoundError:
        return {"state": "absent"}
    try:
        metadata = current_software_metadata(target)
    except RuntimeValidationError as exc:
        return {
            "state": "partial",
            "error": str(exc),
            "code": exc.code,
            "repairable": exc.repairable,
        }
    except JunieCliSetupError as exc:
        return {
            "state": "partial",
            "error": str(exc),
            "code": "runtime_invalid",
            "repairable": False,
        }
    return {
        "state": "installed",
        "version": metadata["version"],
        "platform": metadata["platform"],
        "binary": metadata["binary"]["path"],
    }


def sanitized_subprocess_env(home: Path, data: Path, tmp: Path) -> dict[str, str]:
    java_options = java_isolation_options(home, tmp)
    env: dict[str, str] = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "JUNIE_DATA": str(data),
        "JUNIE_LOG_DIR": str(data / "logs"),
        "JUNIE_SKIP_UPDATE_CHECK": "1",
        "TMPDIR": str(tmp),
        "TMP": str(tmp),
        "TEMP": str(tmp),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "JDK_JAVA_OPTIONS": java_options,
        "JAVA_TOOL_OPTIONS": java_options,
        "PATH": SAFE_SUBPROCESS_PATH,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("TERM", "COLORTERM", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    for name in PROVIDER_SECRET_NAMES:
        env.pop(name, None)
    return env


def write_stage_installer(stage: Path, installer_bytes: bytes) -> Path:
    installer = stage / "install.sh"
    fd = os.open(installer, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OWNER_FILE_MODE)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), OWNER_EXECUTABLE_MODE)
        handle.write(installer_bytes)
    return installer


def run_stage_version_probe(stage_home: Path, version: str, timeout: int) -> None:
    stage_root = stage_home.parent
    require_private_directory(stage_root, "stage root")
    require_private_directory(stage_home, "stage home")
    tmp = stage_home.parent / "probe-tmp"
    ensure_private_directory(tmp, stage_root, "stage probe tmp")
    cache = stage_home.parent / "probe-cache"
    skills = stage_home / "skills"
    agents = stage_home / "agents"
    commands = stage_home / "commands"
    mcp = stage_home / "mcp"
    extensions = stage_home / "extensions-cache"
    for directory in (cache, skills, agents, commands, mcp, extensions):
        ensure_private_directory(directory, stage_root, f"stage probe directory {directory.name}")
    config = stage_home / "config.json"
    if not lstat_exists(config):
        atomic_write(config, b"{}\n", stage_home)
    guidelines = stage_home / "AGENTS.md"
    if not lstat_exists(guidelines):
        atomic_write(guidelines, b"# Junie Stage Probe\n", stage_home)
    env = sanitized_subprocess_env(stage_home, stage_home / ".local" / "share" / "junie", tmp)
    live_before = snapshot_live_junie_home()
    try:
        completed = subprocess.run(
            [
                str(stage_home / ".local" / "bin" / "junie"),
                "--skip-update-check",
                "--project",
                str(stage_home),
                "--config-location",
                str(config),
                "--config-default-locations",
                "false",
                "--guidelines-filename",
                str(stage_home / "AGENTS.md"),
                "--mcp-location",
                str(mcp),
                "--mcp-default-locations",
                "false",
                "--skill-location",
                str(skills),
                "--skill-default-locations",
                "false",
                "--agent-location",
                str(agents),
                "--agent-default-location",
                "false",
                "--command-location",
                str(commands),
                "--command-default-locations",
                "false",
                "--model-default-locations",
                "false",
                "--extensions-default-location",
                str(extensions),
                "--cache-dir",
                str(cache),
                "--version",
            ],
            cwd=stage_home,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        fail(f"stage Junie version probe command is missing: {exc}")
    except subprocess.TimeoutExpired:
        fail("stage Junie version probe timed out")
    require_live_junie_home_unchanged(live_before, context="stage Junie version probe")
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        fail(f"stage Junie version probe failed: {output.strip()}")
    if version not in output:
        fail("stage Junie version probe did not report the pinned version")


def build_runtime_receipt(
    baseline: dict[str, Any],
    artifact: dict[str, Any],
    artifact_verification: dict[str, Any],
    installer_output: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "version": baseline["release"]["exact_version"],
        "platform": artifact["platform"],
        "installer": baseline["release"]["installer"],
        "update_info": baseline["release"]["update_info"],
        "artifact": artifact,
        "artifact_verification": artifact_verification,
        "installer_output_sha256": sha256_bytes(installer_output.encode("utf-8")),
    }


def install_software_to_stage(stage: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    installer_url, expected_installer_sha256 = installer_source(baseline)
    update_info_url = update_info_source(baseline)
    installer_bytes = read_url_bounded(
        installer_url, max_bytes=INSTALLER_MAX_BYTES, label="Junie installer"
    )
    actual_installer_sha256 = sha256_bytes(installer_bytes)
    if actual_installer_sha256 != expected_installer_sha256:
        fail("Junie installer SHA256 does not match the pinned baseline")
    artifact = artifact_metadata_from_update_info(baseline, update_info_url)
    artifact_verification = verify_artifact_binding(artifact)
    stage_home = stage / "home"
    stage_tmp = stage / "tmp"
    stage_home.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True)
    stage_tmp.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True)
    installer_path = write_stage_installer(stage, installer_bytes)
    env = sanitized_subprocess_env(stage_home, stage_home / ".local" / "share" / "junie", stage_tmp)
    env["JUNIE_VERSION"] = baseline["release"]["exact_version"]
    env["NDDEV_JUNIE_EXPECTED_ARTIFACT_SHA256"] = artifact["sha256"]
    env["NDDEV_JUNIE_EXPECTED_ARTIFACT_SIZE"] = str(artifact["size"])
    live_before = snapshot_live_junie_home()
    try:
        completed = subprocess.run(
            [str(trusted_bash()), str(installer_path)],
            cwd=stage,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        fail(f"Junie installer shell is missing: {exc}")
    except subprocess.TimeoutExpired:
        fail("Junie installer timed out in isolated staging HOME")
    require_live_junie_home_unchanged(live_before, context="Junie installer")
    installer_output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        fail(f"Junie installer failed in isolated staging HOME: {installer_output.strip()}")
    if f"Using specified version: {baseline['release']['exact_version']}" not in installer_output:
        fail("Junie installer output did not confirm the pinned version")
    if "Found published checksum for version" not in installer_output:
        fail("Junie installer output did not confirm update-info checksum binding")
    if "Checksum verified" not in installer_output:
        fail("Junie installer output did not confirm artifact checksum verification")
    run_stage_version_probe(stage_home, baseline["release"]["exact_version"], PROBE_TIMEOUT_SECONDS)
    return {
        "installer_url": installer_url,
        "installer_sha256": expected_installer_sha256,
        "update_info_url": update_info_url,
        "artifact": artifact,
        "receipt": build_runtime_receipt(
            baseline, artifact, artifact_verification, installer_output
        ),
    }


def begin_software_transaction(target: Path, *, repair: bool) -> SoftwareTransaction:
    current = software_state(target)
    if current["state"] == "installed":
        return SoftwareTransaction(metadata=current_software_metadata(target), changed=False)
    if current["state"] == "partial":
        if not current.get("repairable"):
            fail(str(current.get("error", "Junie software runtime is unsafe")))
        if not repair:
            fail("Junie software runtime is partial; run update to repair it")
    baseline = load_baseline()
    root = runtime_root(target)
    require_runtime_directory(target, target, "target")
    root_info = stat_existing(root, "runtime root")
    if root_info is not None:
        runtime_private_directory(root, target, "runtime root", repairable=False)
    home_info = stat_existing(runtime_home(target), "runtime home")
    if home_info is not None:
        runtime_private_directory(runtime_home(target), target, "runtime home", repairable=False)
    if root_info is None:
        root.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True)
        runtime_private_directory(root, target, "runtime root", repairable=False)
    stage = root / f".install-stage.{os.getpid()}.{time.time_ns()}"
    old_home = root / f".home-old.{os.getpid()}.{time.time_ns()}"
    stage.mkdir(mode=OWNER_DIRECTORY_MODE)
    previous_home: Path | None = None
    try:
        install_result = install_software_to_stage(stage, baseline)
        staged_home = stage / "home"
        if lstat_exists(runtime_home(target)):
            runtime_private_directory(
                runtime_home(target), target, "runtime home", repairable=False
            )
            runtime_home(target).rename(old_home)
            previous_home = old_home
        staged_home.rename(runtime_home(target))
        ensure_current_symlink(target, baseline["release"]["exact_version"])
        ensure_private_directory(runtime_root(target), target, "runtime root")
        ensure_private_directory(runtime_home(target), target, "runtime home")
        ensure_private_directory(runtime_cache(target), target, "runtime cache")
        ensure_private_directory(runtime_tmp(target), target, "runtime tmp")
        atomic_write(
            runtime_receipt_path(target),
            canonical_json(install_result["receipt"]),
            target,
        )
        metadata = current_software_metadata(target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            if lstat_exists(runtime_home(target)) and previous_home is not None:
                safe_rmtree_private_directory(runtime_home(target), "runtime home")
        if previous_home is not None and lstat_exists(previous_home):
            previous_home.rename(runtime_home(target))
        safe_rmtree_private_directory_if_exists(stage, "software install stage")
        prune_empty_runtime_dirs(target)
        raise
    safe_rmtree_private_directory_if_exists(stage, "software install stage")
    return SoftwareTransaction(
        metadata=metadata,
        changed=True,
        previous_home=previous_home,
        new_home=runtime_home(target),
    )


def commit_software_transaction(transaction: SoftwareTransaction) -> None:
    if transaction.previous_home is not None and lstat_exists(transaction.previous_home):
        safe_rmtree_private_directory(transaction.previous_home, "previous runtime home")


def rollback_software_transaction(target: Path, transaction: SoftwareTransaction) -> None:
    if not transaction.changed:
        return
    if transaction.new_home is not None and lstat_exists(transaction.new_home):
        safe_rmtree_private_directory(transaction.new_home, "new runtime home")
    if transaction.previous_home is not None and lstat_exists(transaction.previous_home):
        transaction.previous_home.rename(runtime_home(target))
    prune_empty_runtime_dirs(target)


def begin_runtime_remove(target: Path) -> RuntimeRemoveTransaction:
    root = runtime_root(target)
    if not lstat_exists(root):
        return RuntimeRemoveTransaction(changed=False)
    runtime_private_directory(root, target, "runtime root", repairable=False)
    removed = target / f".{RUNTIME_DIR_NAME}.removed.{os.getpid()}.{time.time_ns()}"
    root.rename(removed)
    return RuntimeRemoveTransaction(changed=True, removed_root=removed)


def commit_runtime_remove(transaction: RuntimeRemoveTransaction) -> None:
    if transaction.removed_root is not None:
        safe_rmtree_private_directory(transaction.removed_root, "removed runtime root")


def rollback_runtime_remove(target: Path, transaction: RuntimeRemoveTransaction) -> None:
    if transaction.removed_root is not None and lstat_exists(transaction.removed_root):
        if lstat_exists(runtime_root(target)):
            info = runtime_root(target).lstat()
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                safe_rmtree_private_directory(runtime_root(target), "runtime root")
            else:
                runtime_root(target).unlink()
        transaction.removed_root.rename(runtime_root(target))


def prune_empty_runtime_dirs(target: Path) -> None:
    for directory in (
        runtime_tmp(target),
        runtime_cache(target),
        runtime_home(target),
        runtime_root(target),
    ):
        with contextlib.suppress(OSError):
            directory.rmdir()


def stamp_path(target: Path) -> Path:
    return target / STAMP_NAME


def read_stamp(target: Path) -> dict[str, Any] | None:
    path = stamp_path(target)
    if not lstat_exists(path):
        return None
    stamp = read_json_file(path, max_bytes=METADATA_MAX_BYTES, label=STAMP_NAME)
    if stamp.get("product_name") != PRODUCT_NAME:
        fail("stamp belongs to another product")
    schema = stamp.get("schema_version")
    if schema not in (*LEGACY_STAMP_SCHEMAS, STAMP_SCHEMA):
        fail("stamp schema is unsupported")
    setup_id = stamp.get("setup_id")
    if schema == STAMP_SCHEMA:
        if setup_id not in SETUP_ORDER:
            fail("stamp setup_id is unsupported")
        if stamp.get("profile_id") not in PROFILE_ORDER:
            fail("stamp profile_id is unsupported")
    elif setup_id not in LEGACY_SETUP_IDS:
        fail("legacy stamp setup_id is unsupported")
    canonical = str(validate_target(target, create=False))
    if stamp.get("canonical_target") != canonical:
        fail("stamp is bound to a different canonical target")
    return stamp


def is_legacy_stamp(stamp: dict[str, Any]) -> bool:
    return stamp.get("schema_version") != STAMP_SCHEMA


def stamp_profile_id(stamp: dict[str, Any]) -> str | None:
    if not is_legacy_stamp(stamp):
        profile_id = stamp.get("profile_id")
        return profile_id if isinstance(profile_id, str) else None
    setup_id = stamp.get("setup_id")
    if setup_id in PROFILE_ORDER:
        return str(setup_id)
    return None


def stamp_setup_id(stamp: dict[str, Any]) -> str:
    if is_legacy_stamp(stamp):
        return DEFAULT_SETUP_ID
    return str(stamp["setup_id"])


def stamp_managed_relatives(stamp: dict[str, Any]) -> list[str]:
    managed = stamp.get("managed_files")
    if not isinstance(managed, dict):
        fail("stamp managed_files is invalid")
    relatives: list[str] = []
    for relative in managed:
        if not isinstance(relative, str):
            fail("stamp managed file path is invalid")
        safe_target_path(Path("/tmp/nddev-junie-cli-path-check"), relative)
        relatives.append(relative)
    return sorted(relatives)


def drift_for_stamp(target: Path, stamp: dict[str, Any]) -> list[str]:
    drift: list[str] = []
    managed = stamp.get("managed_files")
    if not isinstance(managed, dict):
        fail("stamp managed_files is invalid")
    legacy = stamp.get("schema_version") == LEGACY_STAMP_SCHEMA
    for relative, expected in managed.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            fail("stamp managed file digest is invalid")
        current = current_managed_digest(target, relative, legacy=legacy)
        if current != expected:
            drift.append(relative)
    expected_software = stamp.get("software")
    if not isinstance(expected_software, dict):
        drift.append("software")
    else:
        try:
            current_software = current_software_metadata(target)
        except JunieCliSetupError:
            drift.append("software")
        else:
            if current_software != expected_software:
                drift.append("software")
    return drift


def status_payload(target: Path) -> dict[str, Any]:
    canonical = validate_target(target, create=False)
    if not lstat_exists(target):
        return {
            "state": "absent",
            "managed": False,
            "canonical_target": str(canonical),
            "setup_id": None,
            "profile_id": None,
            "legacy_setup_id": None,
            "drift": [],
            "software": {"state": "absent"},
        }
    require_real_directory(target, "target")
    stamp = read_stamp(target)
    if stamp is None:
        return {
            "state": "unmanaged",
            "managed": False,
            "canonical_target": str(canonical),
            "setup_id": None,
            "profile_id": None,
            "legacy_setup_id": None,
            "drift": [],
            "software": software_state(target),
        }
    drift = drift_for_stamp(target, stamp)
    software = software_state(target)
    legacy = is_legacy_stamp(stamp)
    return {
        "state": "managed",
        "managed": True,
        "canonical_target": str(canonical),
        "setup_id": stamp_setup_id(stamp),
        "profile_id": stamp_profile_id(stamp),
        "legacy_setup_id": stamp["setup_id"] if legacy else None,
        "build_version": stamp["build_version"],
        "stamp_schema": stamp["schema_version"],
        "legacy": legacy,
        "launchable": stamp["schema_version"] == STAMP_SCHEMA,
        "drift": drift,
        "managed_files": sorted(stamp["managed_files"]),
        "software": software,
    }


def unique_relatives(relatives: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    return sorted(set(relatives))


def snapshot_files(target: Path, relatives: list[str]) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for relative in unique_relatives([*relatives, STAMP_NAME]):
        snapshot[relative] = read_existing_file(
            safe_target_path(target, relative), max_bytes=MANAGED_MAX_BYTES, label=relative
        )
    return snapshot


def capture_transaction_snapshot(
    target: Path, *, target_existed: bool, relatives: list[str]
) -> TransactionSnapshot:
    files: dict[str, FileSnapshot] = {}
    target_mode = None
    if target_existed:
        target_mode = stat.S_IMODE(require_real_directory(target, "target").st_mode)
    for relative in unique_relatives([*relatives, STAMP_NAME]):
        path = safe_target_path(target, relative)
        info = require_existing_managed_file(path, relative, max_bytes=MANAGED_MAX_BYTES)
        if info is None:
            files[relative] = FileSnapshot(exists=False)
            continue
        data = read_existing_file(path, max_bytes=MANAGED_MAX_BYTES, label=relative)
        files[relative] = FileSnapshot(
            exists=True,
            data=data,
            mode=stat.S_IMODE(info.st_mode),
        )
    return TransactionSnapshot(
        target_existed=target_existed,
        target_mode=target_mode,
        files=files,
    )


def restore_transaction_snapshot(target: Path, snapshot: TransactionSnapshot) -> None:
    for relative, item in snapshot.files.items():
        path = safe_target_path(target, relative)
        if not item.exists:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        if item.data is None or item.mode is None:
            fail("transaction snapshot is invalid")
        atomic_write(path, item.data, target, mode=item.mode)
    prune_empty_managed_dirs(target, list(snapshot.files))
    if snapshot.target_existed and snapshot.target_mode is not None:
        with contextlib.suppress(FileNotFoundError):
            chmod_private_directory(target, snapshot.target_mode, "target")
    if not snapshot.target_existed:
        with contextlib.suppress(OSError):
            target.rmdir()


def restore_snapshot(target: Path, snapshot: dict[str, bytes | None]) -> None:
    for relative, data in snapshot.items():
        path = safe_target_path(target, relative)
        if data is None:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        atomic_write(path, data, target)
    prune_empty_managed_dirs(target, list(snapshot))


def choose_backup_slot(pool: Path) -> int:
    info = stat_existing(pool, "backup pool")
    if info is None:
        try:
            pool.mkdir(mode=OWNER_DIRECTORY_MODE)
        except FileExistsError:
            fail("backup pool appeared during creation")
        require_private_directory(pool, "backup pool")
    else:
        require_current_owner(info, "backup pool")
        if not stat.S_ISDIR(info.st_mode):
            fail("backup pool must be a directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            fail("backup pool must be private")
    for slot in range(10):
        slot_path = pool / str(slot)
        if not lstat_exists(slot_path):
            return slot
        slot_info = stat_existing(slot_path, f"backup slot {slot}")
        if slot_info is None:
            return slot
        require_current_owner(slot_info, f"backup slot {slot}")
        if not stat.S_ISDIR(slot_info.st_mode):
            fail(f"backup slot {slot} must be a directory")
        if stat.S_IMODE(slot_info.st_mode) & 0o077:
            fail(f"backup slot {slot} must be private")
    return min(range(10), key=lambda item: (pool / str(item)).lstat().st_mtime_ns)


def create_backup(target: Path, stamp: dict[str, Any]) -> int:
    pool = backup_pool(target)
    slot = choose_backup_slot(pool)
    slot_dir = pool / str(slot)
    if lstat_exists(slot_dir):
        safe_rmtree_private_directory(slot_dir, f"backup slot {slot}")
    try:
        slot_dir.mkdir(mode=OWNER_DIRECTORY_MODE)
    except FileExistsError:
        fail(f"backup slot {slot} appeared during creation")
    require_private_directory(slot_dir, f"backup slot {slot}")
    files: dict[str, Any] = {}
    for relative in unique_relatives([*stamp_managed_relatives(stamp), STAMP_NAME]):
        data = read_existing_file(
            safe_target_path(target, relative), max_bytes=MANAGED_MAX_BYTES, label=relative
        )
        files[relative] = None if data is None else base64.b64encode(data).decode("ascii")
    envelope = {
        "schema_version": 2,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "slot": slot,
        "canonical_target": str(validate_target(target, create=False)),
        "source_setup_id": stamp["setup_id"],
        "source_profile_id": stamp_profile_id(stamp),
        "source_legacy_setup_id": stamp["setup_id"] if is_legacy_stamp(stamp) else None,
        "created_at": int(time.time()),
        "files": files,
    }
    atomic_write(slot_dir / BACKUP_NAME, canonical_json(envelope), slot_dir)
    return slot


def require_backup_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        fail(f"{label} is invalid")
    return value


def require_backup_int(value: Any, label: str) -> int:
    if type(value) is not int:
        fail(f"{label} is invalid")
    return value


def require_backup_optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        fail(f"{label} is invalid")
    return value


def parse_backup_stamp(target: Path, data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"backup stamp is invalid JSON: {exc}")
    if not isinstance(value, dict):
        fail("backup stamp must contain a JSON object")
    if value.get("product_name") != PRODUCT_NAME:
        fail("backup stamp belongs to another product")
    schema = value.get("schema_version")
    if schema not in (*LEGACY_STAMP_SCHEMAS, STAMP_SCHEMA):
        fail("backup stamp schema is unsupported")
    if not isinstance(value.get("build_version"), str) or not value.get("build_version"):
        fail("backup stamp build_version is invalid")
    setup_id = value.get("setup_id")
    if not isinstance(setup_id, str):
        fail("backup stamp setup_id is invalid")
    if schema == STAMP_SCHEMA:
        if setup_id not in SETUP_ORDER:
            fail("backup stamp setup_id is unsupported")
        if value.get("profile_id") not in PROFILE_ORDER:
            fail("backup stamp profile_id is unsupported")
    elif setup_id not in LEGACY_SETUP_IDS:
        fail("legacy backup stamp setup_id is unsupported")
    if value.get("canonical_target") != str(validate_target(target, create=False)):
        fail("backup stamp is bound to a different canonical target")
    managed = value.get("managed_files")
    if not isinstance(managed, dict):
        fail("backup stamp managed_files is invalid")
    for relative, expected in managed.items():
        if not isinstance(relative, str) or not isinstance(expected, str) or not expected:
            fail("backup stamp managed file digest is invalid")
        safe_target_path(target, relative)
    if not isinstance(value.get("software"), dict):
        fail("backup stamp software is invalid")
    return value


def decode_backup_payload(relative: str, encoded: Any) -> bytes:
    if not isinstance(encoded, str):
        fail("backup file payload is invalid")
    try:
        data = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        fail("backup file payload is invalid")
    if len(data) > MANAGED_MAX_BYTES:
        fail("backup file payload is too large")
    return data


def validate_backup_envelope_scalars(
    target: Path,
    slot: int,
    envelope: dict[str, Any],
) -> None:
    if envelope.get("schema_version") != 2:
        fail("backup schema is unsupported")
    if envelope.get("product_name") != PRODUCT_NAME:
        fail("backup belongs to another product")
    require_backup_string(envelope.get("build_version"), "backup build_version")
    if require_backup_int(envelope.get("slot"), "backup slot") != slot:
        fail("backup slot does not match the requested slot")
    if envelope.get("canonical_target") != str(validate_target(target, create=False)):
        fail("backup is bound to a different canonical target")
    require_backup_string(envelope.get("source_setup_id"), "backup source_setup_id")
    require_backup_optional_string(
        envelope.get("source_profile_id"), "backup source_profile_id"
    )
    require_backup_optional_string(
        envelope.get("source_legacy_setup_id"), "backup source_legacy_setup_id"
    )
    require_backup_int(envelope.get("created_at"), "backup created_at")
    if not isinstance(envelope.get("files"), dict):
        fail("backup files are invalid")


def build_backup_restore_plan(target: Path, slot: int, envelope: dict[str, Any]) -> BackupRestorePlan:
    validate_backup_envelope_scalars(target, slot, envelope)
    files = envelope["files"]
    decoded: dict[str, bytes] = {}
    for relative, encoded in files.items():
        if not isinstance(relative, str):
            fail("backup file paths are invalid")
        safe_target_path(target, relative)
        decoded[relative] = decode_backup_payload(relative, encoded)
    stamp_payload = decoded.get(STAMP_NAME)
    if stamp_payload is None:
        fail("backup stamp payload is missing")
    stamp = parse_backup_stamp(target, stamp_payload)
    expected_relatives = unique_relatives([*stamp_managed_relatives(stamp), STAMP_NAME])
    if sorted(decoded) != expected_relatives:
        fail("backup file paths do not match the backup stamp")
    if envelope["source_setup_id"] != stamp["setup_id"]:
        fail("backup source_setup_id does not match the backup stamp")
    if envelope["source_profile_id"] != stamp_profile_id(stamp):
        fail("backup source_profile_id does not match the backup stamp")
    expected_legacy_setup_id = stamp["setup_id"] if is_legacy_stamp(stamp) else None
    if envelope["source_legacy_setup_id"] != expected_legacy_setup_id:
        fail("backup source_legacy_setup_id does not match the backup stamp")
    legacy = stamp.get("schema_version") == LEGACY_STAMP_SCHEMA
    for relative, expected in stamp["managed_files"].items():
        digest = managed_digest_for_bytes(relative, decoded[relative], legacy=legacy)
        if digest != expected:
            fail("backup payload digest does not match the backup stamp")
    try:
        current_software = current_software_metadata(target)
    except JunieCliSetupError as exc:
        fail(f"backup target software is not restorable: {exc}")
    if current_software != stamp["software"]:
        fail("backup software does not match current target runtime")
    return BackupRestorePlan(files=decoded, stamp=stamp, relatives=expected_relatives)


def validate_restored_backup_state(target: Path, expected_stamp: dict[str, Any]) -> dict[str, Any]:
    restored_stamp = read_stamp(target)
    if restored_stamp != expected_stamp:
        fail("restored stamp does not match the backup stamp")
    drift = drift_for_stamp(target, restored_stamp)
    if drift:
        fail(f"restored target has drift: {', '.join(drift)}")
    return restored_stamp


def validate_snapshot_restored(target: Path, snapshot: dict[str, bytes | None]) -> None:
    for relative, expected in snapshot.items():
        current = read_existing_file(
            safe_target_path(target, relative),
            max_bytes=MANAGED_MAX_BYTES,
            label=relative,
        )
        if current != expected:
            fail("restore rollback did not restore the previous target state")


def build_stamp(
    target: Path,
    setup_id: str,
    profile_id: str,
    files: dict[str, bytes],
    software: dict[str, Any],
) -> dict[str, Any]:
    managed = {
        relative: managed_digest_for_bytes(relative, data) for relative, data in files.items()
    }
    return {
        "schema_version": STAMP_SCHEMA,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "setup_id": setup_id,
        "profile_id": profile_id,
        "canonical_target": str(validate_target(target, create=False)),
        "managed_files": managed,
        "software": software,
    }


def assert_no_unmanaged_conflicts(target: Path, relatives: list[str]) -> None:
    for relative in relatives:
        path = safe_target_path(target, relative)
        if lstat_exists(path):
            fail(f"unmanaged file already exists at managed path: {relative}")


def validate_legacy_migration_clean(target: Path, stamp: dict[str, Any]) -> None:
    if stamp.get("schema_version") != LEGACY_STAMP_SCHEMA:
        return
    config = read_existing_file(
        target / "config.json", max_bytes=MANAGED_MAX_BYTES, label="legacy config.json"
    )
    if config is not None:
        value = parse_optional_json(config, "legacy config.json")
        extra = sorted(set(value) - set(LEGACY_MANAGED_JSON_KEYS["config.json"]))
        if extra:
            fail(
                "legacy config.json contains unmanaged keys; remove or back it up before migrate: "
                + ", ".join(extra)
            )
    allowlist = read_existing_file(
        target / "allowlist.json", max_bytes=MANAGED_MAX_BYTES, label="legacy allowlist.json"
    )
    if allowlist is not None:
        value = parse_optional_json(allowlist, "legacy allowlist.json")
        extra = sorted(set(value) - set(LEGACY_MANAGED_JSON_KEYS["allowlist.json"]))
        if extra:
            fail(
                "legacy allowlist.json contains unmanaged keys; remove or back it up before migrate: "
                + ", ".join(extra)
            )
    agents = read_existing_file(
        target / "AGENTS.md", max_bytes=MANAGED_MAX_BYTES, label="legacy AGENTS.md"
    )
    if agents is not None:
        text = agents.decode("utf-8")
        block = extract_managed_block(text)
        unmanaged = text.replace(block or "", "").strip()
        if unmanaged:
            fail("legacy AGENTS.md contains unmanaged text; remove or back it up before migrate")


def write_setup_locked(
    target: Path,
    setup: dict[str, Any],
    profile: dict[str, Any],
    *,
    require_existing: bool = False,
    repair_software: bool = False,
    allow_legacy_migration: bool = False,
    directory_transaction: DirectoryTransaction | None = None,
) -> dict[str, Any]:
    target = validate_target(target, create=True, transaction=directory_transaction)
    created_paths = [] if directory_transaction is None else directory_transaction.created
    target_existed = target not in created_paths
    current = read_stamp(target)
    if require_existing and current is None:
        fail("operation requires an already managed target")
    if current is not None:
        if current["schema_version"] != STAMP_SCHEMA and not allow_legacy_migration:
            fail("legacy managed target must be migrated before this operation")
        if allow_legacy_migration:
            validate_legacy_migration_clean(target, current)
        drift = drift_for_stamp(target, current)
        if drift and not (repair_software and drift == ["software"]):
            fail(f"managed target has drift: {', '.join(drift)}")
    files = desired_files(target, setup, profile)
    previous_relatives = [] if current is None else stamp_managed_relatives(current)
    transaction_relatives = unique_relatives([*previous_relatives, *files])
    if current is None:
        assert_no_unmanaged_conflicts(target, transaction_relatives)
    changed = [
        relative
        for relative, data in files.items()
        if current_managed_digest(target, relative) != managed_digest_for_bytes(relative, data)
    ]
    backup_slot = None
    if current is not None and (
        current["schema_version"] != STAMP_SCHEMA
        or current["setup_id"] != setup["id"]
        or current.get("profile_id") != profile["id"]
    ):
        backup_slot = create_backup(target, current)
    snapshot = capture_transaction_snapshot(
        target, target_existed=target_existed, relatives=transaction_relatives
    )
    software_tx: SoftwareTransaction | None = None
    try:
        software_tx = begin_software_transaction(target, repair=repair_software)
        for relative, data in files.items():
            atomic_write(safe_target_path(target, relative), data, target)
        for relative in previous_relatives:
            if relative not in files:
                with contextlib.suppress(FileNotFoundError):
                    safe_target_path(target, relative).unlink()
        prune_empty_managed_dirs(target, transaction_relatives)
        desired_stamp = build_stamp(target, setup["id"], profile["id"], files, software_tx.metadata)
        atomic_write(stamp_path(target), canonical_json(desired_stamp), target)
    except BaseException:
        if software_tx is not None:
            rollback_software_transaction(target, software_tx)
        else:
            prune_empty_runtime_dirs(target)
        restore_transaction_snapshot(target, snapshot)
        raise
    commit_software_transaction(software_tx)
    return {
        "setup_id": setup["id"],
        "profile_id": profile["id"],
        "changed": changed,
        "backup_slot": backup_slot,
        "software_changed": software_tx.changed,
        "software": {
            "state": "installed",
            "version": software_tx.metadata["version"],
            "platform": software_tx.metadata["platform"],
        },
        "target": str(validate_target(target, create=False)),
    }


def write_setup(
    target: Path,
    setup: dict[str, Any],
    profile: dict[str, Any],
    *,
    require_existing: bool = False,
    repair_software: bool = False,
) -> dict[str, Any]:
    with target_lock(target, create_parent=True) as directory_transaction:
        return write_setup_locked(
            target,
            setup,
            profile,
            require_existing=require_existing,
            repair_software=repair_software,
            directory_transaction=directory_transaction,
        )


def update_setup(target: Path, setup_id: str | None, profile_id: str | None) -> dict[str, Any]:
    with target_lock(target, create_parent=False) as directory_transaction:
        target = validate_target(target, create=False)
        if not lstat_exists(target):
            fail("update requires an already managed target")
        current = read_stamp(target)
        if current is None:
            fail("update requires an already managed target")
        if is_legacy_stamp(current):
            fail("legacy managed target must be migrated before update")
        setup = load_setup(setup_id or current["setup_id"])
        profile = load_profile(profile_id or current["profile_id"])
        return write_setup_locked(
            target,
            setup,
            profile,
            require_existing=True,
            repair_software=True,
            directory_transaction=directory_transaction,
        )


def migrate_setup(target: Path, setup_id: str | None, profile_id: str | None) -> dict[str, Any]:
    with target_lock(target, create_parent=False) as directory_transaction:
        target = validate_target(target, create=False)
        if not lstat_exists(target):
            fail("migrate requires an already managed target")
        current = read_stamp(target)
        if current is None:
            fail("migrate requires an already managed target")
        if current["schema_version"] == STAMP_SCHEMA:
            if setup_id is not None and setup_id != current["setup_id"]:
                fail("migrate setup selection does not match the current target")
            if profile_id is not None and profile_id != current["profile_id"]:
                fail("migrate profile selection does not match the current target")
            return {
                "setup_id": current["setup_id"],
                "profile_id": current["profile_id"],
                "migrated": False,
                "target": str(validate_target(target, create=False)),
            }
        selected_setup = setup_id or DEFAULT_SETUP_ID
        if selected_setup != DEFAULT_SETUP_ID:
            fail("migrate supports only --setup nddev-builder")
        selected_profile = profile_id or stamp_profile_id(current)
        if selected_profile is None:
            fail("legacy setup has no safe profile mapping; pass --profile safe or --profile full-auto")
        profile = load_profile(selected_profile)
        result = write_setup_locked(
            target,
            load_setup(selected_setup),
            profile,
            require_existing=True,
            repair_software=True,
            allow_legacy_migration=True,
            directory_transaction=directory_transaction,
        )
        result["migrated"] = True
        result["legacy_setup_id"] = current["setup_id"]
        return result


def restore_backup(target: Path, slot: int) -> dict[str, Any]:
    if slot < 0 or slot > 9:
        fail("backup slot must be between 0 and 9")
    with target_lock(target, create_parent=True) as directory_transaction:
        target = validate_target(target, create=True, transaction=directory_transaction)
        require_private_directory(backup_pool(target), "backup pool")
        require_private_directory(backup_pool(target) / str(slot), f"backup slot {slot}")
        envelope_path = backup_pool(target) / str(slot) / BACKUP_NAME
        envelope = read_json_file(envelope_path, max_bytes=METADATA_MAX_BYTES, label=BACKUP_NAME)
        plan = build_backup_restore_plan(target, slot, envelope)
        current_stamp = read_stamp(target)
        current_relatives = [] if current_stamp is None else stamp_managed_relatives(current_stamp)
        restore_relatives = unique_relatives([*current_relatives, *plan.relatives])
        snapshot = snapshot_files(target, restore_relatives)
        try:
            for relative in current_relatives:
                if relative not in plan.files:
                    with contextlib.suppress(FileNotFoundError):
                        safe_target_path(target, relative).unlink()
            for relative in plan.relatives:
                atomic_write(safe_target_path(target, relative), plan.files[relative], target)
            prune_empty_managed_dirs(target, restore_relatives)
            restored_stamp = validate_restored_backup_state(target, plan.stamp)
        except BaseException:
            restore_snapshot(target, snapshot)
            validate_snapshot_restored(target, snapshot)
            raise
        return {
            "setup_id": None if restored_stamp is None else stamp_setup_id(restored_stamp),
            "profile_id": None if restored_stamp is None else stamp_profile_id(restored_stamp),
            "legacy_setup_id": (
                None
                if restored_stamp is None or not is_legacy_stamp(restored_stamp)
                else restored_stamp["setup_id"]
            ),
            "backup_slot": slot,
            "target": str(validate_target(target, create=False)),
        }


def remove_setup(target: Path) -> dict[str, Any]:
    if not lstat_exists(target.parent) or not lstat_exists(target):
        return {
            "removed_setup_id": None,
            "removed_profile_id": None,
            "removed_legacy_setup_id": None,
            "target": str(validate_target(target, create=False)),
        }
    with target_lock(target, create_parent=False):
        target = validate_target(target, create=False)
        stamp = read_stamp(target)
        if stamp is None:
            return {
                "removed_setup_id": None,
                "removed_profile_id": None,
                "removed_legacy_setup_id": None,
                "target": str(validate_target(target, create=False)),
            }
        drift = drift_for_stamp(target, stamp)
        if drift:
            fail(f"managed target has drift: {', '.join(drift)}")
        removed_setup_id = stamp_setup_id(stamp)
        removed_profile_id = stamp_profile_id(stamp)
        removed_legacy_setup_id = stamp["setup_id"] if is_legacy_stamp(stamp) else None
        managed_relatives = stamp_managed_relatives(stamp)
        snapshot = snapshot_files(target, managed_relatives)
        runtime_tx: RuntimeRemoveTransaction | None = None
        try:
            runtime_tx = begin_runtime_remove(target)
            legacy = stamp.get("schema_version") == LEGACY_STAMP_SCHEMA
            for relative in managed_relatives:
                if legacy and relative in LEGACY_MANAGED_JSON_KEYS:
                    remove_managed_json_keys(target, relative)
                elif legacy and relative in MERGED_MARKER_PATHS:
                    remove_managed_block_from_target(target, relative)
                else:
                    with contextlib.suppress(FileNotFoundError):
                        safe_target_path(target, relative).unlink()
            with contextlib.suppress(FileNotFoundError):
                stamp_path(target).unlink()
            prune_empty_managed_dirs(target, managed_relatives)
        except BaseException:
            if runtime_tx is not None:
                rollback_runtime_remove(target, runtime_tx)
            restore_snapshot(target, snapshot)
            raise
        if runtime_tx is not None:
            commit_runtime_remove(runtime_tx)
        return {
            "removed_setup_id": removed_setup_id,
            "removed_profile_id": removed_profile_id,
            "removed_legacy_setup_id": removed_legacy_setup_id,
            "software_removed": runtime_tx.changed if runtime_tx is not None else False,
            "target": str(validate_target(target, create=False)),
        }


def remove_managed_json_keys(target: Path, relative: str) -> None:
    path = safe_target_path(target, relative)
    data = read_existing_file(path, max_bytes=MANAGED_MAX_BYTES, label=relative)
    if data is None:
        return
    value = parse_optional_json(data, relative)
    for key in LEGACY_MANAGED_JSON_KEYS[relative]:
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


def prune_empty_managed_dirs(target: Path, relatives: list[str] | None = None) -> None:
    candidates: set[Path] = set()
    for relative in relatives or list(BASE_CONTROL_MANAGED_PATHS):
        directory = safe_target_path(target, relative).parent
        while directory != target and target in directory.parents:
            candidates.add(directory)
            directory = directory.parent
    directories = sorted(candidates, key=lambda item: len(item.parts), reverse=True)
    for directory in directories:
        with contextlib.suppress(OSError):
            directory.rmdir()


def plan_payload(target: Path, setup: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    status = status_payload(target)
    operation = "install"
    backup_required = False
    if status["managed"]:
        if not status.get("launchable"):
            operation = "migrate"
            backup_required = True
        elif status["setup_id"] == setup["id"] and status["profile_id"] == profile["id"]:
            operation = "update"
        else:
            operation = "switch"
            backup_required = True
    return {
        "operation": operation,
        "setup_id": setup["id"],
        "profile_id": profile["id"],
        "target": str(validate_target(target, create=False)),
        "current_setup_id": status["setup_id"],
        "current_profile_id": status["profile_id"],
        "legacy_setup_id": status["legacy_setup_id"],
        "drift": status["drift"],
        "backup_required": backup_required,
        "software": status["software"],
        "mutates": False,
    }


def child_args_use_target_scope_overrides(child_args: list[str]) -> str | None:
    for index, arg in enumerate(child_args):
        if arg == "--":
            return None
        if arg in TARGET_SCOPE_FLAGS:
            return arg
        for flag in TARGET_SCOPE_FLAGS:
            if arg.startswith(f"{flag}="):
                return flag
        if index > 0 and child_args[index - 1] in TARGET_SCOPE_FLAGS:
            return child_args[index - 1]
    return None


def build_launch_plan_locked(target: Path, child_args: list[str]) -> LaunchPlan:
    target = validate_target(target, create=False)
    status = status_payload(target)
    if not status["managed"]:
        fail("launch requires a managed target")
    if not status.get("launchable"):
        fail("launch requires a migrated schema 3 target")
    if status["drift"]:
        fail(f"managed target has drift: {', '.join(status['drift'])}")
    canonical = validate_target(target, create=False)
    metadata = current_software_metadata(canonical)
    home = runtime_home(canonical)
    data = runtime_data(canonical)
    cache = runtime_cache(canonical)
    tmp = runtime_tmp(canonical)
    ensure_private_directory(cache, canonical, "runtime cache")
    ensure_private_directory(tmp, canonical, "runtime tmp")
    child_env = sanitized_subprocess_env(home, data, tmp)
    child_env.update(
        {
            "JUNIE_CONFIG_LOCATION": str((canonical / "config.json").resolve()),
            "JUNIE_CONFIG_DEFAULT_LOCATIONS": "false",
            "JUNIE_PROJECT": str(canonical),
            "JUNIE_SKILL_LOCATIONS": str((canonical / "skills").resolve()),
            "JUNIE_SKILL_DEFAULT_LOCATIONS": "false",
            "JUNIE_AGENT_LOCATIONS": str((canonical / "agents").resolve()),
            "JUNIE_AGENT_DEFAULT_LOCATIONS": "false",
            "JUNIE_COMMAND_LOCATIONS": str((canonical / "commands").resolve()),
            "JUNIE_COMMAND_DEFAULT_LOCATIONS": "false",
            "JUNIE_MCP_LOCATIONS": str((canonical / "mcp").resolve()),
            "JUNIE_MCP_DEFAULT_LOCATIONS": "false",
            "JUNIE_MODEL_DEFAULT_LOCATIONS": "false",
            "JUNIE_EXTENSIONS_DEFAULT_LOCATION": str(
                (canonical / "extensions" / "cache").resolve()
            ),
            "JUNIE_GUIDELINES_FILENAME": str((canonical / "AGENTS.md").resolve()),
        }
    )
    if not isinstance(metadata.get("shim"), dict) or not isinstance(metadata.get("binary"), dict):
        fail("Junie software metadata is invalid")
    shim_relative = metadata["shim"].get("path")
    binary_relative = metadata["binary"].get("path")
    if not isinstance(shim_relative, str) or not isinstance(binary_relative, str):
        fail("Junie software metadata path is invalid")
    shim_path = safe_target_path(canonical, shim_relative)
    binary_path = safe_target_path(canonical, binary_relative)
    command = [str(shim_path)]
    if "--skip-update-check" not in child_args:
        command.append("--skip-update-check")
    command.extend(
        [
            "--project",
            str(canonical),
            "--config-location",
            str((canonical / "config.json").resolve()),
            "--config-default-locations",
            "false",
            "--guidelines-filename",
            str((canonical / "AGENTS.md").resolve()),
            "--mcp-location",
            str((canonical / "mcp").resolve()),
            "--mcp-default-locations",
            "false",
            "--skill-location",
            str((canonical / "skills").resolve()),
            "--skill-default-locations",
            "false",
            "--agent-location",
            str((canonical / "agents").resolve()),
            "--agent-default-location",
            "false",
            "--command-location",
            str((canonical / "commands").resolve()),
            "--command-default-locations",
            "false",
            "--model-default-locations",
            "false",
            "--extensions-default-location",
            str((canonical / "extensions" / "cache").resolve()),
            "--cache-dir",
            str(cache),
        ]
    )
    command.extend(child_args)
    shim = capture_launch_file_snapshot(
        canonical,
        shim_path,
        "Junie shim",
        max_bytes=MANAGED_MAX_BYTES,
    )
    binary = capture_launch_file_snapshot(
        canonical,
        binary_path,
        "Junie binary",
        max_bytes=SOFTWARE_FILE_MAX_BYTES,
    )
    if shim.sha256 != metadata["shim"].get("sha256"):
        runtime_fail(
            "Junie shim digest changed after software validation",
            code="junie_shim_digest_changed",
            repairable=False,
        )
    expected_binary_size = metadata["binary"].get("size")
    expected_binary_sha256 = metadata["binary"].get("sha256")
    if binary.size != expected_binary_size or binary.sha256 != expected_binary_sha256:
        runtime_fail(
            "Junie binary changed after software validation",
            code="junie_binary_changed",
            repairable=False,
        )
    return LaunchPlan(
        command=command,
        child_env=child_env,
        cwd=canonical,
        shim=shim,
        binary=binary,
    )


def launch(target: Path, child_args: list[str]) -> int:
    override = child_args_use_target_scope_overrides(child_args)
    if override is not None:
        fail(f"{override} is managed by the target launch environment")
    with target_lock(target, create_parent=False):
        plan = build_launch_plan_locked(target, child_args)
        live_before = snapshot_live_junie_home()
        revalidate_launch_artifacts(plan)
        try:
            completed = subprocess.run(
                plan.command,
                cwd=plan.cwd,
                env=plan.child_env,
                check=False,
            )
        except FileNotFoundError:
            fail("target-owned Junie command was not found")
        finally:
            require_live_junie_home_unchanged(live_before, context="Junie launch")
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
    for name in ("plan", "install"):
        command = subparsers.add_parser(name)
        command.add_argument("--setup", default=DEFAULT_SETUP_ID)
        command.add_argument("--profile", default=DEFAULT_PROFILE_ID)
        command.add_argument("--target", required=True)
        command.add_argument("--json", action="store_true")
    switch = subparsers.add_parser("switch")
    switch.add_argument("--setup", default=DEFAULT_SETUP_ID)
    switch.add_argument("--profile", required=True)
    switch.add_argument("--target", required=True)
    switch.add_argument("--json", action="store_true")
    update = subparsers.add_parser("update")
    update.add_argument("--setup")
    update.add_argument("--profile")
    update.add_argument("--target", required=True)
    update.add_argument("--json", action="store_true")
    migrate = subparsers.add_parser("migrate")
    migrate.add_argument("--setup")
    migrate.add_argument("--profile")
    migrate.add_argument("--target", required=True)
    migrate.add_argument("--json", action="store_true")
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
        profiles = list_profiles()
        emit(
            {
                "setups": [item["id"] for item in items],
                "profiles": [item["id"] for item in profiles],
                "items": items,
                "profile_items": profiles,
                "default_setup_id": DEFAULT_SETUP_ID,
                "default_profile_id": DEFAULT_PROFILE_ID,
            },
            as_json=args.json,
        )
        return 0
    if args.command == "status":
        emit(status_payload(require_absolute_target(args.target)), as_json=args.json)
        return 0
    if args.command == "plan":
        target = require_absolute_target(args.target)
        emit(
            plan_payload(target, load_setup(args.setup), load_profile(args.profile)),
            as_json=args.json,
        )
        return 0
    if args.command == "install":
        target = require_absolute_target(args.target)
        emit(
            write_setup(target, load_setup(args.setup), load_profile(args.profile)),
            as_json=args.json,
        )
        return 0
    if args.command == "switch":
        target = require_absolute_target(args.target)
        emit(
            write_setup(
                target,
                load_setup(args.setup),
                load_profile(args.profile),
                require_existing=True,
            ),
            as_json=args.json,
        )
        return 0
    if args.command == "update":
        target = require_absolute_target(args.target)
        emit(update_setup(target, args.setup, args.profile), as_json=args.json)
        return 0
    if args.command == "migrate":
        target = require_absolute_target(args.target)
        emit(migrate_setup(target, args.setup, args.profile), as_json=args.json)
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
