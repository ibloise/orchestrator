"""Capability checks for tests that actually launch the offline sandbox."""
import ctypes
import errno
import os
import platform

import pytest


@pytest.fixture(scope='session')
def sandbox_capability():
    if platform.system() != 'Linux' or platform.machine() != 'x86_64' or os.getuid() == 0:
        pytest.skip('sandbox requires non-root Linux x86_64')
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 0 and ctypes.get_errno() in (errno.ENOSYS, errno.EOPNOTSUPP):
        pytest.skip('Landlock ABI query is unsupported by the kernel')
    assert abi >= 0, f'Landlock ABI query failed: errno {ctypes.get_errno()}'
    if abi < 6:
        pytest.skip(f'Landlock ABI {abi} is below the required ABI 6')


@pytest.fixture(autouse=True)
def require_sandbox_for_marked_tests(request):
    if request.node.get_closest_marker('sandbox'):
        request.getfixturevalue('sandbox_capability')
