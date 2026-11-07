"""Publish generated files with destination permissions and repair selected exports.

Windows hard links and renames retain the source security descriptor. A private
TemporaryDirectory can therefore leak its restrictive DACL into a public export.
Fresh exclusive creation inherits the destination directory's DACL instead. No
permissions on the live rollout, staging input, or destination parent are changed.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from .progress import ProgressReporter

CHUNK_BYTES = 4 * 1024 * 1024
ProgressCallback = Callable[[int, int], None]


def hardlink_publication_allowed() -> bool:
    """A Windows hard link cannot acquire its destination directory's DACL."""
    return os.name != 'nt'


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, 'st_file_attributes', 0) &
                getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _digest(stream, *, progress: Callable[[int], None] | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(CHUNK_BYTES), b''):
        digest.update(block)
        size += len(block)
        if progress is not None:
            progress(size)
    return digest.hexdigest(), size


def publish_file(source: Path, destination: Path, *,
                 progress: ProgressCallback | None = None,
                 reporter: ProgressReporter | None = None,
                 stage_label: str = 'Publish archive') -> str:
    """Publish a regular staged file without overwriting an existing path.

    POSIX permits the existing inexpensive hard-link strategy. Windows always
    creates a new file in the destination directory, inheriting that directory's
    access rules rather than copying source permissions or metadata. A copied
    file is flushed, compared with the original digest, and independently read
    back before success. The callback receives copied bytes and the fixed total.
    A reporter shows separately measured checksum, copy, and readback stages.
    The source must be a generated staging file, never an app-owned live source.
    """
    source, destination = Path(source), Path(destination)
    initial = source.lstat()
    if not stat.S_ISREG(initial.st_mode) or _is_reparse(initial):
        raise ValueError('Publication requires a regular staging file, not a link')

    def stage(label: str, total: int | None, unit: str = 'bytes'):
        return (reporter.stage(f'{stage_label}: {label}', total=total, unit=unit)
                if reporter is not None else nullcontext(None))

    if hardlink_publication_allowed():
        with stage('link', None, 'files') as task:
            try:
                os.link(source, destination)
                if task is not None:
                    task.update(1, total=1)
                return 'hardlink'
            except FileExistsError:
                raise
            except OSError:
                if task is not None:
                    task.update(detail='Filesystem requires a verified copy')
    owned: tuple[int, int] | None = None
    try:
        with source.open('rb') as original:
            before = os.fstat(original.fileno())
            # Windows pathname stat can expose creation time as st_ctime while
            # fstat exposes ChangeTime. Compare timestamps within the same API;
            # only device/file identity is compared across the two observations.
            if (not os.path.samestat(before, initial) or
                    _signature(source.lstat()) != _signature(initial)):
                raise RuntimeError('Staged file changed before publication; no cleanup performed')
            with stage('source checksum', before.st_size) as task:
                expected = _digest(original, progress=task.update if task else None)
                if expected[1] != before.st_size:
                    raise RuntimeError('Staged file changed before publication; no cleanup performed')
            original.seek(0)
            with stage('copy', before.st_size) as task:
                with destination.open('xb') as output:
                    created = os.fstat(output.fileno())
                    owned = created.st_dev, created.st_ino
                    digest = hashlib.sha256()
                    copied = 0
                    if progress is not None:
                        progress(0, before.st_size)
                    while copied < before.st_size:
                        block = original.read(min(CHUNK_BYTES, before.st_size - copied))
                        if not block:
                            raise RuntimeError('Staged file was truncated during publication; no cleanup performed')
                        output.write(block)
                        digest.update(block)
                        copied += len(block)
                        if progress is not None:
                            progress(copied, before.st_size)
                        if task is not None:
                            task.update(copied)
                    output.flush()
                    os.fsync(output.fileno())
                if ((digest.hexdigest(), copied) != expected or
                        _signature(os.fstat(original.fileno())) != _signature(before) or
                        _signature(source.lstat()) != _signature(initial)):
                    raise RuntimeError('Staged file changed during publication; no cleanup performed')
        with stage('verify', before.st_size) as task:
            current = destination.lstat()
            if (current.st_dev, current.st_ino) != owned or _is_reparse(current):
                raise RuntimeError('Published file was replaced during verification; no cleanup performed')
            with destination.open('rb') as check:
                if _digest(check, progress=task.update if task else None) != expected:
                    raise RuntimeError('Published copy verification failed; no cleanup performed')
        return 'verified_copy'
    except BaseException:
        # A collision or a concurrent replacement must never delete another file.
        if owned is not None:
            try:
                current = destination.lstat()
                if ((current.st_dev, current.st_ino) == owned and
                        stat.S_ISREG(current.st_mode) and not _is_reparse(current)):
                    destination.unlink()
            except OSError:
                pass
        raise


