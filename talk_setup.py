"""Interactive, confirmation-gated setup for ``hermes talk``.

The wizard is deliberately separate from :mod:`talk_doctor`: doctor only
observes, while this module owns every prompt and persistent mutation.  The
workflow is always detect -> ask only unresolved decisions -> confirm each
setting -> commit the confirmed set as one transaction -> rerun doctor -> verify.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import re
import stat
import tempfile
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    from . import talk_config, talk_doctor
except ImportError:  # pragma: no cover - flat-module fallback (pip -e install)
    import talk_config
    import talk_doctor

InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]
EnvChange = tuple[str, str | None, bool]
_ENV_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=).*$"
)
_SAFE_DOTENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:+@-]+$")
_YES = frozenset({"y", "yes"})


@dataclass(frozen=True)
class ApplyReceipt:
    """Secret-free result of one confirmed setup transaction."""

    state: Literal["applied", "rolled-back", "failed"]
    settings: tuple[str, ...]
    failed_at: str | None = None
    error_type: str | None = None
    mutation_survived: bool = False
    cleanup_errors: tuple[str, ...] = ()


class SetupApplyError(RuntimeError):
    """Compatibility error for a failed direct :func:`apply_env_write` call."""

    def __init__(self, receipt: ApplyReceipt):
        super().__init__(
            f"dotenv transaction {receipt.state} at {receipt.failed_at or 'unknown'} "
            f"({receipt.error_type or 'unknown error'})"
        )
        self.receipt = receipt


def _checks(report: dict) -> dict[str, dict]:
    return {check["id"]: check for check in report.get("checks", [])}


def _ask_choice(prompt: str, choices: tuple[str, ...], input_fn: InputFn) -> str:
    allowed = {choice.lower(): choice for choice in choices}
    while True:
        answer = input_fn(f"{prompt} [{'/'.join(choices)}]: ").strip().lower()
        if answer in allowed:
            return allowed[answer]


def _ask_nonempty_secret(prompt: str, secret_input_fn: InputFn) -> str:
    while True:
        value = secret_input_fn(prompt).strip()
        if value:
            return value


def _dotenv_value(value: str) -> str:
    if _SAFE_DOTENV_VALUE_RE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _env_name_key(name: str, windows_env_names: bool | None) -> str:
    case_insensitive = os.name == "nt" if windows_env_names is None else windows_env_names
    return name.upper() if case_insensitive else name


def _collapse_changes(
    changes: Sequence[EnvChange], windows_env_names: bool | None
) -> list[EnvChange]:
    """Keep one deterministic final write for each platform-equivalent name."""

    collapsed: list[EnvChange] = []
    positions: dict[str, int] = {}
    for change in changes:
        normalized = _env_name_key(change[0], windows_env_names)
        if normalized in positions:
            collapsed[positions[normalized]] = change
        else:
            positions[normalized] = len(collapsed)
            collapsed.append(change)
    return collapsed


def _render_dotenv(
    original: str,
    changes: Sequence[EnvChange],
    windows_env_names: bool | None,
) -> str:
    selected = {
        _env_name_key(key, windows_env_names): (key, value)
        for key, value, _secret in changes
    }
    order = [_env_name_key(key, windows_env_names) for key, _value, _secret in changes]
    lines = original.splitlines()
    updated: list[str] = []
    emitted: set[str] = set()
    for line in lines:
        match = _ENV_ASSIGNMENT_RE.match(line)
        if match:
            normalized = _env_name_key(match.group("key"), windows_env_names)
            replacement = selected.get(normalized)
            if replacement is not None:
                replacement_key, replacement_value = replacement
                if replacement_value is not None and normalized not in emitted:
                    updated.append(f"{replacement_key}={_dotenv_value(replacement_value)}")
                    emitted.add(normalized)
                continue
        updated.append(line)

    for normalized in order:
        key, value = selected[normalized]
        if value is not None and normalized not in emitted:
            updated.append(f"{key}={_dotenv_value(value)}")
            emitted.add(normalized)

    body = "\n".join(updated)
    if body:
        body += "\n"
    return body


def _windows_security_api():
    """Return typed native security functions without a third-party dependency."""

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    get_named_security = advapi32.GetNamedSecurityInfoW
    get_named_security.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_named_security.restype = wintypes.DWORD

    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL

    descriptor_to_sddl = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    descriptor_to_sddl.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    ]
    descriptor_to_sddl.restype = wintypes.BOOL

    sddl_to_descriptor = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    sddl_to_descriptor.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    sddl_to_descriptor.restype = wintypes.BOOL

    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_dacl.restype = wintypes.BOOL

    get_control = advapi32.GetSecurityDescriptorControl
    get_control.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_control.restype = wintypes.BOOL

    set_named_security = advapi32.SetNamedSecurityInfoW
    set_named_security.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    set_named_security.restype = wintypes.DWORD

    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    return (
        ctypes,
        wintypes,
        get_named_security,
        convert_sid,
        descriptor_to_sddl,
        sddl_to_descriptor,
        get_dacl,
        get_control,
        set_named_security,
        local_free,
    )


def _windows_current_user_sid() -> str:
    """Return the SID of the user token running this process."""

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_token_information.restype = wintypes.BOOL
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    token = wintypes.HANDLE()
    if not open_process_token(get_current_process(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        get_token_information(token, 1, None, 0, ctypes.byref(size))
        error = ctypes.get_last_error()
        if error != 122:  # ERROR_INSUFFICIENT_BUFFER is the size probe result.
            raise ctypes.WinError(error)
        buffer = ctypes.create_string_buffer(size.value)
        if not get_token_information(
            token,
            1,
            buffer,
            size,
            ctypes.byref(size),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        (
            _ctypes,
            _wintypes,
            _get_named_security,
            convert_sid,
            _descriptor_to_sddl,
            _sddl_to_descriptor,
            _get_dacl,
            _get_control,
            _set_named_security,
            local_free,
        ) = _windows_security_api()
        sid_text = wintypes.LPWSTR()
        try:
            if not convert_sid(token_user.user.sid, ctypes.byref(sid_text)):
                raise ctypes.WinError(ctypes.get_last_error())
            return sid_text.value
        finally:
            if sid_text:
                local_free(sid_text)
    finally:
        close_handle(token)


def _windows_owner_sid(path: Path) -> str:
    (
        ctypes,
        wintypes,
        get_named_security,
        convert_sid,
        _descriptor_to_sddl,
        _sddl_to_descriptor,
        _get_dacl,
        _get_control,
        _set_named_security,
        local_free,
    ) = _windows_security_api()
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    error = get_named_security(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000001,  # OWNER_SECURITY_INFORMATION
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    sid_text = wintypes.LPWSTR()
    try:
        if not convert_sid(owner, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        return sid_text.value
    finally:
        if sid_text:
            local_free(sid_text)
        if descriptor:
            local_free(descriptor)


def _windows_dacl_sddl(path: Path) -> str:
    (
        ctypes,
        wintypes,
        get_named_security,
        _convert_sid,
        descriptor_to_sddl,
        _sddl_to_descriptor,
        _get_dacl,
        _get_control,
        _set_named_security,
        local_free,
    ) = _windows_security_api()
    descriptor = ctypes.c_void_p()
    error = get_named_security(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000004,  # DACL_SECURITY_INFORMATION
        None,
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    sddl = wintypes.LPWSTR()
    try:
        if not descriptor_to_sddl(
            descriptor,
            1,  # SDDL_REVISION_1
            0x00000004,
            ctypes.byref(sddl),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return sddl.value
    finally:
        if sddl:
            local_free(sddl)
        if descriptor:
            local_free(descriptor)


def _windows_apply_dacl_sddl(path: Path, sddl: str) -> None:
    (
        ctypes,
        wintypes,
        _get_named_security,
        _convert_sid,
        _descriptor_to_sddl,
        sddl_to_descriptor,
        get_dacl,
        get_control,
        set_named_security,
        local_free,
    ) = _windows_security_api()
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    dacl_present = wintypes.BOOL()
    dacl_defaulted = wintypes.BOOL()
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not sddl_to_descriptor(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not get_dacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not dacl_present.value or not dacl:
            raise PermissionError("refusing to apply an absent or null secret-file DACL")
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        protection = 0x80000000 if control.value & 0x1000 else 0x20000000
        error = set_named_security(
            str(path),
            1,  # SE_FILE_OBJECT
            0x00000004 | protection,
            None,
            None,
            dacl,
            None,
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        if descriptor:
            local_free(descriptor)


def _windows_dacl_equivalent(left: str, right: str) -> bool:
    """Compare DACL entries/protection while ignoring bookkeeping-only AI/AR flags."""

    def normalize(sddl: str) -> str:
        flags, separator, aces = sddl.partition("(")
        flags = flags.replace("AI", "").replace("AR", "")
        return flags + (separator + aces if separator else "")

    return normalize(left) == normalize(right)


def _windows_restrict_owner_only_dacl(path: Path) -> None:
    user_sid = _windows_current_user_sid()
    desired = f"D:P(A;;FA;;;{user_sid})"
    _windows_apply_dacl_sddl(path, desired)
    actual = _windows_dacl_sddl(path)
    if not (
        actual.startswith("D:P")
        and actual.count("(") == 1
        and f"(A;;FA;;;{user_sid})" in actual
    ):
        raise PermissionError("secret-file DACL verification failed")


def _cleanup_staged_paths(paths: Sequence[Path]) -> tuple[tuple[str, ...], bool]:
    """Delete and independently verify every transaction temp by redacted index."""

    failures: list[str] = []
    survived = False
    for index, path in enumerate(paths):
        delete_error: str | None = None
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - cleanup must still verify all paths
            delete_error = type(exc).__name__
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 - unverifiable means not rolled back
            survived = True
            error_type = delete_error or type(exc).__name__
        else:
            survived = True
            error_type = delete_error or "CleanupVerificationError"
        failures.append(
            f"staged-temp[{index}]-beside-destination:{error_type}"
        )
    return tuple(failures), survived


def _ensure_private_parent(parent: Path) -> tuple[Path, ...]:
    missing: list[Path] = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    try:
        parent.mkdir(mode=stat.S_IRWXU, parents=True, exist_ok=True)
        for created in reversed(missing):
            created.chmod(stat.S_IRWXU)
    except Exception:
        _cleanup_created_directories(missing)
        raise
    return tuple(missing)


def _cleanup_created_directories(paths: Sequence[Path]) -> bool:
    for path in paths:
        with contextlib.suppress(OSError):
            path.rmdir()
    return any(path.exists() for path in paths)


def _stage_env_file(
    env_path: Path,
    payload: bytes,
    mode: int,
    *,
    staged_paths: list[Path],
) -> Path:
    """Create, secure, flush, and return a unique sibling transaction file."""

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{env_path.name}.hermes-talk-",
        suffix=".tmp",
        dir=str(env_path.parent),
    )
    temporary = Path(temporary_name)
    staged_paths.append(temporary)
    try:
        if os.name == "nt":
            # GitHub-hosted Windows runners reject SetNamedSecurityInfoW while
            # the CRT descriptor returned by mkstemp remains open. Close the
            # still-empty file first, harden and verify its DACL, then reopen
            # it for the secret payload. No secret bytes exist before the ACL
            # is private.
            os.close(fd)
            fd = -1
            _windows_restrict_owner_only_dacl(temporary)
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0),
            )
        elif hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:  # pragma: no cover - supported targets expose fchmod
            os.chmod(temporary, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except Exception:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise


def _replace_env_file(temporary: Path, env_path: Path, *, destination_existed: bool) -> None:
    """Atomically replace the dotenv file while preserving a Windows DACL."""

    if os.name != "nt" or not destination_existed:
        os.replace(temporary, env_path)
        return

    # ReplaceFileW carries the replaced file's ACL and other protected metadata
    # to the replacement. Unlike a plain MoveFileEx/os.replace, a merge failure
    # fails closed rather than silently widening an existing secret file.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    replace_file.restype = wintypes.BOOL
    if not replace_file(str(env_path), str(temporary), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _environment_snapshot(
    environ: MutableMapping[str, str],
    changes: Sequence[EnvChange],
    windows_env_names: bool | None,
) -> dict[str, tuple[tuple[str, str], ...]]:
    snapshots: dict[str, tuple[tuple[str, str], ...]] = {}
    items = list(environ.items())
    for key, _value, _secret in changes:
        normalized = _env_name_key(key, windows_env_names)
        snapshots[normalized] = tuple(
            (present_key, present_value)
            for present_key, present_value in items
            if _env_name_key(present_key, windows_env_names) == normalized
        )
    return snapshots


def _remove_environment_variants(
    environ: MutableMapping[str, str],
    normalized: str,
    windows_env_names: bool | None,
) -> None:
    for present_key in list(environ):
        if _env_name_key(present_key, windows_env_names) == normalized:
            del environ[present_key]


def _restore_environment(
    environ: MutableMapping[str, str],
    snapshots: dict[str, tuple[tuple[str, str], ...]],
    windows_env_names: bool | None,
) -> tuple[str | None, str | None]:
    failed_at = None
    error_type = None
    for normalized, prior_items in snapshots.items():
        try:
            _remove_environment_variants(environ, normalized, windows_env_names)
            for prior_key, prior_value in prior_items:
                environ[prior_key] = prior_value
        except Exception as exc:  # noqa: BLE001 - rollback must continue for other keys
            if failed_at is None:
                failed_at = f"rollback:environment:{normalized}"
                error_type = type(exc).__name__
    return failed_at, error_type


def _environment_matches_snapshot(
    environ: MutableMapping[str, str],
    snapshots: dict[str, tuple[tuple[str, str], ...]],
    windows_env_names: bool | None,
) -> bool:
    try:
        probes = [
            (items[0][0] if items else normalized, None, False)
            for normalized, items in snapshots.items()
        ]
        current = _environment_snapshot(
            environ,
            probes,
            windows_env_names,
        )
    except Exception:  # noqa: BLE001 - unreadable state cannot be claimed rolled back
        return False
    return current == snapshots


def _file_matches_snapshot(
    env_path: Path,
    existed: bool,
    payload: bytes,
    windows_dacl: str | None = None,
) -> bool:
    try:
        if not existed:
            return not env_path.exists()
        if env_path.read_bytes() != payload:
            return False
        return not (
            os.name == "nt"
            and windows_dacl is not None
            and not _windows_dacl_equivalent(
                _windows_dacl_sddl(env_path), windows_dacl
            )
        )
    except OSError:
        return False


def _restore_file_snapshot(
    env_path: Path,
    *,
    existed: bool,
    payload: bytes,
    mode: int,
    windows_dacl: str | None,
    staged_paths: list[Path],
) -> tuple[str | None, str | None]:
    if _file_matches_snapshot(env_path, existed, payload, windows_dacl):
        return None, None
    try:
        if not existed:
            env_path.unlink(missing_ok=True)
            return None, None
        rollback_temp = _stage_env_file(
            env_path,
            payload,
            mode,
            staged_paths=staged_paths,
        )
        _replace_env_file(rollback_temp, env_path, destination_existed=env_path.exists())
        if (
            os.name == "nt"
            and windows_dacl is not None
            and not _windows_dacl_equivalent(
                _windows_dacl_sddl(env_path), windows_dacl
            )
        ):
            _windows_apply_dacl_sddl(env_path, windows_dacl)
            if not _windows_dacl_equivalent(
                _windows_dacl_sddl(env_path), windows_dacl
            ):
                raise PermissionError("destination DACL rollback verification failed")
        return None, None
    except Exception as exc:  # noqa: BLE001 - the receipt must admit failed rollback
        return "rollback:file", type(exc).__name__


def apply_env_transaction(
    env_path: Path,
    changes: Sequence[EnvChange],
    *,
    environ: MutableMapping[str, str] | None = None,
    windows_env_names: bool | None = None,
) -> ApplyReceipt:
    """Apply all confirmed dotenv/process changes atomically or roll them back."""

    selected = _collapse_changes(changes, windows_env_names)
    settings = tuple(key for key, _value, _secret in selected)
    if not selected:
        return ApplyReceipt(state="applied", settings=())

    process_environment = os.environ if environ is None else environ
    temporary: Path | None = None
    created_directories: tuple[Path, ...] = ()
    snapshots: dict[str, tuple[tuple[str, str], ...]] = {}
    environment_started = False
    original_observed = False
    original_existed = False
    original_payload = b""
    original_mode = stat.S_IRUSR | stat.S_IWUSR
    original_windows_dacl: str | None = None
    staged_paths: list[Path] = []
    failed_at = "read"
    try:
        original_existed = env_path.exists()
        if original_existed:
            original_payload = env_path.read_bytes()
            original_mode = stat.S_IMODE(env_path.stat().st_mode)
            if os.name == "nt":
                failed_at = "read-security"
                original_windows_dacl = _windows_dacl_sddl(env_path)
        original_text = original_payload.decode("utf-8")
        original_observed = True
        updated = _render_dotenv(original_text, selected, windows_env_names).encode("utf-8")

        failed_at = "environment-snapshot"
        snapshots = _environment_snapshot(process_environment, selected, windows_env_names)
        failed_at = "parent"
        created_directories = _ensure_private_parent(env_path.parent)
        failed_at = "stage"
        temporary = _stage_env_file(
            env_path,
            updated,
            original_mode,
            staged_paths=staged_paths,
        )

        for key, value, _secret in selected:
            failed_at = f"environment:{key}"
            environment_started = True
            normalized = _env_name_key(key, windows_env_names)
            _remove_environment_variants(process_environment, normalized, windows_env_names)
            if value is not None:
                process_environment[key] = value

        failed_at = "commit"
        _replace_env_file(temporary, env_path, destination_existed=original_existed)
        temporary = None
        failed_at = "commit-security"
        if os.name == "nt":
            if original_windows_dacl is None:
                _windows_restrict_owner_only_dacl(env_path)
            elif not _windows_dacl_equivalent(
                _windows_dacl_sddl(env_path), original_windows_dacl
            ):
                raise PermissionError("existing destination DACL changed")
        cleanup_errors, staged_survived = _cleanup_staged_paths(staged_paths)
        if staged_survived:
            raise PermissionError("staged cleanup verification failed")
        return ApplyReceipt(state="applied", settings=settings)
    except Exception as exc:  # noqa: BLE001 - setup returns a redacted failure receipt
        _cleanup_staged_paths(staged_paths)

        rollback_at = None
        rollback_error = None
        if environment_started:
            rollback_at, rollback_error = _restore_environment(
                process_environment, snapshots, windows_env_names
            )

        file_rollback_at = None
        file_rollback_error = None
        if original_observed:
            file_rollback_at, file_rollback_error = _restore_file_snapshot(
                env_path,
                existed=original_existed,
                payload=original_payload,
                mode=original_mode,
                windows_dacl=original_windows_dacl,
                staged_paths=staged_paths,
            )
        cleanup_errors, staged_survived = _cleanup_staged_paths(staged_paths)
        directories_survived = _cleanup_created_directories(created_directories)
        environment_survived = environment_started and not _environment_matches_snapshot(
            process_environment, snapshots, windows_env_names
        )
        file_survived = original_observed and not _file_matches_snapshot(
            env_path,
            original_existed,
            original_payload,
            original_windows_dacl,
        )
        mutation_survived = (
            environment_survived
            or file_survived
            or directories_survived
            or staged_survived
        )
        rollback_failure = rollback_at or file_rollback_at
        return ApplyReceipt(
            state="failed" if mutation_survived else "rolled-back",
            settings=settings,
            failed_at=rollback_failure or failed_at,
            error_type=rollback_error or file_rollback_error or type(exc).__name__,
            mutation_survived=mutation_survived,
            cleanup_errors=cleanup_errors,
        )


def apply_env_write(
    env_path: Path,
    key: str,
    value: str | None,
    *,
    windows_env_names: bool | None = None,
) -> None:
    """Compatibility wrapper for one atomic, file-only dotenv mutation."""

    receipt = apply_env_transaction(
        env_path,
        [(key, value, False)],
        environ={},
        windows_env_names=windows_env_names,
    )
    if receipt.state != "applied":
        raise SetupApplyError(receipt)


def _auth_changes(
    check: dict,
    *,
    input_fn: InputFn,
    secret_input_fn: InputFn,
    output_fn: OutputFn,
) -> list[tuple[str, str | None, bool]]:
    details = check.get("details", {})
    status = check.get("status")
    preference = details.get("preference")
    oauth_state = details.get("codex_oauth")
    blocked_by = details.get("blocked_by")
    metered_present = bool(details.get("metered_key_present"))

    if status != "fail" and not (
        details.get("metered_key_wins_over_codex") and preference == "absent"
    ):
        return []

    if blocked_by in {"blank-talk-key", "blank-openai-key"}:
        key = "TALK_OPENAI_API_KEY" if blocked_by == "blank-talk-key" else "OPENAI_API_KEY"
        action = _ask_choice(
            f"{key} is blank. Replace it or remove its dotenv entry?",
            ("replace", "remove"),
            input_fn,
        )
        if action == "remove":
            return [(key, None, True)]
        secret = _ask_nonempty_secret(f"Enter {key} (input hidden): ", secret_input_fn)
        return [(key, secret, True)]

    if blocked_by == "invalid-preference":
        lane = _ask_choice(
            "Choose the Talk auth policy: require Codex OAuth or preserve key-first precedence?",
            ("oauth", "key"),
            input_fn,
        )
        if lane == "oauth":
            return [("TALK_PREFER_CODEX_OAUTH", "true", False)]
        changes: list[EnvChange] = []
        if not metered_present:
            secret = _ask_nonempty_secret(
                "Enter TALK_OPENAI_API_KEY (input hidden): ", secret_input_fn
            )
            changes.append(("TALK_OPENAI_API_KEY", secret, True))
        changes.append(("TALK_PREFER_CODEX_OAUTH", "false", False))
        return changes

    if status == "fail":
        lane = _ask_choice(
            "No usable Talk authentication was detected. Configure Codex OAuth or an API key?",
            ("codex", "key"),
            input_fn,
        )
        if lane == "codex":
            output_fn("Run `codex login`, then rerun `hermes talk setup`; no file was changed.")
            return []
        changes: list[EnvChange] = []
        if not metered_present:
            secret = _ask_nonempty_secret(
                "Enter TALK_OPENAI_API_KEY (input hidden): ", secret_input_fn
            )
            changes.append(("TALK_OPENAI_API_KEY", secret, True))
        if preference == "enabled":
            # The operator chose metered auth while the fail-closed OAuth
            # policy was active. The key and the policy transition remain two
            # independently visible/confirmed settings, then commit together.
            changes.append(("TALK_PREFER_CODEX_OAUTH", "false", False))
        return changes

    # Both lanes exist under the historical key-first behavior.  Asking here
    # resolves the billing decision without changing the absent-knob default
    # when the operator intentionally keeps key-first precedence.
    if metered_present and oauth_state in {"valid", "expired"}:
        lane = _ask_choice(
            "Both a metered key and Codex OAuth are available. Which lane should Talk require?",
            ("oauth", "key"),
            input_fn,
        )
        if lane == "oauth":
            return [("TALK_PREFER_CODEX_OAUTH", "true", False)]
    return []


def _model_changes(check: dict, input_fn: InputFn) -> list[tuple[str, str | None, bool]]:
    if check.get("status") == "pass":
        return []
    details = check.get("details", {})
    if check.get("status") == "warn" and details.get("compatibility") == "unknown":
        choice = _ask_choice(
            "The configured model has unknown duplex/tool compatibility. "
            "Keep it or use the default?",
            ("keep", "default"),
            input_fn,
        )
        return [] if choice == "keep" else [("TALK_MODEL", talk_config.DEFAULT_TALK_MODEL, False)]

    while True:
        model = input_fn(
            f"Choose a known duplex/tool-compatible model [{talk_config.DEFAULT_TALK_MODEL}]: "
        ).strip() or talk_config.DEFAULT_TALK_MODEL
        if talk_config.realtime_model_valid(model):
            return [("TALK_MODEL", model, False)]


def _voice_changes(check: dict, input_fn: InputFn) -> list[tuple[str, str | None, bool]]:
    if check.get("status") != "fail":
        return []
    allowed = tuple(talk_config.OPENAI_REALTIME_VOICES)
    while True:
        voice = input_fn(
            f"Choose a Realtime voice [{talk_config.DEFAULT_TALK_VOICE}]: "
        ).strip().lower() or talk_config.DEFAULT_TALK_VOICE
        if voice in allowed:
            return [("TALK_VOICE", voice, False)]


def cli_entry(
    *,
    input_fn: InputFn = input,
    secret_input_fn: InputFn | None = None,
    output_fn: OutputFn = print,
) -> int:
    """Run the confirmation-gated setup workflow and return verification status."""

    secret_reader = getpass.getpass if secret_input_fn is None else secret_input_fn
    env_path = talk_config.get_hermes_home() / ".env"
    output_fn("Hermes Talk setup: detecting current state (doctor remains read-only).")
    initial = talk_doctor.collect_report()
    checks = _checks(initial)

    changes: list[tuple[str, str | None, bool]] = []
    if "auth" in checks:
        changes.extend(
            _auth_changes(
                checks["auth"],
                input_fn=input_fn,
                secret_input_fn=secret_reader,
                output_fn=output_fn,
            )
        )
    if "model" in checks:
        changes.extend(_model_changes(checks["model"], input_fn))
    if "voice" in checks:
        changes.extend(_voice_changes(checks["voice"], input_fn))

    confirmed_changes: list[EnvChange] = []
    for key, value, secret in changes:
        shown = "<redacted>" if secret and value is not None else (value or "<remove>")
        confirmed = input_fn(f"Write {key}={shown} to {env_path}? [yes/no]: ").strip().lower()
        if confirmed not in _YES:
            output_fn(f"Skipped {key}; no write was made for that setting.")
            continue
        confirmed_changes.append((key, value, secret))

    receipt = (
        apply_env_transaction(env_path, confirmed_changes) if confirmed_changes else None
    )

    # No output or other fallible work is placed between a surviving mutation
    # and this call: verification is always attempted after the transaction.
    try:
        verified = talk_doctor.collect_report()
        rendered = talk_doctor.render_human(verified)
    except Exception as exc:  # noqa: BLE001 - keep setup failures inside the CLI boundary
        if receipt is not None:
            output_fn(_render_apply_receipt(receipt))
        output_fn(f"Verification: FAIL (doctor error={type(exc).__name__})")
        return 1

    if receipt is not None:
        output_fn(_render_apply_receipt(receipt))
    output_fn("Reran read-only doctor for verification.")
    output_fn(rendered)
    passed = bool(verified.get("ok")) and (receipt is None or receipt.state == "applied")
    output_fn(f"Verification: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def _render_apply_receipt(receipt: ApplyReceipt) -> str:
    fields = [
        f"state={receipt.state}",
        f"settings={','.join(receipt.settings) or 'none'}",
    ]
    if receipt.failed_at is not None:
        fields.append(f"failed_at={receipt.failed_at}")
    if receipt.error_type is not None:
        fields.append(f"error={receipt.error_type}")
    if receipt.cleanup_errors:
        fields.append(f"cleanup={','.join(receipt.cleanup_errors)}")
    fields.append(f"mutation_survived={'true' if receipt.mutation_survived else 'false'}")
    return "Setup receipt: " + "; ".join(fields)


__all__ = ["ApplyReceipt", "apply_env_transaction", "apply_env_write", "cli_entry"]
