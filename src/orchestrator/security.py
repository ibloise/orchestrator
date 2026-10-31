"""Linux x86-64 offline sandbox. Also the standalone trusted exec trampoline.

No network, child processes, host IPC, credentials or shared Git metadata are exposed.
The live Codex gate intentionally remains closed: provider authentication/network need
a separately reviewed boundary. No configuration flag can downgrade this profile.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import resource
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

PROFILE = 'landlock-seccomp-offline-v1'
PYTHON = str(Path(sys.executable).resolve())
LIVE_BLOCKER = ('Live Codex is disabled: this profile denies networking and child processes; '
                'provider authentication isolation and an approved live smoke test are outstanding')


class SecurityError(ValueError):
    pass


def clean_environment(workspace: Path | None = None) -> dict[str, str]:
    return {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
            'HOME': str(workspace or '/nonexistent'), 'TMPDIR': str(workspace or '/nonexistent'),
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TERMINAL_PROMPT': '0', 'PYTHONDONTWRITEBYTECODE': '1'}


def sandbox_command(workspace: Path, command: list[str]) -> list[str]:
    return [PYTHON, '-I', str(Path(__file__).resolve()), str(workspace), *command]


def _check(result: int, operation: str) -> int:
    if result < 0:
        raise SecurityError(f'{operation}: {os.strerror(ctypes.get_errno())}')
    return result


def restrict(workspace: Path) -> None:
    if platform.system() != 'Linux' or platform.machine() != 'x86_64' or os.getuid() == 0:
        raise SecurityError('Sandbox requires non-root Linux x86_64')
    libc = ctypes.CDLL(None, use_errno=True)
    abi = _check(libc.syscall(444, 0, 0, 1), 'Landlock ABI')
    if abi < 6:
        raise SecurityError('Landlock ABI >= 6 required; no fallback')
    _check(libc.prctl(38, 1, 0, 0, 0), 'no_new_privs')
    class CapHeader(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int32)]

    class CapData(ctypes.Structure):
        _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]

    header = CapHeader(0x20080522, 0)
    capabilities = (CapData * 2)()
    _check(libc.syscall(126, ctypes.byref(header), ctypes.byref(capabilities)), 'drop capabilities')
    # Disable dumpability before loading any untrusted code.
    _check(libc.prctl(4, 0, 0, 0, 0), 'PR_SET_DUMPABLE')

    class Ruleset(ctypes.Structure):
        _fields_ = [('fs', ctypes.c_uint64), ('net', ctypes.c_uint64), ('scoped', ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _layout_ = 'ms'
        _pack_ = 1
        _fields_ = [('allowed', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]

    # ABI 5 filesystem rights including TRUNCATE/IOCTL_DEV; ABI 6 signal/abstract IPC scope.
    all_fs = (1 << 16) - 1
    read = (1 << 0) | (1 << 2) | (1 << 3)
    # No device nodes, sockets or FIFOs in the writable tree.
    write = all_fs & ~((1 << 6) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 15))
    attr = Ruleset(all_fs, 3, 3)
    ruleset = _check(libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0), 'Landlock ruleset')
    try:
        paths = {Path('/usr').resolve(): read, workspace.resolve(): write}
        for path in ('/bin', '/lib', '/lib64'):
            if Path(path).exists():
                paths[Path(path).resolve()] = read
        for path in ('/etc/ld.so.cache', '/dev/null', '/dev/urandom'):
            if Path(path).exists():
                paths[Path(path)] = (1 << 2) | ((1 << 1) if path == '/dev/null' else 0)
        for path, allowed in paths.items():
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(allowed, fd)
                _check(libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0), 'Landlock path rule')
            finally:
                os.close(fd)
        _check(libc.syscall(446, ruleset, 0), 'Landlock enforce')
    finally:
        os.close(ruleset)

    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]

    class Program(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ushort), ('filters', ctypes.POINTER(Filter))]

    # Audit architecture check prevents int80/x32 bypass of the syscall-number policy.
    instructions = [Filter(0x20, 0, 0, 4), Filter(0x15, 1, 0, 0xC000003E),
                    Filter(0x06, 0, 0, 0x80000000), Filter(0x20, 0, 0, 0),
                    Filter(0x35, 0, 1, 0x40000000), Filter(0x06, 0, 0, 0x80000000),
                    Filter(0x35, 0, 1, 472), Filter(0x06, 0, 0, 0x00050000 | errno.EPERM)]
    fd_operations = [Filter(0x20, 0, 0, 24)]  # fcntl cmd (argument 1)
    for operation in (0, 1, 2, 3, 4, 1030):
        fd_operations.extend([Filter(0x15, 0, 1, operation), Filter(0x06, 0, 0, 0x7FFF0000)])
    fd_operations.append(Filter(0x06, 0, 0, 0x00050000 | errno.EPERM))
    instructions.extend([Filter(0x15, 0, len(fd_operations), 72), *fd_operations])
    # Deny process creation/escape, sockets, tracing, cross-process memory/fds,
    # kernel administration and io_uring. EPERM is observable in security probes.
    denied = {16, 29, 30, 31, 41, 42, 43, 49, 50, 53, 56, 57, 58, 62,
              64, 65, 66, 67, 68, 69, 70, 71, 90, 91, 92, 93, 94,
              101, 103, 109, 112, 126, 129, 132, 141, 142, 144, 155,
              161, 163, 165, 166, 167, 168, 169, 172, 175, 176, 179, 200, 234, 240, 241, 242, 243, 244, 245, 246, 248,
              188, 189, 190, 191, 192, 193, 194, 195, 196, 197, 198, 199,
              203, 235, 249, 250, 251, 254, 260, 261, 268, 272, 274, 280, 297, 298, 300, 302, 303, 304, 308, 310, 311, 312, 313, 314, 317,
              320, 321, 323, 424, 425, 426, 427, 428, 429, 430, 431, 432, 433,
              434, 435, 438, 440, 442, 443, 448, 452, 460, 463, 464, 465, 466, 467, 469}
    for number in sorted(denied):
        instructions.extend([Filter(0x15, 0, 1, number), Filter(0x06, 0, 0, 0x00050000 | errno.EPERM)])
    instructions.append(Filter(0x06, 0, 0, 0x7FFF0000))
    filters = (Filter * len(instructions))(*instructions)
    program = Program(len(instructions), filters)
    _check(libc.prctl(22, 2, ctypes.byref(program), 0, 0), 'seccomp enforce')


def probe(workspace: Path) -> dict:
    """Negative sentinel probes in the exact profile used by provider processes."""
    link = workspace / f'.security-probe-{uuid4().hex}'
    try:
        with tempfile.TemporaryDirectory(prefix='protected-probe-', dir=workspace.parent) as protected:
            sentinel = Path(protected) / 'sentinel'
            sentinel.write_text('synthetic-control-plane-secret')
            original_mode = sentinel.stat().st_mode
            link.symlink_to(sentinel)
            script = '''
import ctypes, errno, json, os, pathlib, socket, sys
answers = {}
for value in sys.argv[1:]:
    path = pathlib.Path(value)
    for action in ("read", "write", "chmod"):
        try:
            if action == "read": path.read_bytes()
            elif action == "write": path.write_text("modified")
            else: path.chmod(0o777)
            answers[str(path)+":"+action] = False
        except OSError as exc:
            answers[str(path)+":"+action] = exc.errno in (errno.EACCES, errno.EPERM)
try:
    socket.socket()
    answers["network"] = False
except OSError as exc:
    answers["network"] = exc.errno == errno.EPERM
libc = ctypes.CDLL(None, use_errno=True)
for name, number in (("unshare",272),("host_signal",62),("fork",57)):
    answers[name] = libc.syscall(number,0,0,0,0,0) == -1 and ctypes.get_errno() == errno.EPERM
print(json.dumps(answers))
'''
            result = subprocess.run(sandbox_command(workspace, [PYTHON, '-I', '-c', script, str(sentinel), str(link)]),
                                    input='ORCHESTRATOR_LAUNCH\n', env=clean_environment(workspace),
                                    capture_output=True, text=True, timeout=10, check=False)
            import json
            try:
                observations = json.loads(result.stdout)
            except ValueError:
                observations = {}
            intact = sentinel.read_text() == 'synthetic-control-plane-secret' and sentinel.stat().st_mode == original_mode
            available = result.returncode == 0 and bool(observations) and all(observations.values()) and intact
            return {'profile': PROFILE, 'available': available, 'observations': observations,
                    'sentinel_intact': intact, 'diagnostic': result.stderr.strip(),
                    'live_enabled': False, 'live_blocker': LIVE_BLOCKER}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'profile': PROFILE, 'available': False, 'diagnostic': str(exc),
                'live_enabled': False, 'live_blocker': LIVE_BLOCKER}
    finally:
        if link.is_symlink():
            link.unlink()


def main() -> int:
    try:
        workspace = Path(sys.argv[1]).resolve(strict=True)
        # No untrusted program starts until the parent has persisted/revalidated launch.
        token = b''
        while len(token) < 20 and not token.endswith(b'\n'):
            chunk = os.read(0, 1)
            if not chunk:
                break
            token += chunk
        if token != b'ORCHESTRATOR_LAUNCH\n':
            raise SecurityError('Missing trusted launch token')
        os.chdir(workspace)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
        restrict(workspace)
        try:
            os.execv(sys.argv[2], sys.argv[2:])
        except OSError as exc:
            raise SecurityError(f'execv {sys.argv[2]}: errno {exc.errno} ({exc.strerror})') from exc
    except (OSError, SecurityError, ValueError) as exc:
        print(f'SANDBOX_REFUSED: {exc}', file=sys.stderr)
        return 126


if __name__ == '__main__':
    raise SystemExit(main())
