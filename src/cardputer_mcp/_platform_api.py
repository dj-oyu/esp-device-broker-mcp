"""Typed view of required POSIX APIs, used only after callers' platform guards.

The cast neither supplies missing APIs nor changes permissions. Windows retains
its existing unsupported/private-ACL boundaries; there is no fallback value.
"""

import os
from typing import Protocol, cast


class _PosixFileAPI(Protocol):
    O_DIRECTORY: int
    O_NOFOLLOW: int
    O_NONBLOCK: int

    def geteuid(self) -> int: ...

    def fchmod(self, descriptor: int, mode: int) -> None: ...


posix_file_api = cast(_PosixFileAPI, os)