def _windows_security(path: Path) -> dict:
    """Read owner and DACL without requesting SACL privileges or changing rights."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(pointer), ctypes.POINTER(pointer),
        ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer),
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        pointer, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorControl.argtypes = [
        pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
    advapi.GetAce.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    owner, dacl, descriptor = pointer(), pointer(), pointer()
    code = advapi.GetNamedSecurityInfoW(str(path), 1, 0x5,
                                       ctypes.byref(owner), None, ctypes.byref(dacl),
                                       None, ctypes.byref(descriptor))
    if code:
        raise ctypes.WinError(code)
    sddl, owner_text = wintypes.LPWSTR(), wintypes.LPWSTR()
    try:
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, 0x5, ctypes.byref(sddl), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.ConvertSidToStringSidW(owner, ctypes.byref(owner_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control),
                                                  ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        # ACL's fixed header is BYTE revision/reserved, WORD size/count/reserved.
        count = int.from_bytes(ctypes.string_at(dacl, 8)[4:6], 'little') if dacl else 0
        inherited = 0
        for index in range(count):
            ace = pointer()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            inherited += bool(ctypes.string_at(ace, 2)[1] & 0x10)
        return {
            'owner_sid': owner_text.value,
            'sddl': sddl.value,
            'dacl_protected': bool(control.value & 0x1000),
            'null_dacl': not bool(dacl),
            'inherited_aces': inherited,
            'explicit_aces': count - inherited,
        }
    finally:
        for allocated in (sddl, owner_text, descriptor):
            if allocated:
                kernel.LocalFree(ctypes.cast(allocated, pointer))


def diagnose_access(path: Path) -> dict:
    """Inspect actual read access and, on Windows, owner and parent/file DACLs."""
    path = Path(os.path.abspath(Path(path).expanduser()))
    result: dict = {'path': str(path), 'platform': os.name, 'readable': False}
    try:
        info = path.lstat()
        result.update(size_bytes=info.st_size, hardlink_count=info.st_nlink,
                      is_reparse_point=_is_reparse(info),
                      kind='directory' if stat.S_ISDIR(info.st_mode) else 'file')
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            result['error'] = 'Refusing to follow a symbolic link or reparse point'
            return result
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as children:
                next(children, None)
        elif stat.S_ISREG(info.st_mode):
            with path.open('rb') as stream:
                stream.read(1)
        else:
            raise ValueError('Expected a regular file or directory')
        result['readable'] = True
    except (OSError, ValueError) as exc:
        result['error'] = str(exc)
    if os.name == 'nt':
        for key, selected in (('security', path), ('parent_security', path.parent)):
            try:
                result[key] = _windows_security(selected)
            except OSError as exc:
                result[key + '_error'] = str(exc)
    return result


def _validate_repair_path(path: Path) -> os.stat_result:
    # abspath deliberately does not resolve links. Reject them before touching ACLs.
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise ValueError(f'Access repair refuses links and reparse points: {component}')
    info = path.lstat()
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise ValueError(f'Access repair refuses multiply linked files: {path}')
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError(f'Access repair requires a regular export file or review directory: {path}')
    if any(part.lower() == '.codex' for part in path.parts):
        raise ValueError('Access repair never modifies app-owned Codex paths')
    return info


def _repair_plan(paths: Iterable[Path]) -> list[Path]:
    """Validate the entire requested set before allowing any ACL modification."""
    planned: list[Path] = []
    seen: set[str] = set()
    review_member = re.compile(r'(?:part-\d{4,}\.txt|START_HERE\.txt|RECENT\.txt|'
                               r'review-index\.json|original-export\.manifest\.json)')
    for supplied in paths:
        path = Path(os.path.abspath(Path(supplied).expanduser()))
        if '*' in str(path) or '?' in str(path).removeprefix('\\\\?\\'):
            raise ValueError('Access repair requires explicit paths, not wildcards')
        info = _validate_repair_path(path)
        name = path.name.lower()
        if not name.startswith('codex_'):
            raise ValueError(f'Access repair only accepts named codex_ export artifacts: {path}')
        if stat.S_ISDIR(info.st_mode):
            if not name.endswith('.review'):
                raise ValueError('Only a specifically named .review export directory can be repaired')
            children = sorted(path.iterdir())
            if not children or not {'START_HERE.txt', 'review-index.json'}.issubset(
                    {child.name for child in children}):
                raise ValueError(f'Directory is not a recognized review export: {path}')
            for child in children:
                child_info = _validate_repair_path(child)
                if not stat.S_ISREG(child_info.st_mode) or not review_member.fullmatch(child.name):
                    raise ValueError(f'Unexpected review export entry; nothing repaired: {child}')
            candidates = [path, *children]
        else:
            if not (name.endswith(('.zip', '.md', '.jsonl', '.manifest.json'))):
                raise ValueError(f'Unrecognized export file type: {path}')
            candidates = [path]
        for candidate in candidates:
            key = os.path.normcase(str(candidate))
            if key not in seen:
                seen.add(key)
                planned.append(candidate)
    if not planned:
        raise ValueError('Specify at least one export artifact to repair')
    return planned


def _icacls_executable() -> str:
    """Use the Windows system executable, never a PATH or current-directory shim."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return str(Path(buffer.value) / 'icacls.exe')


