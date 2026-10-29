"""Explicit operator-owned configuration; no provider credentials are loaded."""
from __future__ import annotations

import math
import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .domain import ValidationError, nonempty


@dataclass(frozen=True)
class ModelProfile:
    provider: str
    model_id: str
    verified: bool = False


@dataclass(frozen=True)
class SchedulerConfig:
    global_concurrency: int = 2
    per_project_concurrency: int = 2
    interval_seconds: float = 2
    lease_seconds: float = 15
    claim_seconds: float = 30
    backoff_seconds: float = 3
    model_profile: str = 'sol'
    weights: tuple[int, int, int] = (1, 2, 3)

    def validate(self) -> None:
        for value in (self.global_concurrency, self.per_project_concurrency):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
                raise ValidationError('Concurrency limits must be integers in 1..32')
        for value in (self.interval_seconds, self.lease_seconds, self.claim_seconds, self.backoff_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 86400:
                raise ValidationError('Scheduler intervals must be finite and in (0, 86400]')
        if self.lease_seconds < self.interval_seconds * 2:
            raise ValidationError('Lease duration must be at least twice the polling interval')
        if self.model_profile not in ('luna', 'sol', 'astra'):
            raise ValidationError('Unknown default scheduler model profile')
        if len(self.weights) != 3 or any(isinstance(w, bool) or not isinstance(w, int) or not 1 <= w <= 8 for w in self.weights):
            raise ValidationError('weights must contain three integers in 1..8')

    @classmethod
    def load(cls, path: Path) -> SchedulerConfig:
        ExecutorConfig.load(path)  # Reuse operator-owned configuration checks.
        with path.expanduser().open('rb') as file:
            settings = tomllib.load(file).get('scheduler', {})
        if not isinstance(settings, dict):
            raise ValidationError('scheduler must be a TOML table')
        try:
            result = cls(**{**settings, 'weights': tuple(settings.get('weights', (1, 2, 3)))})
        except TypeError as exc:
            raise ValidationError(f'Invalid scheduler configuration: {exc}') from exc
        result.validate()
        return result


@dataclass(frozen=True)
class ExecutorConfig:
    repository_root: Path
    workspace_root: Path
    artifact_root: Path
    models: dict[str, ModelProfile]
    timeout_seconds: float = 300
    cancel_grace_seconds: float = 1
    max_log_bytes: int = 8 * 1024 * 1024
    codex_binary: str = 'codex'

    def validate(self, database: Path) -> None:
        nonempty(self.codex_binary, 'codex_binary')
        roots = (self.repository_root, self.workspace_root, self.artifact_root)
        for root in roots:
            if not root.is_absolute() or root != root.resolve():
                raise ValidationError('Configured roots must be absolute, canonical paths without symlinks')
            if database.resolve().is_relative_to(root):
                raise ValidationError('Managed roots must not contain the control-plane database')
            if any(root.is_relative_to(Path(p).resolve()) for p in ('/usr', '/bin', '/lib', '/lib64', '/etc', '/dev', '/proc', '/sys')):
                raise ValidationError('Managed roots cannot be under system runtime paths')
            if root.exists():
                trusted_directory(root)
        for index, root in enumerate(roots):
            for other in roots[index + 1:]:
                if root.is_relative_to(other) or other.is_relative_to(root):
                    raise ValidationError('Repository, worktree and artifact roots must be disjoint')
        for name, number in [('timeout_seconds', self.timeout_seconds), ('cancel_grace_seconds', self.cancel_grace_seconds)]:
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 < number <= 86400:
                raise ValidationError(f'{name} must be finite and in (0, 86400]')
        if isinstance(self.max_log_bytes, bool) or not isinstance(self.max_log_bytes, int) or not 1024 <= self.max_log_bytes <= 64 * 1024 * 1024:
            raise ValidationError('max_log_bytes must be between 1024 and 67108864')
        for name, model in self.models.items():
            if name not in ('luna', 'sol', 'astra') or model.provider not in ('fake', 'codex'):
                raise ValidationError('Profiles must be luna/sol/astra with provider fake/codex')
            nonempty(model.model_id, 'model_id')
            if not isinstance(model.verified, bool):
                raise ValidationError('verified must be a boolean')

    @classmethod
    def load(cls, path: Path) -> ExecutorConfig:
        path = path.expanduser()
        info = path.stat()
        if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValidationError('Executor config must be operator-owned and not group/world writable')
        with path.open('rb') as file:
            data = tomllib.load(file)
        try:
            settings = data['executor']
            if not isinstance(settings, dict) or not isinstance(data.get('models', {}), dict):
                raise ValidationError('executor and models must be TOML tables')
            profiles = {name: ModelProfile(**profile) for name, profile in data.get('models', {}).items()}
            return cls(Path(settings['repository_root']), Path(settings['workspace_root']),
                       Path(settings['artifact_root']), profiles,
                       settings.get('timeout_seconds', 300), settings.get('cancel_grace_seconds', 1),
                       settings.get('max_log_bytes', 8 * 1024 * 1024), settings.get('codex_binary', 'codex'))
        except (KeyError, TypeError) as exc:
            raise ValidationError(f'Invalid executor configuration: {exc}') from exc


def trusted_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValidationError(f'Directory must be operator-owned, not symlinked or group/world writable: {path}')
