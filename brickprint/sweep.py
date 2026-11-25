"""Sweep old work dirs that brickprint leaves behind.

``verify`` keeps its ``brickprint_verify_*`` temp dir on purpose (its path is
in the report), and an interrupted probe / plate check could leave its dir in
the cache.  ``build`` and ``verify`` call :func:`maybe_sweep` at start:

* candidates: real directories (never symlinks) directly under the temp dir
  named ``brickprint_verify_*``, and directly under ``cfg["cache_dir"]`` named
  ``platecheck_*``, ``probe_*``, ``final_*``.  Nothing else, and never a
  ``--workdir`` the user passed;
* a dir with anything modified within the last hour is never touched (it may
  belong to a build running right now);
* candidates older than ``sweep_days`` (default 3) are deleted; then, while
  the remaining ``brickprint_verify_*`` dirs total more than
  ``verify_keep_gb`` (default 2 GB), the oldest are deleted (the newest one
  is always kept, whatever its size);
* ``BRICKPRINT_NO_SWEEP=1`` turns it off; a failure never fails the caller.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from . import slice_cache

NO_SWEEP_ENV = "BRICKPRINT_NO_SWEEP"
SWEEP_DAYS = 3.0
VERIFY_KEEP_GB = 2.0
GUARD_S = 3600.0
VERIFY_PREFIX = "brickprint_verify_"
CACHE_PREFIXES = ("platecheck_", "probe_", "final_")


def disabled() -> bool:
    return os.environ.get(NO_SWEEP_ENV, "").strip() not in ("", "0")


def _usage(path: Path) -> tuple[int, float]:
    """(bytes, newest mtime) of a directory tree, without following links."""
    size, newest = 0, os.lstat(path).st_mtime
    stack = [str(path)]
    while stack:
        top = stack.pop()
        try:
            with os.scandir(top) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    newest = max(newest, st.st_mtime)
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        size += st.st_size
        except OSError:
            continue
    return size, newest


def _candidates(root: Path, prefixes: tuple[str, ...], exclude: set[str]) -> list[Path]:
    out = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return out
    for e in entries:
        if not e.name.startswith(prefixes):
            continue
        if e.is_symlink() or not e.is_dir(follow_symlinks=False):
            continue
        p = Path(e.path)
        rp = os.path.realpath(p)
        # never a user --workdir (or the file being verified), nor a dir holding one
        if any(x == rp or x.startswith(rp + os.sep) for x in exclude):
            continue
        out.append(p)
    return out


def sweep_workdirs(cfg: dict, temp_root: Path | str | None = None,
                   cache_root: Path | str | None = None, exclude=(),
                   now: float | None = None, log=print) -> dict:
    """Apply the sweep policy; returns {"removed": n, "freed": bytes,
    "failed": n}.  ``temp_root`` / ``cache_root`` default to
    tempfile.gettempdir() and cfg["cache_dir"] (injectable for tests)."""
    res = {"removed": 0, "freed": 0, "failed": 0}
    if disabled():
        return res
    now = time.time() if now is None else now
    days = float(cfg.get("sweep_days", SWEEP_DAYS) or 0)
    cap = float(cfg.get("verify_keep_gb", VERIFY_KEEP_GB) or 0) * 1024 ** 3
    temp_root = Path(temp_root if temp_root is not None else tempfile.gettempdir())
    cache_root = Path(cache_root if cache_root is not None else cfg["cache_dir"])
    excl = {os.path.realpath(x) for x in exclude if x}

    found = []   # (path, is_verify, bytes, newest mtime)
    for root, prefixes, is_verify in ((temp_root, (VERIFY_PREFIX,), True),
                                      (cache_root, CACHE_PREFIXES, False)):
        for p in _candidates(root, prefixes, excl):
            try:
                size, newest = _usage(p)
            except OSError:
                continue
            found.append((p, is_verify, size, newest))

    # Also sweep stale leftover dirs under cache_root/bambu/<uuid>/
    bambu_dir = cache_root / "bambu"
    if bambu_dir.is_dir() and not bambu_dir.is_symlink():
        for p in _candidates(bambu_dir, ("",), excl):
            try:
                size, newest = _usage(p)
            except OSError:
                continue
            found.append((p, False, size, newest))

    def remove(item) -> bool:
        p, _, size, _ = item
        shutil.rmtree(p, ignore_errors=True)
        if p.exists():
            res["failed"] += 1
            return False
        res["removed"] += 1
        res["freed"] += size
        return True

    gone = set()
    for item in found:
        p, _, _, newest = item
        if now - newest < GUARD_S:
            continue
        if days > 0 and now - newest > days * 86400 and remove(item):
            gone.add(p)
    if cap > 0:
        left = sorted((it for it in found if it[1] and it[0] not in gone), key=lambda it: it[3])
        total = sum(it[2] for it in left)
        # oldest first; the newest is never evicted by the cap, so the latest
        # verify report always has its G-code (a big project alone can exceed it)
        for item in left[:-1]:
            if total <= cap:
                break
            if now - item[3] < GUARD_S:
                continue
            if remove(item):
                total -= item[2]
    # Best-effort remove cache_root/bambu if emptied
    if bambu_dir.is_dir() and not bambu_dir.is_symlink():
        try:
            bambu_dir.rmdir()
        except OSError:
            pass
    # Keep the shared content-addressed G-code cache bounded (TTL + LRU size cap)
    gcode_cache_dir = cache_root / "gcode_cache"
    if gcode_cache_dir.is_dir() and not gcode_cache_dir.is_symlink():
        ev = slice_cache.evict(gcode_cache_dir, now=now)
        res["removed"] += ev["removed"]
        res["freed"] += ev["freed"]
    if res["removed"]:
        mb = res["freed"] / 1024 ** 2
        size = f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"
        log(f"sweep: removed {res['removed']} old brickprint work dir(s), freed {size} "
            f"(set {NO_SWEEP_ENV}=1 to disable)")
    return res


def maybe_sweep(cfg: dict, exclude=(), log=print) -> dict | None:
    """sweep_workdirs() for the CLI: never raises."""
    if disabled():
        return None
    try:
        return sweep_workdirs(cfg, exclude=exclude, log=log)
    except Exception as e:   # the sweep must never fail a build
        try:
            log(f"sweep: skipped ({e})")
        except Exception:
            pass
        return None
