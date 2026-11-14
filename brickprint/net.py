"""Small download helper with a curl/wget fallback for macOS Python certs."""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
from pathlib import Path


def download(url: str, dest: Path, timeout: int = 300) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brickprint/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response, open(tmp, "wb") as f:
            shutil.copyfileobj(response, f)
        os.replace(tmp, dest)
        return
    except Exception as exc:  # noqa: BLE001 - fall through to curl/wget
        last_error = exc
    for cmd in (
        ["curl", "-fsSL", "--retry", "3", "-o", str(tmp), url],
        ["wget", "-q", "-O", str(tmp), url],
    ):
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, check=True, timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.replace(tmp, dest)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if tmp.exists():
        tmp.unlink()
    raise RuntimeError(f"download failed for {url}: {last_error}")
