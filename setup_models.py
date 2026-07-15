"""First-run download of the Demucs separation model, with progress + cancel.

htdemucs_ft is a bag of 4 checkpoints (~330 MB) that demucs normally fetches
via torch.hub the first time a song is processed — making the user's very
first song feel painfully slow. Instead the server prefetches the same files
into torch.hub's checkpoints dir on first launch; get_model() then finds them
cached and never downloads. The web UI gates on /api/setup until this is done
(see the setup overlay in static/index.html); cancelling is allowed but the
app stays gated.

The (smaller) Whisper timing model is prefetched quietly in the background
once the gate clears — it never blocks entry.
"""

import hashlib
import os
import threading
from pathlib import Path

import requests

import remove_vocals as rv

_lock = threading.Lock()
_cancel = threading.Event()
_state = {"status": "idle", "done_bytes": 0, "total_bytes": 0, "error": None}
_whisper_started = False

CHUNK = 1 << 20  # 1 MiB


def _files() -> list[tuple[str, Path]]:
    """(url, destination) for every checkpoint in the model bag."""
    import torch
    import yaml
    from demucs.pretrained import REMOTE_ROOT, _parse_remote_files

    sigs = yaml.safe_load((REMOTE_ROOT / f"{rv.MODEL_NAME}.yaml").read_text())["models"]
    urls = _parse_remote_files(REMOTE_ROOT / "files.txt")
    ckpt_dir = Path(torch.hub.get_dir()) / "checkpoints"
    return [(urls[sig], ckpt_dir / urls[sig].rsplit("/", 1)[1]) for sig in sigs]


def is_ready() -> bool:
    try:
        return all(dest.exists() for _, dest in _files())
    except Exception:
        return False


def status() -> dict:
    with _lock:
        return {"ready": _state["status"] == "done" or is_ready(), **_state}


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _download_one(url: str, dest: Path) -> None:
    """Stream url to dest, verifying the sha256 prefix embedded in the name
    (…-<hash8>.th, same convention torch.hub checks)."""
    expected = dest.stem.rsplit("-", 1)[1]
    part = dest.with_suffix(".part")
    sha = hashlib.sha256()
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_content(CHUNK):
                if _cancel.is_set():
                    f.close()
                    part.unlink(missing_ok=True)
                    raise InterruptedError
                f.write(chunk)
                sha.update(chunk)
                with _lock:
                    _state["done_bytes"] += len(chunk)
    if not sha.hexdigest().startswith(expected):
        part.unlink(missing_ok=True)
        raise ValueError(f"checksum mismatch for {dest.name}")
    part.replace(dest)


def _worker() -> None:
    try:
        missing = [(u, d) for u, d in _files() if not d.exists()]
        if not missing:
            _set(status="done")
            ensure_whisper_async()
            return
        missing[0][1].parent.mkdir(parents=True, exist_ok=True)
        total = 0
        for url, _ in missing:
            r = requests.head(url, timeout=30, allow_redirects=True)
            total += int(r.headers.get("Content-Length") or 0)
        _set(status="downloading", done_bytes=0, total_bytes=total, error=None)
        for url, dest in missing:
            _download_one(url, dest)
        _set(status="done")
        ensure_whisper_async()
    except InterruptedError:
        _set(status="cancelled")
    except Exception as exc:
        _set(status="error", error=str(exc))


def start() -> None:
    with _lock:
        if _state["status"] == "downloading":
            return
        _state.update(status="downloading", done_bytes=0, total_bytes=0, error=None)
    _cancel.clear()
    threading.Thread(target=_worker, daemon=True).start()


def cancel() -> None:
    _cancel.set()


def ensure_whisper_async() -> None:
    """Best-effort background prefetch of the Whisper model (no gating)."""
    global _whisper_started
    with _lock:
        if _whisper_started:
            return
        _whisper_started = True

    def fetch():
        try:
            from faster_whisper.utils import download_model

            download_model(os.environ.get("ASEREJE_WHISPER_MODEL", "small"))
        except Exception:
            pass  # retried implicitly when timing first runs

    threading.Thread(target=fetch, daemon=True).start()


def autostart() -> None:
    """Called from app startup: begin the gate download if models are missing."""
    if is_ready():
        ensure_whisper_async()
    else:
        start()
