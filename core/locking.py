"""Cross-platform nonblocking file locks for the two durable stores."""

import os


def exclusive_lock(handle):
    if os.name == "nt":
        import msvcrt

        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
