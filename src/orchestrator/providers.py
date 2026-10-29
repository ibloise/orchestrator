"""Narrow adapters: observed subprocess results are distinct from agent declarations."""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .security import (
    LIVE_BLOCKER,
    PYTHON,
    SecurityError,
    clean_environment,
    sandbox_command,
)


@dataclass(frozen=True)
class ExecutionRequest:
    execution_id: str
    task_id: str
    task_revision_id: str
    plan_revision_id: str
    authorization_id: str
    project_id: str
    repository: str
    role: str
    role_version: str
    model_profile: str
    provider: str
    model_id: str
    instructions: str
    acceptance_criteria: tuple[str, ...]
    workspace_id: str
    base_commit: str
    head_commit: str
    sandbox_profile: str
    deadline: str
    input_files: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ProviderResult:
    exit_code: int
    reason: str
    declaration_status: str
    declaration: dict | None
    signal: int | None = None


def process_identity(pid: int) -> str | None:
    try:
        # comm can contain spaces and parentheses; starttime is field 22.
        text = Path(f'/proc/{pid}/stat').read_text()
        fields = text[text.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + fields[19]
    except (OSError, IndexError):
        return None


def supervise(command: list[str], request: ExecutionRequest, workspace: Path, artifacts: Path,
              cancelled: Callable[[], bool], started: Callable[[int, str], None],
              timeout: float, grace: float, max_log_bytes: int) -> ProviderResult:
    """No shell, inherited credentials or unbounded logs. The sandbox denies forks."""
    declaration = None
    reason = 'exited'
    total = 0
    captured = bytearray()
    process = subprocess.Popen(sandbox_command(workspace, command), cwd=workspace,
                               env=clean_environment(workspace), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, close_fds=True)
    started_at = time.monotonic()
    stop_at = None
    identity = process_identity(process.pid)

    def send(sig: signal.Signals) -> None:
        if identity is not None and process_identity(process.pid) == identity:
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass

    try:
        if identity is None:
            raise SecurityError('Cannot establish subprocess identity')
        started(process.pid, identity)
        # Nonblocking input keeps deadlines/cancellation effective even if a child
        # stops consuming its request. Nothing is interpolated through a shell.
        pending = memoryview(b'ORCHESTRATOR_LAUNCH\n' + request.instructions.encode())
        os.set_blocking(process.stdin.fileno(), False)
        with selectors.DefaultSelector() as selector, (artifacts / 'stdout.log').open('xb') as stdout, (artifacts / 'stderr.log').open('xb') as stderr:
            os.fchmod(stdout.fileno(), 0o600)
            os.fchmod(stderr.fileno(), 0o600)
            selector.register(process.stdin, selectors.EVENT_WRITE, None)
            selector.register(process.stdout, selectors.EVENT_READ, stdout)
            selector.register(process.stderr, selectors.EVENT_READ, stderr)
            while selector.get_map() or process.poll() is None:
                elapsed = time.monotonic() - started_at
                if stop_at is None:
                    if cancelled():
                        reason = 'cancelled'
                    elif elapsed >= timeout:
                        reason = 'timed_out'
                    if reason != 'exited':
                        send(signal.SIGTERM)
                        stop_at = time.monotonic()
                if stop_at is not None and time.monotonic() - stop_at >= grace:
                    send(signal.SIGKILL)
                for key, _ in selector.select(timeout=0.02):
                    if key.fileobj is process.stdin:
                        try:
                            pending = pending[os.write(process.stdin.fileno(), pending):]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = max(0, max_log_bytes - total)
                    key.data.write(chunk[:remaining])
                    key.data.flush()
                    if key.fileobj is process.stdout:
                        captured.extend(chunk[:remaining])
                    total += len(chunk)
                    if total > max_log_bytes and stop_at is None:
                        reason = 'output_limit'
                        send(signal.SIGTERM)
                        stop_at = time.monotonic()
            exit_code = process.wait()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
    except BaseException:
        send(signal.SIGTERM)
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            send(signal.SIGKILL)
            process.wait(timeout=5)
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
    try:
        declaration = json.loads(captured.decode())
        if not isinstance(declaration, dict) or not isinstance(declaration.get('summary'), str) or not declaration['summary'].strip():
            raise ValueError('Missing summary')
        if len(declaration['summary']) > 4096 or len(json.dumps(declaration, allow_nan=False).encode()) > 65536:
            raise ValueError('Declaration too large for structured execution metadata')
        status = 'valid'
    except (ValueError, UnicodeError, RecursionError):
        declaration = None
        status = 'invalid' if captured else 'unavailable'
    return ProviderResult(exit_code, reason, status, declaration, -exit_code if exit_code < 0 else None)


FAKE_SCRIPT = '''
import json, pathlib, signal, sys, time
mode = sys.argv[1]
sys.stdin.read()
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode in ("sleep", "ignore-term"):
    time.sleep(60)
pathlib.Path("fake-change.txt").write_text("deterministic fake provider edit\\n")
if mode == "malformed":
    print("not-json", flush=True)
elif mode != "missing":
    print(json.dumps({"summary":"Fake edit completed", "mode":mode}), flush=True)
if mode == "flood":
    print("x" * 100000, flush=True)
sys.exit(7 if mode == "failure" else 0)
'''


class FakeAdapter:
    version = 'fake-v1'

    def __init__(self, mode: str = 'success') -> None:
        self.mode = mode
        self.requests: list[ExecutionRequest] = []

    def run(self, request: ExecutionRequest, workspace: Path, artifacts: Path,
            cancelled: Callable[[], bool], started: Callable[[int, str], None],
            timeout: float, grace: float, max_log_bytes: int) -> ProviderResult:
        self.requests.append(request)
        return supervise([PYTHON, '-I', '-c', FAKE_SCRIPT, self.mode], request, workspace,
                         artifacts, cancelled, started, timeout, grace, max_log_bytes)


class CodexCliAdapter:
    """Command contract verified against codex-cli 0.165.0. Live invocation is gated."""
    REQUIRED_FLAGS = ('--sandbox', '--model', '--json', '--ephemeral', '--ignore-user-config',
                      '--ignore-rules', '--skip-git-repo-check', '--output-last-message')

    def __init__(self, binary: str = 'codex') -> None:
        self.binary = binary
        self.version = 'unverified'

    def inspect_installation(self) -> dict:
        try:
            version = subprocess.run([self.binary, '--version'], capture_output=True, text=True, timeout=10,
                                     env=clean_environment(), check=False)
            help_result = subprocess.run([self.binary, 'exec', '--help'], capture_output=True, text=True, timeout=10,
                                         env=clean_environment(), check=False)
            supported = version.returncode == help_result.returncode == 0 and all(
                flag in help_result.stdout for flag in self.REQUIRED_FLAGS)
            self.version = version.stdout.strip() if version.returncode == 0 else 'unavailable'
            return {'installed': version.returncode == 0, 'version': self.version,
                    'supported_flags': supported, 'credentials': 'not exposed or inspected',
                    'live_enabled': False, 'blocker': LIVE_BLOCKER}
        except (OSError, subprocess.TimeoutExpired):
            return {'installed': False, 'version': 'unavailable', 'supported_flags': False,
                    'credentials': 'not exposed or inspected', 'live_enabled': False, 'blocker': LIVE_BLOCKER}

    def command(self, request: ExecutionRequest) -> list[str]:
        return [self.binary, 'exec', '--model', request.model_id, '--sandbox', 'workspace-write',
                '--json', '--ephemeral', '--ignore-user-config', '--ignore-rules', '--skip-git-repo-check',
                '--output-last-message', 'agent-result.json', '-']

    def run(self, request: ExecutionRequest, workspace: Path, artifacts: Path,
            cancelled: Callable[[], bool], started: Callable[[int, str], None],
            timeout: float, grace: float, max_log_bytes: int) -> ProviderResult:
        raise SecurityError(LIVE_BLOCKER)