def repair_export_access(paths: Iterable[Path]) -> list[dict]:
    """Reset only explicit generated artifacts to their parent-inherited DACL.

    This opt-in operation never changes ownership, grants Everyone access, edits
    a parent directory, follows links, or invokes recursive icacls. A recognized
    old .review directory's flat, validated members are repaired individually.
    Existing rights must permit WRITE_DAC; failures are returned with diagnostics
    rather than invoking elevation, taking ownership, or changing broader ACLs.
    """
    if os.name != 'nt':
        raise OSError('Windows access repair is only available on Windows')
    planned = _repair_plan(paths)
    executable = _icacls_executable()
    results: list[dict] = []
    for path in planned:
        _validate_repair_path(path)
        before = diagnose_access(path)
        # /L is an additional safeguard against following a replaced link.
        completed = subprocess.run([executable, str(path), '/reset', '/L', '/Q'],
                                   capture_output=True, text=True, errors='replace',
                                   check=False)
        after = diagnose_access(path)
        security = after.get('security', {})
        repaired = (completed.returncode == 0 and after['readable'] and
                    security.get('dacl_protected') is False and
                    security.get('explicit_aces') == 0)
        results.append({
            'path': str(path), 'repaired': repaired,
            'before': before, 'after': after,
            'command_exit_code': completed.returncode,
            'command_output': (completed.stdout + completed.stderr).strip(),
        })
    return results


def validate_export_paths(outputs: dict[str, Path],
                          protected_sources: Iterable[Path] = ()) -> None:
    """Reject output/source aliases before writing any generated file.

    Resolve relative paths, symlinks and junctions, then compare filesystem
    identities for existing files so differently named hard links also collide.
    This is a read-only preflight, not a transaction against hostile path changes.
    Missing output files are permitted and never created by this check.
    """
    def identity(value: Path) -> tuple[Path, os.stat_result | None]:
        path = Path(value).expanduser()
        try:
            resolved = path.resolve()
            try:
                info = resolved.stat()
            except FileNotFoundError:
                info = None
        except (OSError, RuntimeError) as exc:
            raise ValueError(f'Cannot verify export path identity: {path}: {exc}') from exc
        return resolved, info

    def same_file(left: tuple[Path, os.stat_result | None],
                  right: tuple[Path, os.stat_result | None]) -> bool:
        return (left[0] == right[0] or
                (left[1] is not None and right[1] is not None and
                 os.path.samestat(left[1], right[1])))

    protected = [identity(source) for source in protected_sources]
    selected: list[tuple[str, tuple[Path, os.stat_result | None]]] = []
    for label, supplied in outputs.items():
        current = identity(supplied)
        for original in protected:
            if same_file(current, original):
                raise ValueError(f'Export {label} path aliases a source file: {supplied}; '
                                 'choose a different output path')
        for previous_label, previous in selected:
            if same_file(current, previous):
                raise ValueError(f'Export {label} and {previous_label} paths refer to the same file: '
                                 f'{supplied}; choose distinct output paths')
        selected.append((label, current))
