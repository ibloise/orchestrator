"""Trusted Git worktrees and isolated file staging; never execute Git in agent code."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from .config import ExecutorConfig, trusted_directory
from .domain import ValidationError
from .security import clean_environment


def safe_id(identifier: str) -> str:
    if not re.fullmatch(r'[a-z]+-[0-9a-f]{12}', identifier):
        raise ValidationError('Invalid managed identifier')
    return identifier


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
         '-c', 'core.attributesFile=/dev/null', '-C', str(repository), *arguments],
        env=clean_environment(), capture_output=True, text=True, errors='replace', timeout=30, check=False)
    if result.returncode:
        raise ValidationError(f'Git operation failed: {result.stderr.strip()}')
    return result.stdout.rstrip('\n')


def tree_snapshot(root: Path) -> dict[str, dict]:
    """Hash files without following agent symlinks, including ignored/untracked files."""
    result = {}

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            if relative == '.git':
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                result[relative] = {'kind': 'symlink', 'target': os.readlink(path)}
            elif stat.S_ISDIR(info.st_mode):
                result[relative] = {'kind': 'directory'}
                visit(path)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                with path.open('rb') as file:
                    checksum = hashlib.file_digest(file, 'sha256').hexdigest()
                result[relative] = {'kind': 'file', 'sha256': checksum,
                                    'executable': bool(info.st_mode & 0o111)}
            else:
                raise ValidationError(f'Unsupported workspace file or hardlink: {relative}')
    visit(root)
    return result


class WorkspaceManager:
    def __init__(self, config: ExecutorConfig) -> None:
        self.config = config

    def repository(self, reference: str) -> Path:
        raw = Path(reference)
        path = raw.resolve(strict=True)
        if '..' in raw.parts or not path.is_relative_to(self.config.repository_root) or path == self.config.repository_root:
            raise ValidationError('Repository must resolve inside the trusted repository root')
        trusted_directory(path)
        if git(path, 'rev-parse', '--is-bare-repository') != 'false':
            raise ValidationError('Only non-bare reference clones are supported')
        if Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path or not (path / '.git').is_dir():
            raise ValidationError('Reference must be a standalone repository, not another worktree')
        if (path / '.git').is_symlink():
            raise ValidationError('Symlinked Git metadata is unsupported')
        trusted_directory(path / '.git')
        local = git(path, 'config', '--local', '--list')
        if any(line.lower().startswith(('filter.', 'include.', 'includeif.', 'extensions.')) for line in local.splitlines()):
            raise ValidationError('Repository filters/includes/extensions are unsupported')
        if (path / '.gitmodules').exists():
            raise ValidationError('Submodules are unsupported')
        git(path, 'rev-parse', '--verify', 'HEAD^{commit}')
        return path

    def prepare(self, task_id: str, reference: str, existing: dict | None) -> dict:
        safe_id(task_id)
        repository = self.repository(reference)
        root = self.config.workspace_root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        trusted_directory(root)
        path = root / task_id
        branch = f'agent/{task_id}'
        if existing:
            self.validate(existing)
            if existing['repository'] != str(repository):
                raise ValidationError('Workspace repository changed; operator reconciliation required')
            return existing
        if path.exists() or path.is_symlink():
            raise ValidationError('Unmanaged workspace collision; refusing to overwrite')
        refs = git(repository, 'for-each-ref', '--format=%(refname)', f'refs/heads/{branch}')
        if refs:
            raise ValidationError('Unmanaged branch collision; refusing to overwrite')
        base = git(repository, 'rev-parse', '--verify', 'HEAD^{commit}')
        git(repository, 'worktree', 'add', '-b', branch, str(path), base)
        path.chmod(0o700)
        return {'id': f'wsp-{task_id.split("-", 1)[1]}', 'task_id': task_id,
                'repository': str(repository), 'path': str(path), 'branch': branch,
                'base_commit': base, 'git_marker': (path / '.git').read_text()}

    def validate(self, workspace: dict) -> Path:
        path = self.config.workspace_root / safe_id(workspace['task_id'])
        if workspace['path'] != str(path) or path.resolve() != path:
            raise ValidationError('Workspace identity/path mismatch')
        trusted_directory(path)
        marker = path / '.git'
        if marker.is_symlink() or not marker.is_file() or marker.read_text() != workspace['git_marker']:
            raise ValidationError('Workspace Git metadata changed')
        repository = self.repository(workspace['repository'])
        registrations = git(repository, 'worktree', 'list', '--porcelain')
        if f'worktree {path}\n' not in registrations + '\n':
            raise ValidationError('Workspace is no longer registered')
        if git(path, 'symbolic-ref', 'HEAD') != f'refs/heads/{workspace["branch"]}':
            raise ValidationError('Workspace branch changed; refusing to reset')
        return path

    def evidence(self, workspace: dict) -> dict:
        path = self.validate(workspace)
        status = git(path, 'status', '--porcelain=v1', '--untracked-files=all')
        tracked = git(path, 'diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', workspace['base_commit'])
        untracked = git(path, 'ls-files', '--others', '--exclude-standard', '-z')
        return {'base_commit': workspace['base_commit'], 'head_commit': git(path, 'rev-parse', 'HEAD'),
                'status_porcelain': status, 'changed_paths': sorted(set(filter(None, (tracked + '\0' + untracked).split('\0')))),
                'diff': git(path, 'diff', '--no-ext-diff', '--no-textconv', workspace['base_commit'], '--')}

    def stage(self, workspace: dict, destination: Path) -> dict:
        path = self.validate(workspace)
        before = tree_snapshot(path)
        shutil.copytree(path, destination, symlinks=True, ignore=lambda directory, names: ['.git'] if Path(directory) == path else [])
        if tree_snapshot(destination) != before:
            raise ValidationError('Workspace changed while staging; refusing inconsistent input')
        return before

    def import_changes(self, workspace: dict, stage: Path, before: dict) -> dict:
        path = self.validate(workspace)
        if (stage / '.git').exists() or (stage / '.git').is_symlink():
            raise ValidationError('Agent attempted to create Git metadata; stage retained for inspection')
        after = tree_snapshot(stage)
        for relative, info in after.items():
            if info['kind'] == 'symlink' and info != before.get(relative):
                try:
                    inside = (stage / relative).resolve().is_relative_to(stage)
                except RuntimeError:
                    inside = False
                if not inside:
                    raise ValidationError('New or changed symlink escapes the isolated workspace; import refused')
        if tree_snapshot(path) != before:
            raise ValidationError('Worktree changed during execution; stage retained, no import performed')
        # Only operate on validated, supervisor-owned trees after the process is terminal.
        for relative in sorted(before, key=lambda name: len(Path(name).parts), reverse=True):
            if relative not in after or before[relative]['kind'] != after[relative]['kind']:
                target = path / relative
                if target.is_dir() and not target.is_symlink():
                    target.rmdir()
                else:
                    target.unlink()
        for relative, info in after.items():
            target = path / relative
            source = stage / relative
            if info['kind'] == 'directory':
                target.mkdir(exist_ok=True)
            elif before.get(relative) != info:
                if target.exists() or target.is_symlink():
                    target.unlink()
                if info['kind'] == 'symlink':
                    target.symlink_to(info['target'])
                else:
                    shutil.copy2(source, target, follow_symlinks=False)
        return self.evidence(workspace)
