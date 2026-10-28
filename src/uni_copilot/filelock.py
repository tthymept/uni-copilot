"""
A lock between programs, so two of them don't do the same thing at once.

In plain words:
  threading.Lock only works inside one program. Here several programs can
  collide: the scheduled sync, the digest's own sync, and the ntulearn-mcp
  server in a Claude Code session all open the same browser profile, and
  Chromium allows only one at a time (the likely cause of the 26 Oct
  "Browser failed (hidden)" errors). So they take turns via a small lock file:
  the operating system lets only one program hold a lock on it.

  The OS drops the lock by itself when the program ends, even if it crashes,
  so a lock can never be left behind by accident.

Used for:
  .auth/profile.lock   one browser sign-in at a time (auth/session.py)
  .data/sync.lock      one sync at a time (cli.py)
"""

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt

    # Locks the first byte of the file without waiting; raises OSError if
    # another program holds it.
    def _try_lock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    # Releases that byte.
    def _unlock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    # Same on Linux/macOS (for a later always-on machine).
    def _try_lock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    # Releases the lock.
    def _unlock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# Holds the lock on `path` for the `with` block. Waits up to wait_s seconds
# for another program to let go, checking twice a second. Yields True if it
# got the lock, False if it gave up (the caller decides what to do then).
@contextmanager
def file_lock(path: Path, wait_s: float = 0) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    got = False
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                _try_lock(f)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.5)
        yield got
    finally:
        if got:
            _unlock(f)
        f.close()
