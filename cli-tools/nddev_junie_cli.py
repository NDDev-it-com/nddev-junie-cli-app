#!/usr/bin/env python3
"""Transactional setup manager for an explicit Junie CLI target."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import fcntl
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
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

_ORIGINAL_OS_FSYNC = os.fsync
_ORIGINAL_OS_RENAME = os.rename
_ORIGINAL_OS_WRITE = os.write

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
SUPPORTED_PUBLIC_PLATFORMS = (
    "macos-arm64",
    "macos-x64",
    "ubuntu-glibc-arm64",
    "ubuntu-glibc-x64",
)
OFFICIAL_ARTIFACT_PLATFORM_BY_HOST = {
    "macos-arm64": "macos-aarch64",
    "macos-x64": "macos-amd64",
    "ubuntu-glibc-arm64": "linux-aarch64",
    "ubuntu-glibc-x64": "linux-amd64",
}
OFFICIAL_UNSUPPORTED_ARTIFACT_PLATFORMS = {
    "windows": ("windows-aarch64", "windows-amd64"),
}
STAMP_NAME = "NDDEV-JUNIE-CLI-SETUP.json"
BACKUP_NAME = "NDDEV-JUNIE-CLI-BACKUP.json"
STAMP_SCHEMA = 3
LEGACY_STAMP_SCHEMAS = (1, 2)
LEGACY_STAMP_SCHEMA = 1
RUNTIME_DIR_NAME = ".nddev-junie-cli-runtime"
RUNTIME_RECEIPT_NAME = "NDDEV-JUNIE-CLI-RUNTIME.json"
LAUNCH_IMAGE_DIR_NAME = "launch-image"
LAUNCH_IMAGE_COMMAND_NAME = "junie"
LOCK_DIR_NAME = ".nddev-junie-cli.lock"
LOCK_FILE_NAME = "lock"
BOOTSTRAP_LOCK_ROOT_NAME = f"{PRODUCT_NAME}-bootstrap-locks"
BACKUP_DIR_NAME = ".nddev-junie-cli-backups"
MANAGED_BEGIN = "<!-- BEGIN NDDEV-JUNIE-CLI MANAGED -->"
MANAGED_END = "<!-- END NDDEV-JUNIE-CLI MANAGED -->"
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700
OWNER_EXECUTABLE_MODE = 0o700
OWNER_READ_EXECUTE_DIRECTORY_MODE = 0o500
SAFE_SUBPROCESS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
MANAGED_MAX_BYTES = 8 * 1024 * 1024
METADATA_MAX_BYTES = 256 * 1024
INSTALLER_MAX_BYTES = 2 * 1024 * 1024
UPDATE_INFO_MAX_BYTES = 8 * 1024 * 1024
SOFTWARE_FILE_MAX_BYTES = 1024 * 1024 * 1024
INSTALL_TIMEOUT_SECONDS = 900
PROBE_TIMEOUT_SECONDS = 30
OS_RELEASE_MAX_BYTES = 128 * 1024
OS_RELEASE_PATHS = (Path("/etc/os-release"), Path("/usr/lib/os-release"))
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


class JunieCliArgumentError(Exception):
    """Argument parsing error that can be emitted as a JSON manager error."""


class JunieCliArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise JunieCliArgumentError(message)


@dataclass
class FileSnapshot:
    exists: bool
    data: bytes | None = None
    mode: int | None = None
    device: int | None = None
    inode: int | None = None
    mtime_ns: int | None = None
    preserved_path: Path | None = None


@dataclass
class DirectorySnapshot:
    exists: bool
    mode: int | None = None
    mtime_ns: int | None = None
    device: int | None = None
    inode: int | None = None


@dataclass
class TransactionSnapshot:
    target_existed: bool
    target_mode: int | None
    files: dict[str, FileSnapshot]
    directories: dict[str, DirectorySnapshot]
    preserve_dir: Path | None = None


@dataclass
class SoftwareTransaction:
    metadata: dict[str, Any]
    changed: bool
    previous_root: Path | None = None
    new_root: Path | None = None
    previous_directories: dict[str, DirectorySnapshot] | None = None


@dataclass
class RuntimeRemoveTransaction:
    changed: bool
    removed_root: Path | None = None


@dataclass
class BackupTransaction:
    pool: Path | None = None
    pool_created: bool = False
    pool_directory: DirectorySnapshot | None = None
    slot: int | None = None
    slot_dir: Path | None = None
    slot_directory: DirectorySnapshot | None = None
    envelope: bytes | None = None
    previous_envelope: FileSnapshot | None = None
    preserved_envelope_path: Path | None = None
    slot_created: bool = False
    slot_committed: bool = False
    rolled_back: bool = False


@dataclass(frozen=True)
class BackupRestorePlan:
    files: dict[str, bytes]
    stamp: dict[str, Any]
    relatives: list[str]


@dataclass
class TargetLockHandle:
    directory: Path
    lock_file: Path
    directory_fd: int
    lock_fd: int
    original_directory_mode: int
    created_directory: bool = False
    created_lock_file: bool = False


@dataclass
class BootstrapLockHandle:
    path: Path
    fd: int
    binding: dict[str, Any]


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
    launcher: LaunchFileSnapshot


@dataclass
class ProtectedDirectory:
    path: Path
    fd: int
    original_mode: int


@dataclass
class LaunchProtection:
    directories: list[ProtectedDirectory]
    file_fds: list[int]


@dataclass
class DirectoryTransaction:
    created: list[Path]

    def cleanup(self) -> None:
        for path in reversed(self.created):
            with contextlib.suppress(OSError):
                rmdir_path(path)


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


def require_live_junie_home_unchanged(before: LiveJunieHomeSnapshot, *, context: str) -> None:
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


def parse_os_release_text(data: bytes, *, label: str) -> dict[str, str]:
    if len(data) > OS_RELEASE_MAX_BYTES:
        fail(f"{label} is too large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        fail(f"{label} is not UTF-8: {exc}")
    result: dict[str, str] = {}
    for line_number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator != "=" or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            fail(f"{label} line {line_number} is not valid os-release metadata")
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError as exc:
            fail(f"{label} line {line_number} has invalid quoting: {exc}")
        if len(parts) != 1:
            fail(f"{label} line {line_number} must contain exactly one value")
        result[key] = parts[0]
    return result


def read_os_release_file(paths: tuple[Path, ...] | None = None) -> dict[str, str]:
    paths = OS_RELEASE_PATHS if paths is None else paths
    errors: list[str] = []
    for path in paths:
        try:
            with path.open("rb") as handle:
                data = handle.read(OS_RELEASE_MAX_BYTES + 1)
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        return parse_os_release_text(data, label=str(path))
    details = "; ".join(errors)
    if details:
        fail(f"cannot read Linux distribution metadata: {details}")
    fail("Linux distribution detection requires /etc/os-release metadata")


def linux_os_release(os_release: dict[str, str] | None = None) -> dict[str, str]:
    if os_release is not None:
        return {str(key): str(value) for key, value in os_release.items()}
    reader = getattr(platform, "freedesktop_os_release", None)
    if reader is not None:
        try:
            return {str(key): str(value) for key, value in reader().items()}
        except OSError as exc:
            fail(f"cannot read Linux distribution metadata: {exc}")
    return read_os_release_file()


def require_glibc_linux(libc: tuple[str, str] | None = None) -> None:
    detected = platform.libc_ver() if libc is None else libc
    libc_name = (detected[0] if detected else "").lower()
    if libc_name != "glibc":
        fail("unsupported Linux C library: official Junie linux-* artifacts require glibc")


def current_host_id(
    *,
    system: str | None = None,
    machine: str | None = None,
    os_release: dict[str, str] | None = None,
    libc: tuple[str, str] | None = None,
) -> str:
    system = platform.system() if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    if system == "Linux":
        release = linux_os_release(os_release)
        distribution = release.get("ID", "").strip().lower()
        if distribution != "ubuntu":
            fail(
                "unsupported Linux distribution: "
                f"{distribution or 'unknown'}; supported Linux scope is Ubuntu Desktop/Server"
            )
        require_glibc_linux(libc)
        os_name = "ubuntu-glibc"
    elif system == "Darwin":
        os_name = "macos"
    else:
        fail(f"unsupported OS: {system}")
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        fail(f"unsupported architecture: {machine}")
    return f"{os_name}-{arch}"


def current_platform_id(
    *,
    system: str | None = None,
    machine: str | None = None,
    os_release: dict[str, str] | None = None,
    libc: tuple[str, str] | None = None,
) -> str:
    host_id = current_host_id(
        system=system,
        machine=machine,
        os_release=os_release,
        libc=libc,
    )
    return OFFICIAL_ARTIFACT_PLATFORM_BY_HOST[host_id]


def require_supported_host() -> str:
    return current_host_id()


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
    baseline: dict[str, Any], update_info_url: str, platform_id: str | None = None
) -> dict[str, Any]:
    platform_id = current_platform_id() if platform_id is None else platform_id
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
    for expected_platform_id, artifact in expected.items():
        row = matches[expected_platform_id]
        common_expected = {
            "marketing": baseline["release"]["marketing_version"],
            "version": version,
            "platform": expected_platform_id,
        }
        for key, expected_value in common_expected.items():
            if row.get(key) != expected_value:
                fail(f"update-info {expected_platform_id} {key} does not match the baseline")
        exact_expected = {
            "downloadUrl": artifact["download_url"],
            "sha256": artifact["sha256"],
            "size": artifact["size"],
        }
        for key, expected_value in exact_expected.items():
            if row.get(key) != expected_value:
                fail(f"update-info {expected_platform_id} {key} does not match the baseline")
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


def validate_target_identity_for_lock(target: Path) -> Path:
    canonical = canonicalize_target_parent(target)
    current = canonical.parent
    while True:
        info = stat_existing(current, "target lock identity ancestor")
        if info is not None:
            if not stat.S_ISDIR(info.st_mode):
                fail("target lock identity ancestor must be a directory")
            require_safe_target_parent(current, "target lock identity ancestor")
            return canonical
        parent = current.parent
        if parent == current:
            fail("target lock identity ancestor is missing")
        current = parent


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
    fsync_directory(path.parent)


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


def nofollow_flag() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        fail("O_NOFOLLOW is required for target lock safety")
    return os.O_NOFOLLOW


def verify_fd_matches_path(fd: int, path: Path, label: str) -> os.stat_result:
    fd_info = os.fstat(fd)
    path_info = stat_existing(path, label)
    if path_info is None:
        fail(f"{label} is missing")
    if fd_info.st_dev != path_info.st_dev or fd_info.st_ino != path_info.st_ino:
        fail(f"{label} changed while opening")
    return fd_info


def open_directory_nofollow(path: Path, label: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | nofollow_flag()
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        fail(f"{label} could not be opened safely: {exc}")
    try:
        info = verify_fd_matches_path(fd, path, label)
        require_current_owner(info, label)
        if not stat.S_ISDIR(info.st_mode):
            fail(f"{label} must be a directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            fail(f"{label} must be private")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def open_owned_directory_nofollow(path: Path, label: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | nofollow_flag()
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        fail(f"{label} could not be opened safely: {exc}")
    try:
        info = verify_fd_matches_path(fd, path, label)
        require_current_owner(info, label)
        if not stat.S_ISDIR(info.st_mode):
            fail(f"{label} must be a directory")
        if stat.S_IMODE(info.st_mode) & 0o022:
            fail(f"{label} must not be group/world writable")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def open_regular_nofollow(
    path: Path,
    label: str,
    *,
    create: bool,
    mode: int,
    repair_created_mode: bool = False,
) -> tuple[int, os.stat_result]:
    flags = os.O_RDWR | nofollow_flag()
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(path, flags, mode)
    except OSError as exc:
        fail(f"{label} could not be opened safely: {exc}")
    try:
        info = verify_fd_matches_path(fd, path, label)
        require_current_owner(info, label)
        if not stat.S_ISREG(info.st_mode):
            fail(f"{label} must be a regular file")
        if info.st_nlink != 1:
            fail(f"{label} must not be a hardlink")
        if repair_created_mode and stat.S_IMODE(info.st_mode) != mode:
            os.fchmod(fd, mode)
            info = os.fstat(fd)
        if stat.S_IMODE(info.st_mode) != mode:
            fail(f"{label} mode must be {mode:04o}")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def bootstrap_lock_system_root() -> Path:
    system = platform.system()
    if system == "Darwin":
        candidate = Path("/private/tmp")
    elif system == "Linux":
        candidate = Path("/tmp")
    else:
        fail("bootstrap lock supports only macOS and Linux")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        fail(f"system bootstrap lock root is unavailable: {exc}")
    info = stat_existing(resolved, "system bootstrap lock root")
    if info is None:
        fail("system bootstrap lock root is missing")
    if not stat.S_ISDIR(info.st_mode):
        fail("system bootstrap lock root must be a directory")
    mode = stat.S_IMODE(info.st_mode)
    if not (mode & stat.S_ISVTX) or not (mode & 0o002):
        fail("system bootstrap lock root must be sticky and writable")
    return resolved


def bootstrap_lock_product_root_path(system_root: Path | None = None) -> Path:
    uid = current_user_id()
    if uid is None:
        fail("bootstrap lock requires a POSIX current user id")
    root = bootstrap_lock_system_root() if system_root is None else system_root
    return root / f"{BOOTSTRAP_LOCK_ROOT_NAME}-uid-{uid}"


def ensure_bootstrap_lock_product_root() -> Path:
    system_root = bootstrap_lock_system_root()
    root = bootstrap_lock_product_root_path(system_root)
    info = stat_existing(root, "bootstrap lock product root")
    created = False
    if info is None:
        try:
            root.mkdir(mode=OWNER_DIRECTORY_MODE)
            created = True
        except FileExistsError:
            pass
    fd, info = open_directory_nofollow(root, "bootstrap lock product root")
    try:
        mode = stat.S_IMODE(info.st_mode)
        if created and mode != OWNER_DIRECTORY_MODE:
            os.fchmod(fd, OWNER_DIRECTORY_MODE)
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode != OWNER_DIRECTORY_MODE:
            fail("bootstrap lock product root mode must be 0700")
    finally:
        os.close(fd)
    return root


def bootstrap_lock_digest_for(namespace: str, identity: str) -> str:
    return sha256_bytes(f"{PRODUCT_NAME}\0{namespace}\0{identity}".encode("utf-8"))


def bootstrap_lock_digest(canonical: Path) -> str:
    return bootstrap_lock_digest_for("target-lifecycle", str(canonical))


def lexical_bootstrap_lock_digest(target: Path) -> str:
    return bootstrap_lock_digest_for("target-lifecycle-precanonical", str(target))


def bootstrap_lock_path(canonical: Path) -> Path:
    return ensure_bootstrap_lock_product_root() / f"{bootstrap_lock_digest(canonical)}.lock"


def lexical_bootstrap_lock_path(target: Path) -> Path:
    return ensure_bootstrap_lock_product_root() / f"{lexical_bootstrap_lock_digest(target)}.lock"


def bootstrap_lock_binding(canonical: Path) -> dict[str, Any]:
    uid = current_user_id()
    if uid is None:
        fail("bootstrap lock requires a POSIX current user id")
    digest = bootstrap_lock_digest(canonical)
    return {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "namespace": "target-lifecycle",
        "canonical_target": str(canonical),
        "canonical_target_sha256": digest,
        "uid": uid,
    }


def lexical_bootstrap_lock_binding(target: Path) -> dict[str, Any]:
    uid = current_user_id()
    if uid is None:
        fail("bootstrap lock requires a POSIX current user id")
    digest = lexical_bootstrap_lock_digest(target)
    return {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "namespace": "target-lifecycle-precanonical",
        "lexical_target": str(target),
        "lexical_target_sha256": digest,
        "uid": uid,
    }


def verify_bootstrap_lock_fd_path(fd: int, path: Path) -> os.stat_result:
    info = verify_fd_matches_path(fd, path, "bootstrap lock file")
    require_current_owner(info, "bootstrap lock file")
    if not stat.S_ISREG(info.st_mode):
        fail("bootstrap lock file must be a regular file")
    if info.st_nlink != 1:
        fail("bootstrap lock file must not be a hardlink")
    if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE:
        fail("bootstrap lock file mode must be 0600")
    return info


def open_bootstrap_lock_file(path: Path) -> int:
    flags = os.O_RDWR | nofollow_flag()
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    created = False
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, OWNER_FILE_MODE)
        created = True
    except FileExistsError:
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            fail(f"bootstrap lock file could not be opened safely: {exc}")
    except OSError as exc:
        fail(f"bootstrap lock file could not be created safely: {exc}")
    try:
        info = os.fstat(fd)
        if created and stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE:
            os.fchmod(fd, OWNER_FILE_MODE)
        verify_bootstrap_lock_fd_path(fd, path)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_bootstrap_lock_bytes(fd: int) -> bytes:
    chunks = bytearray()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > METADATA_MAX_BYTES:
            fail("bootstrap lock binding is too large")
    os.lseek(fd, 0, os.SEEK_SET)
    return bytes(chunks)


def write_bootstrap_lock_binding(fd: int, binding: dict[str, Any]) -> None:
    data = canonical_json(binding)
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    total = 0
    while total < len(data):
        total += os.write(fd, data[total:])
    os.fsync(fd)
    os.lseek(fd, 0, os.SEEK_SET)


def ensure_bootstrap_lock_binding(fd: int, binding: dict[str, Any]) -> None:
    data = read_bootstrap_lock_bytes(fd)
    if not data:
        write_bootstrap_lock_binding(fd, binding)
        data = read_bootstrap_lock_bytes(fd)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"bootstrap lock binding is invalid JSON: {exc}")
    if value != binding:
        fail("bootstrap lock binding does not match the canonical target")


def acquire_bootstrap_lock_at(path: Path, binding: dict[str, Any]) -> BootstrapLockHandle:
    fd = open_bootstrap_lock_file(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail(f"target is locked: {path}")
        verify_bootstrap_lock_fd_path(fd, path)
        ensure_bootstrap_lock_binding(fd, binding)
        verify_bootstrap_lock_fd_path(fd, path)
        return BootstrapLockHandle(path=path, fd=fd, binding=binding)
    except BaseException:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        raise


def acquire_bootstrap_lock(canonical: Path) -> BootstrapLockHandle:
    return acquire_bootstrap_lock_at(
        bootstrap_lock_path(canonical), bootstrap_lock_binding(canonical)
    )


def acquire_lexical_bootstrap_lock(target: Path) -> BootstrapLockHandle:
    return acquire_bootstrap_lock_at(
        lexical_bootstrap_lock_path(target),
        lexical_bootstrap_lock_binding(target),
    )


def release_bootstrap_lock(handle: BootstrapLockHandle) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(handle.fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(handle.fd)


def lock_file_path(target: Path) -> Path:
    return lock_path(target) / LOCK_FILE_NAME


def ensure_lock_directory(path: Path) -> tuple[bool, int, os.stat_result]:
    created = False
    info = stat_existing(path, "target lock parent")
    if info is None:
        try:
            path.mkdir(mode=OWNER_DIRECTORY_MODE)
            created = True
        except FileExistsError:
            fail("target lock parent appeared during creation")
    fd, info = open_directory_nofollow(path, "target lock parent")
    mode = stat.S_IMODE(info.st_mode)
    if mode not in (OWNER_DIRECTORY_MODE, OWNER_READ_EXECUTE_DIRECTORY_MODE):
        os.close(fd)
        fail("target lock parent mode must be 0700 or recovered 0500")
    return created, fd, info


def acquire_target_lock(
    canonical: Path,
    transaction: DirectoryTransaction,
    *,
    cleanup_created_artifacts_on_failure: bool,
) -> TargetLockHandle:
    directory = lock_path(canonical)
    created_directory = False
    created_lock_file = False
    repaired_directory_mode = False
    directory_fd = -1
    lock_fd = -1
    try:
        created_directory, directory_fd, directory_info = ensure_lock_directory(directory)
        original_mode = stat.S_IMODE(directory_info.st_mode)
        file_path = lock_file_path(canonical)
        created_lock_file = not lstat_exists(file_path)
        if original_mode == OWNER_READ_EXECUTE_DIRECTORY_MODE and created_lock_file:
            os.fchmod(directory_fd, OWNER_DIRECTORY_MODE)
            repaired_directory_mode = True
            original_mode = OWNER_DIRECTORY_MODE
        lock_fd, _ = open_regular_nofollow(
            file_path,
            "target lock file",
            create=True,
            mode=OWNER_FILE_MODE,
            repair_created_mode=created_lock_file,
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail(f"target is locked: {directory}")
        os.fchmod(directory_fd, OWNER_READ_EXECUTE_DIRECTORY_MODE)
        return TargetLockHandle(
            directory=directory,
            lock_file=file_path,
            directory_fd=directory_fd,
            lock_fd=lock_fd,
            original_directory_mode=original_mode,
            created_directory=created_directory,
            created_lock_file=created_lock_file,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            if lock_fd >= 0:
                os.close(lock_fd)
        with contextlib.suppress(OSError):
            if directory_fd >= 0:
                if created_directory or repaired_directory_mode:
                    os.fchmod(directory_fd, OWNER_DIRECTORY_MODE)
                os.close(directory_fd)
        if cleanup_created_artifacts_on_failure and created_lock_file:
            with contextlib.suppress(OSError):
                unlink_path_if_exists(lock_file_path(canonical))
        if cleanup_created_artifacts_on_failure and created_directory:
            with contextlib.suppress(OSError):
                rmdir_path(directory)
        transaction.cleanup()
        raise


def release_target_lock(handle: TargetLockHandle, *, cleanup_artifacts: bool) -> None:
    with contextlib.suppress(OSError):
        os.fchmod(handle.directory_fd, OWNER_DIRECTORY_MODE)
    with contextlib.suppress(OSError):
        fcntl.flock(handle.lock_fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(handle.lock_fd)
    with contextlib.suppress(OSError):
        os.close(handle.directory_fd)
    if cleanup_artifacts and handle.created_lock_file:
        with contextlib.suppress(OSError):
            unlink_path_if_exists(handle.lock_file)
    if cleanup_artifacts and handle.created_directory:
        with contextlib.suppress(OSError):
            rmdir_path(handle.directory)


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


def runtime_launch_image(target: Path) -> Path:
    return runtime_root(target) / LAUNCH_IMAGE_DIR_NAME


def runtime_launch_image_bin(target: Path) -> Path:
    return runtime_launch_image(target) / LAUNCH_IMAGE_COMMAND_NAME


def runtime_cache(target: Path) -> Path:
    return runtime_root(target) / "cache"


def runtime_tmp(target: Path) -> Path:
    return runtime_root(target) / "tmp"


def runtime_receipt_path(target: Path) -> Path:
    return runtime_root(target) / RUNTIME_RECEIPT_NAME


@contextlib.contextmanager
def target_lock(target: Path, *, create_parent: bool = False, allow_missing: bool = False):
    transaction = DirectoryTransaction([])
    lexical_handle = acquire_lexical_bootstrap_lock(target)
    bootstrap_handle: BootstrapLockHandle | None = None
    lock_handle: TargetLockHandle | None = None
    canonical: Path | None = None
    target_directory_before_lock: DirectorySnapshot | None = None
    lock_directory_before_lock: DirectorySnapshot | None = None
    failed = False
    try:
        canonical_identity = validate_target_identity_for_lock(target)
        bootstrap_handle = acquire_bootstrap_lock(canonical_identity)
        if create_parent:
            canonical = validate_target(target, create=True, transaction=transaction)
        else:
            canonical = validate_target(target, create=False)
            if not lstat_exists(canonical):
                if allow_missing:
                    yield transaction
                    return
                fail("target is missing")
        if canonical != canonical_identity:
            fail("target canonical identity changed after acquiring bootstrap lock")
        target_directory_before_lock = backup_directory_snapshot(canonical)
        lock_directory_before_lock = backup_directory_snapshot(lock_path(canonical))
        lock_handle = acquire_target_lock(
            canonical,
            transaction,
            cleanup_created_artifacts_on_failure=True,
        )
        yield transaction
    except BaseException:
        failed = True
        raise
    finally:
        if lock_handle is not None:
            release_target_lock(
                lock_handle,
                cleanup_artifacts=failed,
            )
        if failed and canonical is not None:
            restore_backup_directory_metadata(lock_path(canonical), lock_directory_before_lock)
            restore_backup_directory_metadata(canonical, target_directory_before_lock)
        if failed:
            transaction.cleanup()
        if bootstrap_handle is not None:
            release_bootstrap_lock(bootstrap_handle)
        release_bootstrap_lock(lexical_handle)


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
            fsync_directory(current.parent)
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


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | nofollow_flag())
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def unlink_path(path: Path) -> None:
    path.unlink()
    fsync_directory(path.parent)


def unlink_path_if_exists(path: Path) -> None:
    try:
        unlink_path(path)
    except FileNotFoundError:
        return


def rmdir_path(path: Path) -> None:
    parent = path.parent
    path.rmdir()
    fsync_directory(parent)


def fsync_directory_unpatched(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | nofollow_flag())
    try:
        _ORIGINAL_OS_FSYNC(fd)
    finally:
        os.close(fd)


def rmdir_path_unpatched(path: Path) -> None:
    parent = path.parent
    path.rmdir()
    fsync_directory_unpatched(parent)


def safe_rmtree_private_directory_fsynced(path: Path, label: str) -> None:
    safe_rmtree_private_directory(path, label)


def write_all_fd(fd: int, data: bytes) -> None:
    total = 0
    while total < len(data):
        written = os.write(fd, data[total:])
        if written <= 0:
            fail("temporary file write made no progress")
        total += written


def write_all_fd_unpatched(fd: int, data: bytes) -> None:
    total = 0
    while total < len(data):
        written = _ORIGINAL_OS_WRITE(fd, data[total:])
        if written <= 0:
            fail("temporary file write made no progress")
        total += written


def file_snapshot_for_atomic_write(path: Path) -> FileSnapshot:
    info = require_existing_managed_file(path, str(path), max_bytes=MANAGED_MAX_BYTES)
    if info is None:
        return FileSnapshot(exists=False)
    data = read_existing_file(path, max_bytes=MANAGED_MAX_BYTES, label=str(path))
    return FileSnapshot(
        exists=True,
        data=data,
        mode=stat.S_IMODE(info.st_mode),
    )


def restore_atomic_write_snapshot(path: Path, snapshot: FileSnapshot, target: Path) -> None:
    if not snapshot.exists:
        unlink_path_if_exists(path)
        return
    if snapshot.data is None or snapshot.mode is None:
        fail("atomic write snapshot is invalid")
    atomic_write(path, snapshot.data, target, mode=snapshot.mode)


def atomic_write(path: Path, data: bytes, target: Path, *, mode: int = OWNER_FILE_MODE) -> None:
    ensure_real_parent(path, target)
    previous = file_snapshot_for_atomic_write(path)
    temporary = path.with_name(f".{path.name}.nddev.tmp.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    replaced = False
    try:
        os.fchmod(fd, mode)
        write_all_fd(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        replaced = True
        fsync_directory(path.parent)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            unlink_path_if_exists(temporary)
        if replaced:
            restore_atomic_write_snapshot(path, previous, target)
        raise


def emergency_unlink_existing(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        require_private_directory(path, str(path))
        shutil.rmtree(path)
    else:
        path.unlink()
    fsync_directory_unpatched(path.parent)


def emergency_atomic_restore(path: Path, item: FileSnapshot, target: Path) -> None:
    if not item.exists:
        emergency_unlink_existing(path)
        return
    if item.data is None or item.mode is None:
        fail("rollback snapshot is invalid")
    ensure_real_parent(path, target)
    temporary = path.with_name(f".{path.name}.nddev.tmp.rollback.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, item.mode)
    published = False
    try:
        os.fchmod(fd, item.mode)
        write_all_fd_unpatched(fd, item.data)
        _ORIGINAL_OS_FSYNC(fd)
        os.close(fd)
        fd = -1
        _ORIGINAL_OS_RENAME(temporary, path)
        published = True
        fsync_directory_unpatched(path.parent)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if not published:
            with contextlib.suppress(OSError):
                temporary.unlink()
        raise


def planned_changed_relatives(
    target: Path, current: dict[str, Any] | None, files: dict[str, bytes]
) -> list[str]:
    previous_relatives = [] if current is None else stamp_managed_relatives(current)
    legacy = current is not None and is_legacy_stamp(current)
    changed = [
        relative
        for relative, data in files.items()
        if current_managed_digest(target, relative, legacy=legacy)
        != managed_digest_for_bytes(relative, data)
    ]
    changed.extend(relative for relative in previous_relatives if relative not in files)
    return unique_relatives(changed)


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
                    ],
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
    files[f"{package}/extensions/nddev-builder/mcp/.mcp.json"] = canonical_json({"mcpServers": {}})
    copy_builder_projection(files, "skills", f"{package}/extensions/nddev-builder/skills")
    copy_builder_projection(files, "agents", f"{package}/extensions/nddev-builder/agents")
    copy_builder_projection(files, "commands", f"{package}/extensions/nddev-builder/commands")
    return files


def managed_digest_for_bytes(relative: str, data: bytes, *, legacy: bool = False) -> str:
    if legacy and relative in MERGED_MARKER_PATHS:
        block = extract_managed_block(data.decode("utf-8"))
        if block is None:
            return ""
        return sha256_bytes(block.encode("utf-8"))
    if legacy and relative in LEGACY_MANAGED_JSON_KEYS:
        value = parse_optional_json(data, relative)
        subset = {key: value.get(key) for key in LEGACY_MANAGED_JSON_KEYS[relative] if key in value}
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
    revalidate_launch_file_snapshot(plan.cwd, plan.launcher)


def sha256_fd_bounded(fd: int, *, max_bytes: int, label: str) -> str:
    digest = hashlib.sha256()
    total = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            fail(f"{label} is too large")
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def open_verified_launch_file(target: Path, snapshot: LaunchFileSnapshot) -> int:
    path = safe_target_path(target, snapshot.relative_path)
    flags = os.O_RDONLY | nofollow_flag()
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        runtime_fail(
            f"{snapshot.label} could not be opened safely: {exc}",
            code=f"{label_slug(snapshot.label)}_open",
            repairable=False,
        )
    try:
        info = verify_fd_matches_path(fd, path, snapshot.label)
        require_current_owner(info, snapshot.label)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            runtime_fail(
                f"{snapshot.label} must be a single regular file",
                code=f"{label_slug(snapshot.label)}_type",
                repairable=False,
            )
        if (
            info.st_dev != snapshot.device
            or info.st_ino != snapshot.inode
            or info.st_size != snapshot.size
        ):
            runtime_fail(
                f"{snapshot.label} changed before verified handoff",
                code=f"{label_slug(snapshot.label)}_identity_changed",
                repairable=False,
            )
        current_sha256 = sha256_fd_bounded(
            fd,
            max_bytes=snapshot.max_bytes,
            label=snapshot.label,
        )
        if current_sha256 != snapshot.sha256:
            runtime_fail(
                f"{snapshot.label} digest changed before verified handoff",
                code=f"{label_slug(snapshot.label)}_digest_changed",
                repairable=False,
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_launch_file_snapshot_bytes(target: Path, snapshot: LaunchFileSnapshot) -> bytes:
    fd = open_verified_launch_file(target, snapshot)
    data = bytearray()
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > snapshot.max_bytes:
                runtime_fail(
                    f"{snapshot.label} is too large",
                    code=f"{label_slug(snapshot.label)}_too_large",
                    repairable=False,
                )
    finally:
        os.close(fd)
    result = bytes(data)
    if len(result) != snapshot.size or sha256_bytes(result) != snapshot.sha256:
        runtime_fail(
            f"{snapshot.label} changed while materializing launch image",
            code=f"{label_slug(snapshot.label)}_materialize_changed",
            repairable=False,
        )
    return result


def ensure_launch_image_directory(target: Path) -> None:
    directory = runtime_launch_image(target)
    ensure_private_directory(directory, target, "runtime launch image")
    fd, info = open_directory_nofollow(directory, "runtime launch image")
    try:
        mode = stat.S_IMODE(info.st_mode)
        if mode == OWNER_READ_EXECUTE_DIRECTORY_MODE:
            os.fchmod(fd, OWNER_DIRECTORY_MODE)
        elif mode != OWNER_DIRECTORY_MODE:
            runtime_fail(
                "runtime launch image mode must be 0700 or recovered 0500",
                code="runtime_launch_image_mode",
                repairable=False,
            )
    finally:
        os.close(fd)


def materialize_launch_image(target: Path, source: LaunchFileSnapshot) -> LaunchFileSnapshot:
    data = read_launch_file_snapshot_bytes(target, source)
    ensure_launch_image_directory(target)
    launcher_path = runtime_launch_image_bin(target)
    atomic_write(launcher_path, data, target, mode=OWNER_EXECUTABLE_MODE)
    launcher = capture_launch_file_snapshot(
        target,
        launcher_path,
        "Junie launch image",
        max_bytes=MANAGED_MAX_BYTES,
    )
    if launcher.size != source.size or launcher.sha256 != source.sha256:
        runtime_fail(
            "Junie launch image does not match the verified shim",
            code="junie_launch_image_digest",
            repairable=False,
        )
    return launcher


def launch_handoff_directories(plan: LaunchPlan) -> list[Path]:
    launcher_path = safe_target_path(plan.cwd, plan.launcher.relative_path)
    directory = runtime_launch_image(plan.cwd)
    if launcher_path.parent != directory:
        runtime_fail(
            "Junie launch image escaped the dedicated launcher directory",
            code="junie_launch_image_parent",
            repairable=False,
        )
    return [directory]


@contextlib.contextmanager
def protected_launch_handoff(plan: LaunchPlan):
    protection = LaunchProtection(directories=[], file_fds=[])
    try:
        for directory in launch_handoff_directories(plan):
            fd, info = open_owned_directory_nofollow(directory, f"launch parent {directory}")
            protection.directories.append(
                ProtectedDirectory(
                    path=directory,
                    fd=fd,
                    original_mode=stat.S_IMODE(info.st_mode),
                )
            )
        for protected in protection.directories:
            os.fchmod(protected.fd, OWNER_READ_EXECUTE_DIRECTORY_MODE)
        protection.file_fds.append(open_verified_launch_file(plan.cwd, plan.shim))
        protection.file_fds.append(open_verified_launch_file(plan.cwd, plan.binary))
        protection.file_fds.append(open_verified_launch_file(plan.cwd, plan.launcher))
        yield protection
    finally:
        for fd in reversed(protection.file_fds):
            with contextlib.suppress(OSError):
                os.close(fd)
        for protected in reversed(protection.directories):
            with contextlib.suppress(OSError):
                os.fchmod(protected.fd, protected.original_mode)
            with contextlib.suppress(OSError):
                os.close(protected.fd)


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
            unlink_path(current)
    current.symlink_to(desired)
    fsync_directory(current.parent)


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
    platform_id = current_platform_id()
    expected_artifact = dict(baseline["release"]["exact_artifacts"][platform_id])
    expected_artifact["platform"] = platform_id
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


def runtime_contains_only_setup_projection(target: Path) -> bool:
    root = runtime_root(target)
    if not lstat_exists(root):
        return True
    try:
        require_private_directory(root, "runtime root")
    except JunieCliSetupError:
        return False
    home = runtime_home(target)
    allowed_directories = {root, home, home / ".junie"}
    allowed_files = {home / ".junie" / "allowlist.json"}
    for path in root.rglob("*"):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            if path not in allowed_directories:
                return False
            if stat.S_IMODE(info.st_mode) & 0o077:
                return False
            continue
        if stat.S_ISREG(info.st_mode) and path in allowed_files:
            if stat.S_IMODE(info.st_mode) & 0o077:
                return False
            continue
        return False
    return True


def software_state(target: Path) -> dict[str, Any]:
    try:
        runtime_root(target).lstat()
    except FileNotFoundError:
        return {"state": "absent"}
    try:
        metadata = current_software_metadata(target)
    except RuntimeValidationError as exc:
        if runtime_contains_only_setup_projection(target):
            return {"state": "absent"}
        return {
            "state": "partial",
            "error": str(exc),
            "code": exc.code,
            "repairable": exc.repairable,
        }
    except JunieCliSetupError as exc:
        if runtime_contains_only_setup_projection(target):
            return {"state": "absent"}
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
    platform_id = current_platform_id()
    installer_url, expected_installer_sha256 = installer_source(baseline)
    update_info_url = update_info_source(baseline)
    installer_bytes = read_url_bounded(
        installer_url, max_bytes=INSTALLER_MAX_BYTES, label="Junie installer"
    )
    actual_installer_sha256 = sha256_bytes(installer_bytes)
    if actual_installer_sha256 != expected_installer_sha256:
        fail("Junie installer SHA256 does not match the pinned baseline")
    artifact = artifact_metadata_from_update_info(baseline, update_info_url, platform_id)
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


def move_managed_runtime_home_state(source_home: Path, destination_home: Path) -> None:
    source = source_home / ".junie"
    if not lstat_exists(source):
        return
    require_private_directory(source, "managed Junie runtime state")
    destination = destination_home / ".junie"
    if lstat_exists(destination):
        safe_rmtree_private_directory_fsynced(destination, "staged Junie runtime state")
    source.rename(destination)
    fsync_directory(source_home)
    fsync_directory(destination_home)


def restore_managed_runtime_home_state(new_home: Path | None, previous_home: Path | None) -> None:
    if new_home is None or previous_home is None:
        return
    source = new_home / ".junie"
    destination = previous_home / ".junie"
    if not lstat_exists(source) or lstat_exists(destination):
        return
    source.rename(destination)
    fsync_directory(source.parent)
    fsync_directory(destination.parent)


def restore_managed_runtime_root_state(new_root: Path | None, previous_root: Path | None) -> None:
    if new_root is None or previous_root is None:
        return
    source_home = new_root / "home"
    previous_home = previous_root / "home"
    if lstat_exists(source_home) and lstat_exists(previous_home):
        restore_managed_runtime_home_state(source_home, previous_home)


def runtime_directory_snapshot_map(target: Path) -> dict[str, DirectorySnapshot]:
    root = runtime_root(target)
    if not lstat_exists(root):
        return {}
    snapshots: dict[str, DirectorySnapshot] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        relative = path.relative_to(target).as_posix()
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            continue
        snapshots[relative] = DirectorySnapshot(
            exists=True,
            mode=stat.S_IMODE(info.st_mode),
            mtime_ns=info.st_mtime_ns,
            device=info.st_dev,
            inode=info.st_ino,
        )
    return snapshots


def restore_runtime_directory_metadata(
    target: Path, snapshots: dict[str, DirectorySnapshot] | None
) -> None:
    for relative, item in sorted(
        (snapshots or {}).items(), key=lambda entry: len(Path(entry[0]).parts), reverse=True
    ):
        path = safe_target_path(target, relative)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            continue
        if item.mode is not None and stat.S_IMODE(info.st_mode) != item.mode:
            chmod_private_directory(path, item.mode, f"runtime directory {relative}")
        if item.mtime_ns is not None:
            os.utime(path, ns=(info.st_atime_ns, item.mtime_ns), follow_symlinks=False)


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
    current_platform_id()
    root = runtime_root(target)
    require_runtime_directory(target, target, "target")
    root_info = stat_existing(root, "runtime root")
    if root_info is not None:
        runtime_private_directory(root, target, "runtime root", repairable=False)
    previous_directories = runtime_directory_snapshot_map(target) if root_info is not None else {}
    stage = target / f".{RUNTIME_DIR_NAME}.install-stage.{os.getpid()}.{time.time_ns()}"
    old_root = target / f".{RUNTIME_DIR_NAME}.old.{os.getpid()}.{time.time_ns()}"
    stage.mkdir(mode=OWNER_DIRECTORY_MODE)
    fsync_directory(target)
    previous_root: Path | None = None
    published_new_root = False
    try:
        install_result = install_software_to_stage(stage, baseline)
        staged_home = stage / "home"
        if root_info is not None:
            root.rename(old_root)
            fsync_directory(target)
            previous_root = old_root
            previous_home = previous_root / "home"
            if lstat_exists(previous_home):
                runtime_private_directory(previous_home, target, "runtime home", repairable=False)
                move_managed_runtime_home_state(previous_home, staged_home)
        stage.rename(root)
        published_new_root = True
        fsync_directory(target)
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
        if published_new_root:
            restore_managed_runtime_root_state(root, previous_root)
        if published_new_root and lstat_exists(root):
            safe_rmtree_private_directory_fsynced(root, "new runtime root")
        if previous_root is not None and lstat_exists(previous_root):
            previous_root.rename(root)
            fsync_directory(target)
            restore_runtime_directory_metadata(target, previous_directories)
        safe_rmtree_private_directory_if_exists(stage, "software install stage")
        if previous_root is None:
            prune_empty_runtime_dirs(target)
        raise
    safe_rmtree_private_directory_if_exists(stage, "software install stage")
    return SoftwareTransaction(
        metadata=metadata,
        changed=True,
        previous_root=previous_root,
        new_root=root,
        previous_directories=previous_directories,
    )


def commit_software_transaction(transaction: SoftwareTransaction) -> None:
    if transaction.previous_root is not None and lstat_exists(transaction.previous_root):
        safe_rmtree_private_directory_fsynced(
            transaction.previous_root,
            "previous runtime root",
        )


def rollback_software_transaction(target: Path, transaction: SoftwareTransaction) -> None:
    if not transaction.changed:
        return
    restore_managed_runtime_root_state(transaction.new_root, transaction.previous_root)
    if transaction.new_root is not None and lstat_exists(transaction.new_root):
        safe_rmtree_private_directory_fsynced(transaction.new_root, "new runtime root")
    if transaction.previous_root is not None and lstat_exists(transaction.previous_root):
        transaction.previous_root.rename(runtime_root(target))
        fsync_directory(target)
        restore_runtime_directory_metadata(target, transaction.previous_directories)
    else:
        prune_empty_runtime_dirs(target)


def begin_runtime_remove(target: Path) -> RuntimeRemoveTransaction:
    root = runtime_root(target)
    if not lstat_exists(root):
        return RuntimeRemoveTransaction(changed=False)
    runtime_private_directory(root, target, "runtime root", repairable=False)
    removed = target / f".{RUNTIME_DIR_NAME}.removed.{os.getpid()}.{time.time_ns()}"
    root.rename(removed)
    fsync_directory(target)
    return RuntimeRemoveTransaction(changed=True, removed_root=removed)


def commit_runtime_remove(transaction: RuntimeRemoveTransaction) -> None:
    if transaction.removed_root is not None:
        with contextlib.suppress(OSError):
            safe_rmtree_private_directory_fsynced(transaction.removed_root, "removed runtime root")


def rollback_runtime_remove(target: Path, transaction: RuntimeRemoveTransaction) -> None:
    if transaction.removed_root is not None and lstat_exists(transaction.removed_root):
        if lstat_exists(runtime_root(target)):
            info = runtime_root(target).lstat()
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                safe_rmtree_private_directory_fsynced(runtime_root(target), "runtime root")
            else:
                unlink_path(runtime_root(target))
        transaction.removed_root.rename(runtime_root(target))
        fsync_directory(runtime_root(target).parent)


def prune_empty_runtime_dirs(target: Path) -> None:
    for directory in (
        runtime_tmp(target),
        runtime_cache(target),
        runtime_home(target),
        runtime_root(target),
    ):
        with contextlib.suppress(OSError):
            rmdir_path(directory)


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
    elif expected_software == {"state": "absent"}:
        if software_state(target).get("state") != "absent":
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


def snapshot_existing_file(path: Path, relative: str, *, include_identity: bool) -> FileSnapshot:
    info = require_existing_managed_file(path, relative, max_bytes=MANAGED_MAX_BYTES)
    if info is None:
        return FileSnapshot(exists=False)
    data = read_existing_file(path, max_bytes=MANAGED_MAX_BYTES, label=relative)
    snapshot = FileSnapshot(
        exists=True,
        data=data,
        mode=stat.S_IMODE(info.st_mode),
    )
    if include_identity:
        snapshot.device = info.st_dev
        snapshot.inode = info.st_ino
        snapshot.mtime_ns = info.st_mtime_ns
    return snapshot


def snapshot_files(target: Path, relatives: list[str]) -> dict[str, FileSnapshot]:
    snapshot: dict[str, FileSnapshot] = {}
    for relative in unique_relatives([*relatives, STAMP_NAME]):
        path = safe_target_path(target, relative)
        snapshot[relative] = snapshot_existing_file(path, relative, include_identity=False)
    return snapshot


def transaction_directory_snapshot(
    target: Path, relatives: list[str]
) -> dict[str, DirectorySnapshot]:
    directories: dict[str, DirectorySnapshot] = {}
    candidates = {Path(".")}
    for relative in relatives:
        current = PurePosixPath(relative).parent
        while str(current) not in ("", "."):
            candidates.add(current)
            current = current.parent
    for relative_directory in sorted(candidates, key=lambda item: (len(item.parts), str(item))):
        path = (
            target
            if str(relative_directory) == "."
            else safe_target_path(target, str(relative_directory))
        )
        try:
            info = path.lstat()
        except FileNotFoundError:
            directories[str(relative_directory)] = DirectorySnapshot(exists=False)
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            directories[str(relative_directory)] = DirectorySnapshot(exists=False)
            continue
        directories[str(relative_directory)] = DirectorySnapshot(
            exists=True,
            mode=stat.S_IMODE(info.st_mode),
            mtime_ns=info.st_mtime_ns,
            device=info.st_dev,
            inode=info.st_ino,
        )
    return directories


def restore_directory_metadata(target: Path, snapshot: TransactionSnapshot) -> None:
    for relative, item in sorted(
        snapshot.directories.items(), key=lambda entry: len(Path(entry[0]).parts), reverse=True
    ):
        if not item.exists:
            continue
        path = target if relative == "." else safe_target_path(target, relative)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            continue
        if item.mode is not None and stat.S_IMODE(info.st_mode) != item.mode:
            chmod_private_directory(path, item.mode, f"managed directory {relative}")
        if item.mtime_ns is not None:
            os.utime(path, ns=(info.st_atime_ns, item.mtime_ns), follow_symlinks=False)


def validate_directory_snapshot_restored(target: Path, snapshot: TransactionSnapshot) -> None:
    for relative, item in snapshot.directories.items():
        path = target if relative == "." else safe_target_path(target, relative)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if item.exists:
                fail("transaction rollback did not restore directory presence")
            continue
        if not item.exists:
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                fail("transaction rollback did not restore directory absence")
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            fail("transaction rollback did not restore directory kind")
        if item.mode is not None and stat.S_IMODE(info.st_mode) != item.mode:
            fail("transaction rollback did not restore directory mode")
        if item.device is not None and info.st_dev != item.device:
            fail("transaction rollback did not restore directory device")
        if item.inode is not None and info.st_ino != item.inode:
            fail("transaction rollback did not restore directory inode")
        if item.mtime_ns is not None and info.st_mtime_ns != item.mtime_ns:
            fail("transaction rollback did not restore directory mtime")


def ensure_preserve_dir(target: Path, preserve_dir: Path | None) -> Path:
    if preserve_dir is not None:
        return preserve_dir
    preserve_dir = target / f".{PRODUCT_NAME}.nddev.tmp.transaction.{os.getpid()}.{time.time_ns()}"
    preserve_dir.mkdir(mode=OWNER_DIRECTORY_MODE)
    fsync_directory(target)
    require_private_directory(preserve_dir, "managed transaction preserve directory")
    return preserve_dir


def preserve_transaction_file(
    target: Path,
    relative: str,
    item: FileSnapshot,
    preserve_dir: Path | None,
) -> Path | None:
    if not item.exists:
        return preserve_dir
    path = safe_target_path(target, relative)
    preserve_dir = ensure_preserve_dir(target, preserve_dir)
    preserved = safe_target_path(preserve_dir, relative)
    ensure_real_parent(preserved, preserve_dir)
    path.rename(preserved)
    fsync_directory(path.parent)
    fsync_directory(preserved.parent)
    item.preserved_path = preserved
    return preserve_dir


def cleanup_transaction_preserve_dir(snapshot: TransactionSnapshot) -> None:
    if snapshot.preserve_dir is None or not lstat_exists(snapshot.preserve_dir):
        return
    safe_rmtree_private_directory_fsynced(
        snapshot.preserve_dir,
        "managed transaction preserve directory",
    )


def capture_transaction_snapshot(
    target: Path, *, target_existed: bool, relatives: list[str]
) -> TransactionSnapshot:
    files: dict[str, FileSnapshot] = {}
    target_mode = None
    if target_existed:
        target_mode = stat.S_IMODE(require_real_directory(target, "target").st_mode)
    all_relatives = unique_relatives([*relatives, STAMP_NAME])
    directories = transaction_directory_snapshot(target, all_relatives)
    if not target_existed:
        directories["."] = DirectorySnapshot(exists=False)
    preserve_dir: Path | None = None
    snapshot = TransactionSnapshot(
        target_existed=target_existed,
        target_mode=target_mode,
        files=files,
        directories=directories,
        preserve_dir=None,
    )
    try:
        for relative in all_relatives:
            path = safe_target_path(target, relative)
            item = snapshot_existing_file(path, relative, include_identity=True)
            files[relative] = item
            preserve_dir = preserve_transaction_file(target, relative, item, preserve_dir)
            snapshot.preserve_dir = preserve_dir
    except BaseException:
        restore_transaction_snapshot(target, snapshot)
        raise
    return snapshot


def target_contains_only_lock_artifacts(target: Path) -> bool:
    if not lstat_exists(target):
        return True
    try:
        entries = sorted(path.name for path in target.iterdir())
    except OSError:
        return False
    if entries != [LOCK_DIR_NAME]:
        return False
    lock_dir = target / LOCK_DIR_NAME
    try:
        require_private_directory(lock_dir, "target lock directory")
        lock_entries = sorted(path.name for path in lock_dir.iterdir())
    except JunieCliSetupError:
        return False
    return lock_entries == [LOCK_FILE_NAME]


def apply_transaction_snapshot_once(target: Path, snapshot: TransactionSnapshot) -> None:
    for relative, item in snapshot.files.items():
        path = safe_target_path(target, relative)
        if not item.exists:
            unlink_path_if_exists(path)
            continue
        if item.data is None or item.mode is None:
            fail("transaction snapshot is invalid")
        if item.preserved_path is not None and lstat_exists(item.preserved_path):
            unlink_path_if_exists(path)
            ensure_real_parent(path, target)
            item.preserved_path.rename(path)
            fsync_directory(path.parent)
            fsync_directory(item.preserved_path.parent)
        else:
            atomic_write(path, item.data, target, mode=item.mode)
    cleanup_transaction_preserve_dir(snapshot)
    prune_empty_managed_dirs(target, list(snapshot.files))
    restore_directory_metadata(target, snapshot)
    if snapshot.target_existed and snapshot.target_mode is not None:
        with contextlib.suppress(FileNotFoundError):
            chmod_private_directory(target, snapshot.target_mode, "target")
    if not snapshot.target_existed:
        with contextlib.suppress(OSError):
            rmdir_path(target)


def validate_transaction_snapshot_restored(target: Path, snapshot: TransactionSnapshot) -> None:
    if snapshot.target_existed:
        if snapshot.target_mode is not None:
            info = require_real_directory(target, "target")
            if stat.S_IMODE(info.st_mode) != snapshot.target_mode:
                fail("transaction rollback did not restore the target mode")
    elif not target_contains_only_lock_artifacts(target):
        fail("transaction rollback did not remove the created target")
    if snapshot.target_existed:
        validate_directory_snapshot_restored(target, snapshot)
    for relative, item in snapshot.files.items():
        path = safe_target_path(target, relative)
        info = require_existing_managed_file(path, relative, max_bytes=MANAGED_MAX_BYTES)
        if not item.exists:
            if info is not None:
                fail("transaction rollback did not restore file absence")
            continue
        if info is None or item.data is None or item.mode is None:
            fail("transaction rollback did not restore file presence")
        if stat.S_IMODE(info.st_mode) != item.mode:
            fail("transaction rollback did not restore file mode")
        if item.device is not None and info.st_dev != item.device:
            fail("transaction rollback did not restore file device")
        if item.inode is not None and info.st_ino != item.inode:
            fail("transaction rollback did not restore file inode")
        if item.mtime_ns is not None and info.st_mtime_ns != item.mtime_ns:
            fail("transaction rollback did not restore file mtime")
        current = read_existing_file(path, max_bytes=MANAGED_MAX_BYTES, label=relative)
        if current != item.data:
            fail("transaction rollback did not restore file bytes")
    if snapshot.preserve_dir is not None and lstat_exists(snapshot.preserve_dir):
        fail("transaction rollback left preserved file residue")


def restore_transaction_snapshot(target: Path, snapshot: TransactionSnapshot) -> None:
    last_error: BaseException | None = None
    for _attempt in range(3):
        try:
            apply_transaction_snapshot_once(target, snapshot)
            validate_transaction_snapshot_restored(target, snapshot)
            return
        except BaseException as exc:
            last_error = exc
    try:
        emergency_restore_transaction_snapshot(target, snapshot)
        validate_transaction_snapshot_restored(target, snapshot)
        return
    except BaseException as exc:
        last_error = exc
    assert last_error is not None
    raise last_error


def apply_snapshot_once(target: Path, snapshot: dict[str, FileSnapshot]) -> None:
    for relative, item in snapshot.items():
        path = safe_target_path(target, relative)
        if not item.exists:
            unlink_path_if_exists(path)
            continue
        if item.data is None or item.mode is None:
            fail("snapshot is invalid")
        atomic_write(path, item.data, target, mode=item.mode)
    prune_empty_managed_dirs(target, list(snapshot))


def restore_snapshot(target: Path, snapshot: dict[str, FileSnapshot]) -> None:
    last_error: BaseException | None = None
    for _attempt in range(3):
        try:
            apply_snapshot_once(target, snapshot)
            validate_snapshot_restored(target, snapshot)
            return
        except BaseException as exc:
            last_error = exc
    try:
        emergency_restore_file_snapshot(target, snapshot)
        validate_snapshot_restored(target, snapshot)
        return
    except BaseException as exc:
        last_error = exc
    assert last_error is not None
    raise last_error


def emergency_restore_file_snapshot(target: Path, snapshot: dict[str, FileSnapshot]) -> None:
    errors: list[BaseException] = []
    for relative, item in snapshot.items():
        try:
            path = safe_target_path(target, relative)
            if (
                item.exists
                and item.preserved_path is not None
                and lstat_exists(item.preserved_path)
            ):
                emergency_unlink_existing(path)
                ensure_real_parent(path, target)
                _ORIGINAL_OS_RENAME(item.preserved_path, path)
                fsync_directory_unpatched(path.parent)
                fsync_directory_unpatched(item.preserved_path.parent)
            else:
                emergency_atomic_restore(path, item, target)
        except BaseException as exc:
            errors.append(exc)
    with contextlib.suppress(OSError):
        prune_empty_managed_dirs(target, list(snapshot))
    try:
        validate_snapshot_restored(target, snapshot)
    except BaseException:
        if errors:
            raise errors[-1]
        raise


def emergency_restore_transaction_snapshot(target: Path, snapshot: TransactionSnapshot) -> None:
    emergency_restore_file_snapshot(target, snapshot.files)
    restore_directory_metadata(target, snapshot)
    cleanup_transaction_preserve_dir(snapshot)
    if snapshot.target_existed and snapshot.target_mode is not None:
        with contextlib.suppress(FileNotFoundError):
            chmod_private_directory(target, snapshot.target_mode, "target")
    elif not snapshot.target_existed:
        with contextlib.suppress(OSError):
            rmdir_path_unpatched(target)


def ensure_backup_pool(pool: Path) -> bool:
    info = stat_existing(pool, "backup pool")
    if info is None:
        try:
            pool.mkdir(mode=OWNER_DIRECTORY_MODE)
        except FileExistsError:
            fail("backup pool appeared during creation")
        fsync_directory(pool.parent)
        require_private_directory(pool, "backup pool")
        return True
    else:
        require_current_owner(info, "backup pool")
        if not stat.S_ISDIR(info.st_mode):
            fail("backup pool must be a directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            fail("backup pool must be private")
        return False


def backup_directory_snapshot(path: Path) -> DirectorySnapshot:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return DirectorySnapshot(exists=False)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return DirectorySnapshot(exists=False)
    return DirectorySnapshot(
        exists=True,
        mode=stat.S_IMODE(info.st_mode),
        mtime_ns=info.st_mtime_ns,
        device=info.st_dev,
        inode=info.st_ino,
    )


def restore_backup_directory_metadata(
    path: Path | None, snapshot: DirectorySnapshot | None
) -> None:
    if path is None or snapshot is None or not snapshot.exists:
        return
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return
    if snapshot.mode is not None and stat.S_IMODE(info.st_mode) != snapshot.mode:
        chmod_private_directory(path, snapshot.mode, str(path))
    if snapshot.mtime_ns is not None:
        os.utime(path, ns=(info.st_atime_ns, snapshot.mtime_ns), follow_symlinks=False)


def choose_backup_slot(pool: Path) -> int:
    ensure_backup_pool(pool)
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


def backup_envelope(target: Path, stamp: dict[str, Any], slot: int) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for relative in unique_relatives([*stamp_managed_relatives(stamp), STAMP_NAME]):
        data = read_existing_file(
            safe_target_path(target, relative), max_bytes=MANAGED_MAX_BYTES, label=relative
        )
        files[relative] = None if data is None else base64.b64encode(data).decode("ascii")
    return {
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


def begin_backup_transaction(target: Path, stamp: dict[str, Any]) -> BackupTransaction:
    pool = backup_pool(target)
    pool_created = ensure_backup_pool(pool)
    pool_directory = None if pool_created else backup_directory_snapshot(pool)
    slot = choose_backup_slot(pool)
    slot_dir = pool / str(slot)
    slot_directory = backup_directory_snapshot(slot_dir)
    previous_envelope = FileSnapshot(exists=False)
    preserved_envelope_path: Path | None = None
    if lstat_exists(slot_dir):
        validate_backup_slot_entries(slot_dir, slot)
        envelope_path = slot_dir / BACKUP_NAME
        envelope_info = require_existing_managed_file(
            envelope_path, BACKUP_NAME, max_bytes=METADATA_MAX_BYTES
        )
        if envelope_info is None:
            fail(f"backup slot {slot} is missing its envelope")
        previous_data = read_existing_file(
            envelope_path, max_bytes=METADATA_MAX_BYTES, label=BACKUP_NAME
        )
        previous_envelope = FileSnapshot(
            exists=True,
            data=previous_data,
            mode=stat.S_IMODE(envelope_info.st_mode),
            device=envelope_info.st_dev,
            inode=envelope_info.st_ino,
            mtime_ns=envelope_info.st_mtime_ns,
        )
        preserved_envelope_path = (
            pool / f".{slot}.{BACKUP_NAME}.nddev-backup.preserved.{os.getpid()}.{time.time_ns()}"
        )
        envelope_path.rename(preserved_envelope_path)
        fsync_directory(slot_dir)
        fsync_directory(pool)
    transaction = BackupTransaction(
        pool=pool,
        pool_created=pool_created,
        pool_directory=pool_directory,
        slot=slot,
        slot_dir=slot_dir,
        slot_directory=slot_directory,
        envelope=canonical_json(backup_envelope(target, stamp, slot)),
        previous_envelope=previous_envelope,
        preserved_envelope_path=preserved_envelope_path,
    )
    return transaction


def commit_backup_transaction(transaction: BackupTransaction) -> None:
    if transaction.slot is None:
        return
    if transaction.pool is None or transaction.slot_dir is None or transaction.envelope is None:
        fail("backup transaction is invalid")
    slot = transaction.slot
    slot_dir = transaction.slot_dir
    try:
        if not lstat_exists(slot_dir):
            slot_dir.mkdir(mode=OWNER_DIRECTORY_MODE)
            fsync_directory(slot_dir.parent)
            transaction.slot_created = True
        require_private_directory(slot_dir, f"backup slot {slot}")
        atomic_write(slot_dir / BACKUP_NAME, transaction.envelope, slot_dir)
        transaction.slot_committed = True
    except BaseException:
        rollback_backup_transaction(transaction)
        raise


def cleanup_backup_transaction(transaction: BackupTransaction) -> None:
    if transaction.preserved_envelope_path is not None and lstat_exists(
        transaction.preserved_envelope_path
    ):
        unlink_path(transaction.preserved_envelope_path)
    transaction.envelope = None
    transaction.previous_envelope = None
    transaction.preserved_envelope_path = None


def rollback_backup_transaction(transaction: BackupTransaction) -> None:
    if transaction.rolled_back:
        return
    pool = transaction.pool
    slot = transaction.slot
    slot_dir = transaction.slot_dir
    previous = transaction.previous_envelope or FileSnapshot(exists=False)
    preserved = transaction.preserved_envelope_path
    if slot_dir is not None and (
        transaction.slot_committed
        or transaction.slot_created
        or preserved is not None
        or previous.exists
    ):
        if previous.exists:
            if previous.data is None or previous.mode is None:
                fail("backup transaction snapshot is invalid")
            require_private_directory(slot_dir, f"backup slot {slot}")
            current_envelope = slot_dir / BACKUP_NAME
            unlink_path_if_exists(current_envelope)
            if preserved is not None and lstat_exists(preserved):
                preserved.rename(current_envelope)
                fsync_directory(slot_dir)
                fsync_directory(preserved.parent)
            else:
                atomic_write(current_envelope, previous.data, slot_dir, mode=previous.mode)
        elif lstat_exists(slot_dir):
            safe_rmtree_private_directory(slot_dir, f"backup slot {slot}")
        restore_backup_directory_metadata(slot_dir, transaction.slot_directory)
        transaction.slot_committed = False
        transaction.slot_created = False
    if transaction.pool_created and pool is not None:
        with contextlib.suppress(OSError):
            rmdir_path(pool)
    elif pool is not None:
        restore_backup_directory_metadata(pool, transaction.pool_directory)
    transaction.rolled_back = True


def create_backup(target: Path, stamp: dict[str, Any]) -> int:
    transaction = begin_backup_transaction(target, stamp)
    try:
        commit_backup_transaction(transaction)
        validate_backup_transaction_postcondition(target, transaction)
        cleanup_backup_transaction(transaction)
    except BaseException:
        rollback_backup_transaction(transaction)
        raise
    if transaction.slot is None:
        fail("backup transaction did not choose a slot")
    return transaction.slot


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
    require_backup_optional_string(envelope.get("source_profile_id"), "backup source_profile_id")
    require_backup_optional_string(
        envelope.get("source_legacy_setup_id"), "backup source_legacy_setup_id"
    )
    require_backup_int(envelope.get("created_at"), "backup created_at")
    if not isinstance(envelope.get("files"), dict):
        fail("backup files are invalid")


def validate_backup_slot_entries(slot_dir: Path, slot: int) -> None:
    require_private_directory(slot_dir, f"backup slot {slot}")
    names = sorted(path.name for path in slot_dir.iterdir())
    if names != [BACKUP_NAME]:
        fail("backup slot contains entries outside the envelope")


def validate_backup_payload(
    target: Path,
    slot: int,
    envelope: dict[str, Any],
    *,
    require_current_software: bool,
) -> BackupRestorePlan:
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
    if require_current_software:
        if stamp["software"] == {"state": "absent"}:
            if software_state(target).get("state") != "absent":
                fail("backup software does not match current target runtime")
        else:
            try:
                current_software = current_software_metadata(target)
            except JunieCliSetupError as exc:
                fail(f"backup target software is not restorable: {exc}")
            if current_software != stamp["software"]:
                fail("backup software does not match current target runtime")
    return BackupRestorePlan(files=decoded, stamp=stamp, relatives=expected_relatives)


def build_backup_restore_plan(
    target: Path, slot: int, envelope: dict[str, Any]
) -> BackupRestorePlan:
    return validate_backup_payload(target, slot, envelope, require_current_software=True)


def validate_backup_transaction_postcondition(target: Path, transaction: BackupTransaction) -> None:
    if transaction.slot is None:
        return
    if transaction.slot_dir is None:
        fail("backup transaction is invalid")
    validate_backup_slot_entries(transaction.slot_dir, transaction.slot)
    envelope_path = transaction.slot_dir / BACKUP_NAME
    envelope = read_json_file(envelope_path, max_bytes=METADATA_MAX_BYTES, label=BACKUP_NAME)
    validate_backup_payload(
        target,
        transaction.slot,
        envelope,
        require_current_software=False,
    )


def validate_restored_backup_state(target: Path, expected_stamp: dict[str, Any]) -> dict[str, Any]:
    restored_stamp = read_stamp(target)
    if restored_stamp != expected_stamp:
        fail("restored stamp does not match the backup stamp")
    drift = drift_for_stamp(target, restored_stamp)
    if drift:
        fail(f"restored target has drift: {', '.join(drift)}")
    return restored_stamp


def validate_snapshot_restored(target: Path, snapshot: dict[str, FileSnapshot]) -> None:
    for relative, expected in snapshot.items():
        path = safe_target_path(target, relative)
        info = require_existing_managed_file(path, relative, max_bytes=MANAGED_MAX_BYTES)
        if not expected.exists:
            if info is not None:
                fail("restore rollback did not restore file absence")
            continue
        if info is None or expected.data is None or expected.mode is None:
            fail("restore rollback did not restore file presence")
        if stat.S_IMODE(info.st_mode) != expected.mode:
            fail("restore rollback did not restore file mode")
        if expected.device is not None and info.st_dev != expected.device:
            fail("restore rollback did not restore file device")
        if expected.inode is not None and info.st_ino != expected.inode:
            fail("restore rollback did not restore file inode")
        if expected.mtime_ns is not None and info.st_mtime_ns != expected.mtime_ns:
            fail("restore rollback did not restore file mtime")
        current = read_existing_file(path, max_bytes=MANAGED_MAX_BYTES, label=relative)
        if current != expected.data:
            fail("restore rollback did not restore file bytes")


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


def setup_result_payload(
    target: Path,
    setup_id: str,
    profile_id: str,
    changed: list[str],
    backup_slot: int | None,
    software_changed: bool,
    software: dict[str, Any],
) -> dict[str, Any]:
    software_payload = {"state": software.get("state", "installed")}
    for key in ("version", "platform"):
        if key in software:
            software_payload[key] = software[key]
    return {
        "setup_id": setup_id,
        "profile_id": profile_id,
        "changed": changed,
        "backup_slot": backup_slot,
        "software_changed": software_changed,
        "software": software_payload,
        "target": str(validate_target(target, create=False)),
    }


def validate_desired_setup_postcondition(
    target: Path,
    desired_stamp: dict[str, Any],
    backup_tx: BackupTransaction,
) -> None:
    current = read_stamp(target)
    if current != desired_stamp:
        fail("setup postcondition did not publish the desired stamp")
    drift = drift_for_stamp(target, desired_stamp)
    if drift:
        fail(f"setup postcondition detected drift: {', '.join(drift)}")
    validate_backup_transaction_postcondition(target, backup_tx)


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


def setup_stamp_software_state(target: Path, current: dict[str, Any] | None) -> dict[str, Any]:
    if current is not None:
        software = current.get("software")
        if not isinstance(software, dict):
            fail("stamp software is invalid")
        return software
    current_state = software_state(target)
    if current_state.get("state") != "absent":
        fail("target-owned Junie software must be installed with install-cli after setup install")
    return {"state": "absent"}


def write_setup_locked(
    target: Path,
    setup: dict[str, Any],
    profile: dict[str, Any],
    *,
    require_existing: bool = False,
    allow_legacy_migration: bool = False,
    directory_transaction: DirectoryTransaction | None = None,
) -> dict[str, Any]:
    target = validate_target(target, create=True, transaction=directory_transaction)
    created_paths = [] if directory_transaction is None else directory_transaction.created
    target_existed = target not in created_paths
    current = read_stamp(target)
    if require_existing and current is None:
        fail("operation requires an already managed target")
    current_drift: list[str] = []
    if current is not None:
        if current["schema_version"] != STAMP_SCHEMA and not allow_legacy_migration:
            fail("legacy managed target must be migrated before this operation")
        if allow_legacy_migration:
            validate_legacy_migration_clean(target, current)
        current_drift = drift_for_stamp(target, current)
        if current_drift:
            fail(f"managed target has drift: {', '.join(current_drift)}")
    files = desired_files(target, setup, profile)
    previous_relatives = [] if current is None else stamp_managed_relatives(current)
    transaction_relatives = unique_relatives([*previous_relatives, *files])
    if current is None:
        assert_no_unmanaged_conflicts(target, transaction_relatives)
    changed = planned_changed_relatives(target, current, files)
    if (
        current is not None
        and not allow_legacy_migration
        and current["schema_version"] == STAMP_SCHEMA
        and current["setup_id"] == setup["id"]
        and current.get("profile_id") == profile["id"]
        and not changed
        and not current_drift
    ):
        software = current.get("software")
        if not isinstance(software, dict):
            fail("stamp software is invalid")
        return setup_result_payload(
            target,
            setup["id"],
            profile["id"],
            changed,
            None,
            False,
            software,
        )
    backup_slot = None
    backup_tx = BackupTransaction()
    if current is not None and (
        current["schema_version"] != STAMP_SCHEMA
        or current["setup_id"] != setup["id"]
        or current.get("profile_id") != profile["id"]
    ):
        backup_tx = begin_backup_transaction(target, current)
        backup_slot = backup_tx.slot
    snapshot: TransactionSnapshot | None = None
    try:
        snapshot = capture_transaction_snapshot(
            target, target_existed=target_existed, relatives=transaction_relatives
        )
        for relative, data in files.items():
            atomic_write(safe_target_path(target, relative), data, target)
        for relative in previous_relatives:
            if relative not in files:
                unlink_path_if_exists(safe_target_path(target, relative))
        prune_empty_managed_dirs(target, transaction_relatives)
        software = setup_stamp_software_state(target, current)
        desired_stamp = build_stamp(target, setup["id"], profile["id"], files, software)
        atomic_write(stamp_path(target), canonical_json(desired_stamp), target)
        commit_backup_transaction(backup_tx)
        validate_desired_setup_postcondition(target, desired_stamp, backup_tx)
        cleanup_backup_transaction(backup_tx)
        cleanup_transaction_preserve_dir(snapshot)
    except BaseException:
        rollback_backup_transaction(backup_tx)
        prune_empty_runtime_dirs(target)
        if snapshot is not None:
            restore_transaction_snapshot(target, snapshot)
        raise
    return setup_result_payload(
        target,
        setup["id"],
        profile["id"],
        changed,
        backup_slot,
        False,
        setup_stamp_software_state(target, read_stamp(target)),
    )


def write_setup(
    target: Path,
    setup: dict[str, Any],
    profile: dict[str, Any],
    *,
    require_existing: bool = False,
) -> dict[str, Any]:
    with target_lock(target, create_parent=True) as directory_transaction:
        return write_setup_locked(
            target,
            setup,
            profile,
            require_existing=require_existing,
            directory_transaction=directory_transaction,
        )


def update_setup(target: Path, setup_id: str | None, profile_id: str | None) -> dict[str, Any]:
    if setup_id is not None or profile_id is not None:
        fail("update preserves the installed setup/profile identity; use switch to change profile")
    with target_lock(target, create_parent=False) as directory_transaction:
        target = validate_target(target, create=False)
        if not lstat_exists(target):
            fail("update requires an already managed target")
        current = read_stamp(target)
        if current is None:
            fail("update requires an already managed target")
        if is_legacy_stamp(current):
            fail("legacy managed target must be migrated before update")
        setup = load_setup(current["setup_id"])
        profile = load_profile(current["profile_id"])
        return write_setup_locked(
            target,
            setup,
            profile,
            require_existing=True,
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
            fail(
                "legacy setup has no safe profile mapping; pass --profile safe or --profile full-auto"
            )
        profile = load_profile(selected_profile)
        result = write_setup_locked(
            target,
            load_setup(selected_setup),
            profile,
            require_existing=True,
            allow_legacy_migration=True,
            directory_transaction=directory_transaction,
        )
        result["migrated"] = True
        result["legacy_setup_id"] = current["setup_id"]
        return result


def require_managed_stamp_for_software(target: Path, command: str) -> dict[str, Any]:
    if not lstat_exists(target):
        fail(f"{command} requires an already managed target")
    current = read_stamp(target)
    if current is None:
        fail(f"{command} requires an already managed target")
    if is_legacy_stamp(current):
        fail(f"{command} requires a migrated schema 3 target")
    drift = drift_for_stamp(target, current)
    unexpected = [item for item in drift if item != "software"]
    if unexpected:
        fail(f"managed target has drift: {', '.join(unexpected)}")
    return current


def software_status_payload(target: Path) -> dict[str, Any]:
    canonical = validate_target(target, create=False)
    return {
        "target": str(canonical),
        "software": software_state(canonical) if lstat_exists(canonical) else {"state": "absent"},
    }


def software_result_payload(
    target: Path,
    command: str,
    changed: bool,
    software: dict[str, Any],
) -> dict[str, Any]:
    return {
        "command": command,
        "software_changed": changed,
        "software": software_state(target)
        if software.get("state") == "absent"
        else {
            "state": "installed",
            "version": software["version"],
            "platform": software["platform"],
        },
        "target": str(validate_target(target, create=False)),
    }


def write_stamp_software_state(
    target: Path, current: dict[str, Any], software: dict[str, Any]
) -> dict[str, Any]:
    updated = dict(current)
    updated["software"] = software
    atomic_write(stamp_path(target), canonical_json(updated), target)
    restored = read_stamp(target)
    if restored != updated:
        fail("software stamp postcondition did not publish the desired state")
    drift = drift_for_stamp(target, restored)
    if drift:
        fail(f"software stamp postcondition detected drift: {', '.join(drift)}")
    return updated


def install_or_update_cli(target: Path, *, repair: bool, command: str) -> dict[str, Any]:
    with target_lock(target, create_parent=False) as directory_transaction:
        target = validate_target(target, create=False, transaction=directory_transaction)
        current = require_managed_stamp_for_software(target, command)
        if not repair and "software" in drift_for_stamp(target, current):
            fail(
                "install-cli requires absent target-owned software; use update-cli to repair drift"
            )
        snapshot: TransactionSnapshot | None = None
        software_tx: SoftwareTransaction | None = None
        try:
            snapshot = capture_transaction_snapshot(target, target_existed=True, relatives=[])
            software_tx = begin_software_transaction(target, repair=repair)
            desired_stamp = write_stamp_software_state(target, current, software_tx.metadata)
            commit_software_transaction(software_tx)
            cleanup_transaction_preserve_dir(snapshot)
        except BaseException:
            if software_tx is not None:
                rollback_software_transaction(target, software_tx)
            if snapshot is not None:
                restore_transaction_snapshot(target, snapshot)
            raise
        return software_result_payload(
            target, command, software_tx.changed, desired_stamp["software"]
        )


def remove_cli(target: Path) -> dict[str, Any]:
    with target_lock(target, create_parent=False, allow_missing=True):
        target = validate_target(target, create=False)
        if not lstat_exists(target):
            return {
                "command": "remove-cli",
                "software_changed": False,
                "software": {"state": "absent"},
                "target": str(target),
            }
        current = read_stamp(target)
        if current is not None:
            unexpected = [item for item in drift_for_stamp(target, current) if item != "software"]
            if unexpected:
                fail(f"managed target has drift: {', '.join(unexpected)}")
        snapshot: TransactionSnapshot | None = None
        runtime_tx: RuntimeRemoveTransaction | None = None
        try:
            if current is not None:
                snapshot = capture_transaction_snapshot(target, target_existed=True, relatives=[])
            runtime_tx = begin_runtime_remove(target)
            if current is not None:
                write_stamp_software_state(target, current, {"state": "absent"})
                cleanup_transaction_preserve_dir(snapshot)
        except BaseException:
            if runtime_tx is not None:
                rollback_runtime_remove(target, runtime_tx)
            if snapshot is not None:
                restore_transaction_snapshot(target, snapshot)
            raise
        if runtime_tx is not None:
            commit_runtime_remove(runtime_tx)
        return software_result_payload(
            target,
            "remove-cli",
            runtime_tx.changed if runtime_tx is not None else False,
            {"state": "absent"},
        )


def restore_backup(target: Path, slot: int) -> dict[str, Any]:
    if slot < 0 or slot > 9:
        fail("backup slot must be between 0 and 9")
    with target_lock(target, create_parent=True) as directory_transaction:
        target = validate_target(target, create=True, transaction=directory_transaction)
        require_private_directory(backup_pool(target), "backup pool")
        validate_backup_slot_entries(backup_pool(target) / str(slot), slot)
        envelope_path = backup_pool(target) / str(slot) / BACKUP_NAME
        envelope = read_json_file(envelope_path, max_bytes=METADATA_MAX_BYTES, label=BACKUP_NAME)
        plan = build_backup_restore_plan(target, slot, envelope)
        current_stamp = read_stamp(target)
        current_relatives = [] if current_stamp is None else stamp_managed_relatives(current_stamp)
        restore_relatives = unique_relatives([*current_relatives, *plan.relatives])
        snapshot = capture_transaction_snapshot(
            target,
            target_existed=True,
            relatives=restore_relatives,
        )
        try:
            for relative in current_relatives:
                if relative not in plan.files:
                    unlink_path_if_exists(safe_target_path(target, relative))
            for relative in plan.relatives:
                atomic_write(safe_target_path(target, relative), plan.files[relative], target)
            prune_empty_managed_dirs(target, restore_relatives)
            restored_stamp = validate_restored_backup_state(target, plan.stamp)
            cleanup_transaction_preserve_dir(snapshot)
        except BaseException:
            restore_transaction_snapshot(target, snapshot)
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
    with target_lock(target, create_parent=False, allow_missing=True):
        target = validate_target(target, create=False)
        if not lstat_exists(target):
            return {
                "removed_setup_id": None,
                "removed_profile_id": None,
                "removed_legacy_setup_id": None,
                "target": str(target),
            }
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
        snapshot = capture_transaction_snapshot(
            target,
            target_existed=True,
            relatives=managed_relatives,
        )
        try:
            legacy = stamp.get("schema_version") == LEGACY_STAMP_SCHEMA
            for relative in managed_relatives:
                if legacy and relative in LEGACY_MANAGED_JSON_KEYS:
                    remove_managed_json_keys(target, relative)
                elif legacy and relative in MERGED_MARKER_PATHS:
                    remove_managed_block_from_target(target, relative)
                else:
                    unlink_path_if_exists(safe_target_path(target, relative))
            unlink_path_if_exists(stamp_path(target))
            prune_empty_managed_dirs(target, managed_relatives)
            cleanup_transaction_preserve_dir(snapshot)
        except BaseException:
            restore_transaction_snapshot(target, snapshot)
            raise
        return {
            "removed_setup_id": removed_setup_id,
            "removed_profile_id": removed_profile_id,
            "removed_legacy_setup_id": removed_legacy_setup_id,
            "software_removed": False,
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
        unlink_path(path)


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
        unlink_path(path)


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
            rmdir_path(directory)


def plan_payload_locked(
    target: Path, setup: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any]:
    status = status_payload(target)
    current = read_stamp(target) if status["managed"] else None
    files = desired_files(target, setup, profile)
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
        "changed": planned_changed_relatives(target, current, files),
        "software": status["software"],
        "mutates": False,
    }


def plan_payload(target: Path, setup: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    with target_lock(target, create_parent=False, allow_missing=True):
        return plan_payload_locked(target, setup, profile)


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
    if not isinstance(metadata.get("shim"), dict) or not isinstance(metadata.get("binary"), dict):
        fail("Junie software metadata is invalid")
    shim_relative = metadata["shim"].get("path")
    binary_relative = metadata["binary"].get("path")
    if not isinstance(shim_relative, str) or not isinstance(binary_relative, str):
        fail("Junie software metadata path is invalid")
    shim_path = safe_target_path(canonical, shim_relative)
    binary_path = safe_target_path(canonical, binary_relative)
    ensure_private_directory(data / "logs", canonical, "runtime logs")
    ensure_private_directory(home / ".config", canonical, "runtime config home")
    ensure_private_directory(home / ".cache", canonical, "runtime XDG cache home")
    ensure_private_directory(home / ".local" / "state", canonical, "runtime XDG state home")
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
    command = [str(runtime_launch_image_bin(canonical))]
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
    launcher = materialize_launch_image(canonical, shim)
    return LaunchPlan(
        command=command,
        child_env=child_env,
        cwd=canonical,
        shim=shim,
        binary=binary,
        launcher=launcher,
    )


def launch(target: Path, child_args: list[str]) -> int:
    override = child_args_use_target_scope_overrides(child_args)
    if override is not None:
        fail(f"{override} is managed by the target launch environment")
    current_platform_id()
    with target_lock(target, create_parent=False):
        plan = build_launch_plan_locked(target, child_args)
        live_before = snapshot_live_junie_home()
        with protected_launch_handoff(plan):
            revalidate_launch_artifacts(plan)
            try:
                process = subprocess.Popen(
                    plan.command,
                    cwd=plan.cwd,
                    env=plan.child_env,
                )
            except FileNotFoundError:
                fail("target-owned Junie command was not found")
            try:
                return_code = process.wait()
            finally:
                require_live_junie_home_unchanged(live_before, context="Junie launch")
            return int(return_code)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = JunieCliArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=JunieCliArgumentParser,
    )
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
    for name in ("software-status", "install-cli", "update-cli", "remove-cli"):
        command = subparsers.add_parser(name)
        command.add_argument("--target", required=True)
        command.add_argument("--json", action="store_true")
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
    require_supported_host()
    if args.command == "status":
        target = require_absolute_target(args.target)
        with target_lock(target, create_parent=False, allow_missing=True):
            emit(status_payload(target), as_json=args.json)
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
        emit(update_setup(target, None, None), as_json=args.json)
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
    if args.command == "software-status":
        target = require_absolute_target(args.target)
        with target_lock(target, create_parent=False, allow_missing=True):
            emit(software_status_payload(target), as_json=args.json)
        return 0
    if args.command == "install-cli":
        target = require_absolute_target(args.target)
        emit(install_or_update_cli(target, repair=False, command=args.command), as_json=args.json)
        return 0
    if args.command == "update-cli":
        target = require_absolute_target(args.target)
        emit(install_or_update_cli(target, repair=True, command=args.command), as_json=args.json)
        return 0
    if args.command == "remove-cli":
        target = require_absolute_target(args.target)
        emit(remove_cli(target), as_json=args.json)
        return 0
    if args.command == "launch":
        child_args = list(args.child_args)
        if child_args[:1] == ["--"]:
            child_args = child_args[1:]
        return launch(require_absolute_target(args.target), child_args)
    fail(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    raw_args = sys.argv[1:] if argv is None else list(argv)
    wants_json = "--json" in raw_args
    try:
        args = parse_args(raw_args)
        return dispatch(args)
    except JunieCliArgumentError as exc:
        if wants_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except JunieCliSetupError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
