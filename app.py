#!/usr/bin/env python3
"""Web UI for the vocal remover.

Run:  .venv/bin/python app.py   then open http://127.0.0.1:5055

Each processed song lives in its own folder under library/:
    library/<slug>/original.mp3
    library/<slug>/instrumental.mp3
    library/<slug>/lyrics.txt      (if found)
    library/<slug>/meta.json
Deleting a folder by hand removes the entry; the UI can delete them too.
"""

import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory

import remove_vocals as rv
import timing as timing_mod
import translate as tr

BASE = Path(__file__).resolve().parent
LIBRARY = BASE / "library"
LEGACY_DOWNLOADS = BASE / "downloads"

app = Flask(__name__, static_folder="static")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

SESSION_FILE = BASE / "session.json"
AVATARS = BASE / "avatars"
session_lock = threading.Lock()
session_state: dict = {"admin": None, "users": {},
                       "party": {"allow_comments": True, "show_scores": True}}
live_messages: list[dict] = []
_msg_seq = 0
vote_state: dict = {"round": 0, "votes": {}}  # votes: client_id -> stars
CID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

DL_QUEUE_FILE = BASE / "download_queue.json"
dl_lock = threading.Lock()
dl_items: list[dict] = []   # pending searches: {id, query, langs}
dl_done: list[dict] = []    # recently finished: {query, status, song_id?, error?}
current_item: dict | None = None

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Words that suggest a result is NOT the plain studio recording we want to split
BAD_WORDS = {
    "live": 40, "en vivo": 40, "en directo": 40, "concert": 30, "cover": 40,
    "karaoke": 50, "instrumental": 30, "remix": 25, "mashup": 40, "parody": 50,
    "reaction": 60, "sped up": 30, "slowed": 30, "reverb": 20, "8d": 40,
    "nightcore": 35, "loop": 25, "1 hour": 50, "extended": 20, "tutorial": 50,
}


# ---------------------------------------------------------------------------
# Library store
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:60] or "song"


def unique_folder(slug: str) -> Path:
    folder, n = LIBRARY / slug, 2
    while folder.exists():
        folder = LIBRARY / f"{slug}-{n}"
        n += 1
    return folder


def is_complete_song(folder: Path) -> bool:
    return (folder / "original.mp3").exists() and (folder / "instrumental.mp3").exists()


def all_sets() -> list[dict]:
    sets = []
    for meta_file in LIBRARY.glob("*/meta.json"):
        if not is_complete_song(meta_file.parent):
            # interrupted download (jobs rename atomically from .tmp, so a
            # visible folder missing audio is genuinely broken): remove it
            print(f"Removing incomplete song {meta_file.parent.name}")
            shutil.rmtree(meta_file.parent, ignore_errors=True)
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta["id"] = meta_file.parent.name
        meta["lyrics"] = (meta_file.parent / "lyrics.txt").exists()
        meta["translations"] = list_translations(meta_file.parent)
        cover = meta_file.parent / "cover.jpg"
        meta["cover"] = cover.exists()
        meta["cover_ts"] = int(cover.stat().st_mtime) if cover.exists() else None
        sets.append(meta)
    sets.sort(key=lambda m: m.get("created", ""), reverse=True)
    return sets


def write_meta(folder: Path, meta: dict) -> None:
    (folder / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


DEFAULT_COVER = BASE / "static" / "default-cover.jpg"
PLAYLISTS_FILE = BASE / "playlists.json"
PLCOVERS = BASE / "plcovers"
pl_lock = threading.Lock()


def _existing_song_ids() -> set[str]:
    if not LIBRARY.is_dir():
        return set()
    return {f.name for f in LIBRARY.iterdir() if f.is_dir() and not f.name.startswith(".")}


def _read_playlists_raw() -> list[dict]:
    try:
        return json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))["playlists"]
    except Exception:
        return []


def _write_playlists(playlists: list[dict]) -> None:
    PLAYLISTS_FILE.write_text(
        json.dumps({"playlists": playlists}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_playlists() -> list[dict]:
    """Read playlists, self-healing references to songs that no longer exist."""
    with pl_lock:
        playlists = _read_playlists_raw()
        existing = _existing_song_ids()
        changed = False
        for pl in playlists:
            healed = [s for s in pl["songs"] if s in existing]
            if healed != pl["songs"]:
                pl["songs"] = healed
                changed = True
        if changed:
            _write_playlists(playlists)
        for pl in playlists:
            cover = PLCOVERS / f"{pl['id']}.jpg"
            pl["cover_ts"] = int(cover.stat().st_mtime) if cover.exists() else None
        return playlists


def mutate_playlists(fn) -> list[dict]:
    """Apply fn(playlists) under the lock and persist. fn may raise ValueError."""
    with pl_lock:
        playlists = _read_playlists_raw()
        fn(playlists)
        _write_playlists(playlists)
    return load_playlists()


def save_square_image(data: bytes, out: Path, size: int = 600) -> None:
    """Center-crop to a square and save as JPEG."""
    from io import BytesIO

    from PIL import Image, ImageOps

    img = Image.open(BytesIO(data))
    # honour the EXIF orientation tag (phone photos) before touching pixels,
    # otherwise portrait shots come out rotated 90°
    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    img.resize((size, size), Image.LANCZOS).save(out, "JPEG", quality=88)


def pick_thumbnail(info: dict) -> str | None:
    """Best video thumbnail URL, preferring square art (music 'Topic' videos)."""
    best_url, best_score = info.get("thumbnail"), 0
    for thumb in info.get("thumbnails") or []:
        url, w, h = thumb.get("url"), thumb.get("width") or 0, thumb.get("height") or 0
        if not url:
            continue
        score = w * h
        if h and abs(w / h - 1) < 0.15:
            score += 10_000_000  # square art beats any resolution
        if score > best_score:
            best_url, best_score = url, score
    return best_url


def fetch_cover(info: dict, out: Path) -> bool:
    try:
        thumb = pick_thumbnail(info)
        if not thumb:
            return False
        r = requests.get(thumb, timeout=20)
        r.raise_for_status()
        save_square_image(r.content, out)
        return True
    except Exception:
        return False


def backfill_lyrics_lang() -> None:
    """Detect and persist the lyrics language for entries that lack it."""
    for meta_file in LIBRARY.glob("*/meta.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            lyrics_file = meta_file.parent / "lyrics.txt"
            if meta.get("lyrics_lang") or not lyrics_file.exists():
                continue
            _, body = split_lyrics_header(lyrics_file.read_text(encoding="utf-8"))
            lang = tr.detect_language(body)
            if lang:
                meta["lyrics_lang"] = lang
                write_meta(meta_file.parent, meta)
                print(f"Detected lyrics language for {meta_file.parent.name}: {lang}")
        except Exception:
            pass


def backfill_covers() -> None:
    """Fetch covers for library entries created before covers existed."""
    for meta_file in LIBRARY.glob("*/meta.json"):
        folder = meta_file.parent
        if (folder / "cover.jpg").exists():
            continue
        try:
            vid = json.loads(meta_file.read_text(encoding="utf-8")).get("video_id")
            if not vid:
                continue
            r = requests.get(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", timeout=15)
            r.raise_for_status()
            save_square_image(r.content, folder / "cover.jpg")
            print(f"Fetched cover for {folder.name}")
        except Exception:
            pass


def list_translations(folder: Path) -> list[str]:
    return sorted(p.name.split(".")[1] for p in folder.glob("lyrics.*.txt"))


def split_lyrics_header(raw: str) -> tuple[str, str]:
    """Our lyrics files start with 'Artist - Track' + one or more '[credit]'
    lines (translations add '[translated to ... by ...]')."""
    lines = raw.splitlines()
    if len(lines) >= 2 and lines[1].startswith("["):
        i = 2
        while i < len(lines) and (
            not lines[i].strip()
            or (i < 5 and lines[i].startswith("[") and lines[i].rstrip().endswith("]"))
        ):
            i += 1
        return "\n".join(lines[:2]) + "\n", "\n".join(lines[i:]).strip()
    return "", raw.strip()


def translate_set(folder: Path, langs: list[str]) -> dict:
    """Create lyrics.<lang>.txt translations inside a song folder."""
    lyrics_file = folder / "lyrics.txt"
    if not lyrics_file.exists():
        raise RuntimeError("this song has no lyrics to translate")
    header, body = split_lyrics_header(lyrics_file.read_text(encoding="utf-8"))

    meta_file = folder / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    source = meta.get("lyrics_lang") or tr.detect_language(body)

    result = {"done": [], "skipped": [], "engine": None, "source_lang": source}
    for lang in [l.lower() for l in langs]:
        if source and lang == source:
            result["skipped"].append(
                {"lang": lang, "source": source,
                 "reason": f"lyrics are already in {tr.lang_name(source)}"}
            )
            continue
        text, engine = tr.translate_lyrics(body, lang)
        # pure lyrics, no header/credit block (engine reported in the response)
        (folder / f"lyrics.{lang}.txt").write_text(text + "\n", encoding="utf-8")
        result["done"].append(lang)
        result["engine"] = engine

    if meta_file.exists() and source and meta.get("lyrics_lang") != source:
        meta["lyrics_lang"] = source
        write_meta(folder, meta)
    return result


def norm_tokens(text: str | None) -> set[str]:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def find_duplicates(video_id: str | None, text: str | None) -> list[dict]:
    """Entries that look like what the user is asking for again."""
    dups = []
    query_tokens = norm_tokens(text)
    for meta in all_sets():
        if video_id and meta.get("video_id") == video_id:
            dups.append({**meta, "match": "exact same video"})
            continue
        stored = norm_tokens(
            " ".join(filter(None, [meta.get("title"), meta.get("artist"), meta.get("query")]))
        )
        if query_tokens and stored:
            overlap = len(query_tokens & stored) / len(query_tokens)
            if overlap >= 0.6:
                dups.append({**meta, "match": "similar title/artist"})
    return dups


def migrate_legacy() -> None:
    """Move loose downloads/*.mp3 sets from older versions into library folders."""
    if not LEGACY_DOWNLOADS.is_dir():
        return
    for inst in list(LEGACY_DOWNLOADS.glob("*_instrumental.mp3")):
        base = inst.name[: -len("_instrumental.mp3")]
        original = LEGACY_DOWNLOADS / f"{base}.mp3"
        if not original.exists():
            continue
        created = datetime.fromtimestamp(original.stat().st_mtime).isoformat(timespec="seconds")
        folder = unique_folder(slugify(base))
        folder.mkdir(parents=True)
        shutil.move(original, folder / "original.mp3")
        shutil.move(inst, folder / "instrumental.mp3")
        lyrics = LEGACY_DOWNLOADS / f"{base}_lyrics.txt"
        if lyrics.exists():
            shutil.move(lyrics, folder / "lyrics.txt")
        write_meta(folder, {
            "title": base.replace("_", " "),
            "artist": None, "channel": None, "duration": None,
            "video_id": None, "url": None, "query": None,
            "reason": "migrated from old downloads/ layout",
            "created": created,
        })
        print(f"Migrated {base} -> {folder.name}")


def repair_metadata() -> None:
    """Give a minimal meta.json to library folders that lack one (e.g. created
    by hand, or left behind by an interrupted migration)."""
    for folder in LIBRARY.iterdir():
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        if (folder / "meta.json").exists() or not (folder / "original.mp3").exists():
            continue
        write_meta(folder, {
            "title": folder.name.replace("-", " ").replace("_", " "),
            "artist": None, "channel": None, "duration": None,
            "video_id": None, "url": None, "query": None,
            "reason": "metadata reconstructed from folder name",
            "created": datetime.fromtimestamp(
                (folder / "original.mp3").stat().st_mtime).isoformat(timespec="seconds"),
        })
        print(f"Repaired metadata for {folder.name}")


def cleanup_tmp() -> None:
    for tmp in LIBRARY.glob(".tmp-*"):
        shutil.rmtree(tmp, ignore_errors=True)


def cleanup_incomplete() -> None:
    """Delete songs left half-downloaded by an interrupted job."""
    if not LIBRARY.is_dir():
        return
    for folder in LIBRARY.iterdir():
        if folder.is_dir() and not folder.name.startswith(".") and not is_complete_song(folder):
            print(f"Removing incomplete song {folder.name}")
            shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------------------
# YouTube pick
# ---------------------------------------------------------------------------

def score_entry(entry: dict) -> tuple[float, list[str]]:
    title = (entry.get("title") or "").lower()
    channel = (entry.get("channel") or entry.get("uploader") or "")
    duration = entry.get("duration") or 0
    views = entry.get("view_count") or 0

    score, why = 0.0, []
    if "official" in title:
        score += 25
        why.append("official")
    if re.search(r"\b(audio|hq)\b", title):
        score += 8
        why.append("audio")
    if channel.lower().endswith(" - topic"):
        # YouTube auto-generated music channels carry the clean studio track
        score += 30
        why.append("topic")
    for word, penalty in BAD_WORDS.items():
        if word in title:
            score -= penalty
    if 90 <= duration <= 480:
        score += 15
        why.append("duration")
    elif duration and (duration > 600 or duration < 60):
        score -= 30
    if views:
        score += min(12.0, math.log10(views + 1))
        if views > 10_000_000:
            why.append(f"views:{views / 1e6:.0f}")
    return score, why


# English rendering of reason codes, used for the stored meta.json only —
# the UI translates the codes itself per locale.
REASON_EN = {
    "official": "official upload", "audio": "audio-only version",
    "topic": "auto-generated studio track", "duration": "song-like duration",
    "views": "{n}M views", "best": "best overall match", "url": "direct URL",
}


def reason_text(codes) -> str:
    if isinstance(codes, str):
        return codes
    parts = []
    for code in codes:
        key, _, n = code.partition(":")
        parts.append(REASON_EN.get(key, key).replace("{n}", n))
    return ", ".join(parts)


def smart_pick(query: str) -> tuple[str, dict, str]:
    """Search YouTube and pick the hit most likely to be the plain studio song."""
    import yt_dlp

    opts = {"quiet": True, "extract_flat": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch10:{query}", download=False)
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        raise RuntimeError(f"no YouTube results for {query!r}")

    scored = sorted((score_entry(e) + (e,) for e in entries), key=lambda t: -t[0])
    best = scored[0][2]
    url = best.get("url") or f"https://www.youtube.com/watch?v={best['id']}"
    reason = scored[0][1] or ["best"]
    return url, best, reason


# ---------------------------------------------------------------------------
# Processing job
# ---------------------------------------------------------------------------

def log(job: dict, key: str, **vars) -> None:
    """Structured log entry: the UI renders it in the active locale."""
    job["log"].append({"k": key, "v": vars})


def set_phase(job: dict, phase: str, key: str, **vars) -> None:
    job["phase"] = phase
    log(job, key, **vars)


def run_job(job_id: str, query: str, url: str, reason: list, langs: list[str]) -> None:
    job = jobs[job_id]
    tmp = LIBRARY / f".tmp-{job_id}"
    try:
        tmp.mkdir(parents=True)
        set_phase(job, "downloading", "downloading")
        mp3_path, info = rv.download_from_youtube(url, tmp, "original.%(ext)s")
        track, artist, duration = rv.guess_track_and_artist(info, info.get("title", "song"))
        job["video"] = {
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"),
            "url": info.get("webpage_url") or url,
            "reason": reason,
        }
        fetch_cover(info, tmp / "cover.jpg")
        set_phase(job, "lyrics", "lyricsSearch", title=info.get("title"))

        lyrics_path = rv.fetch_and_save_lyrics(info, mp3_path, tmp / "lyrics.txt")
        log(job, "lyricsFound" if lyrics_path else "lyricsNotFound")

        if langs and lyrics_path:
            set_phase(job, "translating", "translating", engine=tr.active_engine())
            try:
                res = translate_set(tmp, langs)
                for skip in res["skipped"]:
                    log(job, "skipSameLang", lang=skip["lang"], source=skip.get("source") or "?")
                if res["done"]:
                    log(job, "translated", langs=res["done"], engine=res["engine"])
            except Exception as exc:
                log(job, "translateFailed", error=str(exc))
        elif not langs:
            log(job, "skipTranslateNoLangs")
        else:
            log(job, "skipTranslateNoLyrics")

        set_phase(job, "separating", "separating")

        device = rv.pick_device(None)
        try:
            rv.remove_vocals(mp3_path, tmp / "instrumental.mp3", device, resync_original=True)
        except Exception as exc:
            if device == "cpu":
                raise
            log(job, "deviceFallback", device=device)
            rv.remove_vocals(mp3_path, tmp / "instrumental.mp3", "cpu", resync_original=True)

        if lyrics_path:
            log(job, "timing")
            try:
                if not timing_mod.compute_best(tmp):
                    log(job, "timingFailed")
            except Exception:
                log(job, "timingFailed")

        folder = unique_folder(slugify(f"{artist} {track}" if artist else track))
        tmp.rename(folder)
        meta = {
            "title": track,
            "artist": artist,
            "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"),
            "video_id": info.get("id"),
            "url": info.get("webpage_url") or url,
            "query": query,
            "reason": reason_text(reason),
            "created": datetime.now().isoformat(timespec="seconds"),
        }
        meta["lyrics_lang"] = tr.detect_language(
            split_lyrics_header((folder / "lyrics.txt").read_text(encoding="utf-8"))[1]
        ) if lyrics_path else None
        write_meta(folder, meta)
        job["result"] = {
            **meta, "id": folder.name,
            "lyrics": lyrics_path is not None,
            "translations": list_translations(folder),
            "cover": (folder / "cover.jpg").exists(),
        }
        set_phase(job, "done", "done")
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        job["error"] = str(exc)
        set_phase(job, "error", "failed", error=str(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Download queue (server-side, persisted, self-advancing)
# ---------------------------------------------------------------------------

def _save_dl() -> None:
    DL_QUEUE_FILE.write_text(
        json.dumps({"items": dl_items}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_dl() -> None:
    try:
        dl_items.extend(json.loads(DL_QUEUE_FILE.read_text(encoding="utf-8"))["items"])
    except Exception:
        pass


def _record_done(query: str, status: str, song_id: str | None = None,
                 error: str | None = None) -> None:
    dl_done.append({"id": "dd_" + uuid.uuid4().hex[:8], "query": query,
                    "status": status, "song_id": song_id, "error": error})
    del dl_done[:-20]


def resolve_query(query: str):
    """Shared resolution: bare video id / URL / smart search."""
    bare_id = re.fullmatch(r"[A-Za-z0-9_-]{11}", query)
    if bare_id or URL_RE.match(query):
        if bare_id:
            video_id = query
        else:
            m = VIDEO_ID_RE.search(query)
            video_id = m.group(1) if m else None
        url = f"https://www.youtube.com/watch?v={video_id}" if video_id else query
        return url, video_id, None, ["url"], {"k": "directUrl", "v": {}}
    url, entry, reason = smart_pick(query)
    picked = {"k": "picked", "v": {"title": entry.get("title"),
                                   "channel": entry.get("channel") or "?",
                                   "reasons": reason}}
    return url, entry.get("id"), f"{query} {entry.get('title') or ''}", reason, picked


def _launch(query: str, url: str, reason: list, langs: list[str], picked: dict) -> str | None:
    """Create and start a job unless another one is already running."""
    with jobs_lock:
        if any(j["phase"] not in ("done", "error") for j in jobs.values()):
            return None
        job_id = uuid.uuid4().hex[:12]
        jobs[job_id] = {
            "id": job_id, "query": query, "phase": "queued",
            "log": [picked], "video": None,
            "result": None, "error": None, "started": time.time(),
        }
    threading.Thread(target=_job_runner, args=(job_id, query, url, reason, langs),
                     daemon=True).start()
    return job_id


def _job_runner(job_id: str, query: str, url: str, reason: list, langs: list[str]) -> None:
    try:
        run_job(job_id, query, url, reason, langs)
    finally:
        _on_job_end(job_id)


def _on_job_end(job_id: str) -> None:
    global current_item
    job = jobs.get(job_id)
    with dl_lock:
        item = current_item
        current_item = None
        if item is not None:
            ok = job and job["phase"] == "done"
            _record_done(item["query"], "done" if ok else "error",
                         (job.get("result") or {}).get("id") if job else None,
                         job.get("error") if job else None)
    maybe_start_next()


def maybe_start_next() -> None:
    """Advance the download queue whenever the worker is idle."""
    global current_item
    while True:
        with jobs_lock:
            if any(j["phase"] not in ("done", "error") for j in jobs.values()):
                return
        with dl_lock:
            if current_item is not None or not dl_items:
                return
            item = dl_items.pop(0)
            _save_dl()
            current_item = item
        try:
            url, video_id, text, reason, picked = resolve_query(item["query"])
            dups = find_duplicates(video_id, text)
            if dups:  # can't ask mid-queue: skip and report
                with dl_lock:
                    _record_done(item["query"], "duplicate", dups[0]["id"])
                    current_item = None
                continue
            if _launch(item["query"], url, reason, item.get("langs") or [], picked) is None:
                with dl_lock:  # a user job sneaked in: put the item back
                    dl_items.insert(0, item)
                    _save_dl()
                    current_item = None
                return
            return
        except Exception as exc:
            with dl_lock:
                _record_done(item["query"], "error", error=str(exc))
                current_item = None
            continue


@app.get("/api/queue")
def dlq_get():
    current = None
    with jobs_lock:
        for j in jobs.values():
            if j["phase"] not in ("done", "error"):
                current = {"id": j["id"], "query": j["query"], "phase": j["phase"]}
                break
    with dl_lock:
        return jsonify({"items": list(dl_items), "done": list(dl_done), "current": current})


@app.post("/api/queue")
def dlq_add():
    data = request.get_json(silent=True) or {}
    queries = [q.strip() for q in (data.get("queries") or []) if q and q.strip()]
    langs = [str(l) for l in (data.get("langs") or [])][:6]
    if not queries:
        return jsonify({"error": "empty"}), 400
    with dl_lock:
        for q in queries:
            dl_items.append({"id": "dq_" + uuid.uuid4().hex[:8], "query": q, "langs": langs})
        _save_dl()
    threading.Thread(target=maybe_start_next, daemon=True).start()
    return dlq_get()


@app.post("/api/queue/<qid>")
def dlq_edit(qid: str):
    data = request.get_json(silent=True) or {}
    with dl_lock:
        idx = next((i for i, it in enumerate(dl_items) if it["id"] == qid), None)
        if idx is None:
            return jsonify({"error": "not found"}), 404
        if (data.get("query") or "").strip():
            dl_items[idx]["query"] = data["query"].strip()
        move = data.get("move")
        if move in (-1, 1):
            j = idx + move
            if 0 <= j < len(dl_items):
                dl_items[idx], dl_items[j] = dl_items[j], dl_items[idx]
        _save_dl()
    return dlq_get()


@app.delete("/api/queue/<qid>")
def dlq_remove(qid: str):
    with dl_lock:
        dl_items[:] = [it for it in dl_items if it["id"] != qid]
        _save_dl()
    return dlq_get()


@app.delete("/api/queue")
def dlq_clear():
    with dl_lock:
        dl_items.clear()
        _save_dl()
    return dlq_get()


@app.delete("/api/queue/done")
def dlq_done_clear():
    """Clears the download history records only — never the songs."""
    with dl_lock:
        dl_done.clear()
    return dlq_get()


@app.delete("/api/queue/done/<did>")
def dlq_done_remove(did: str):
    with dl_lock:
        dl_done[:] = [d for d in dl_done if d["id"] != did]
    return dlq_get()


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.post("/api/process")
def process():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    force = bool(data.get("force"))
    langs = [str(l) for l in (data.get("langs") or [])][:6]
    if not query:
        return jsonify({"error": "empty query"}), 400

    with jobs_lock:
        if any(j["phase"] not in ("done", "error") for j in jobs.values()):
            return jsonify({"error": "a job is already running"}), 409

    # Resolve the query first so duplicate detection can use the video id
    try:
        url, video_id, text, reason, picked = resolve_query(query)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if not force:
        dups = find_duplicates(video_id, text)
        if dups:
            return jsonify({"duplicates": dups})

    job_id = _launch(query, url, reason, langs, picked)
    if job_id is None:
        return jsonify({"error": "a job is already running"}), 409
    return jsonify({"id": job_id})


@app.post("/api/library/<set_id>/translate")
def translate_endpoint(set_id: str):
    if not SAFE_ID_RE.match(set_id) or not (LIBRARY / set_id).is_dir():
        return jsonify({"error": "not found"}), 404
    langs = [str(l) for l in ((request.get_json(silent=True) or {}).get("langs") or [])][:6]
    if not langs:
        return jsonify({"error": "no languages given"}), 400
    try:
        result = translate_set(LIBRARY / set_id, langs)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    result["translations"] = list_translations(LIBRARY / set_id)
    return jsonify(result)


@app.post("/api/library/<set_id>/lyrics/custom")
def create_custom_lyrics(set_id: str):
    """Duplicate an existing lyrics tab as a named custom variant."""
    folder = LIBRARY / set_id
    if not SAFE_ID_RE.match(set_id) or not folder.is_dir():
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "empty name"}), 400
    if _norm_lang_name(name) in _OFFICIAL_LANG_NAMES:
        return jsonify({"error": "official name"}), 409
    slug = "x-" + slugify(name)[:30].strip("-")
    if slug == "x-":
        return jsonify({"error": "empty name"}), 400
    target = folder / f"lyrics.{slug}.txt"
    if target.exists():
        return jsonify({"error": "already exists"}), 409
    source = (data.get("source") or "").strip().lower()
    if source and not LANG_CODE_RE.match(source):
        return jsonify({"error": "bad source"}), 400
    src_file = folder / (f"lyrics.{source}.txt" if source else "lyrics.txt")
    if not src_file.exists():
        return jsonify({"error": "source not found"}), 404
    target.write_text(src_file.read_text(encoding="utf-8"), encoding="utf-8")
    return jsonify({"ok": True, "code": slug, "translations": list_translations(folder)})


@app.delete("/api/library/<set_id>/translate/<lang>")
def delete_translation(set_id: str, lang: str):
    if not SAFE_ID_RE.match(set_id) or not LANG_CODE_RE.match(lang):
        return jsonify({"error": "bad request"}), 400
    target = LIBRARY / set_id / f"lyrics.{lang}.txt"
    if not target.exists():
        return jsonify({"error": "not found"}), 404
    target.unlink()
    return jsonify({"ok": True, "translations": list_translations(LIBRARY / set_id)})


@app.get("/api/status/<job_id>")
def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({**job, "elapsed": round(time.time() - job["started"], 1)})


def upsert_env_var(key: str, value: str) -> None:
    env_file = BASE / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    lines = [l for l in lines if not l.strip().startswith(key + "=")]
    lines.append(f"{key}={value}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _validate_key(pid: str, key: str) -> str | None:
    """Return an error string if the key is definitely bad, else None."""
    import requests as rq
    try:
        if pid == "claude":
            import anthropic
            try:
                anthropic.Anthropic(api_key=key).models.retrieve("claude-haiku-4-5")
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
                return "that API key was rejected by Anthropic"
        elif pid == "openai":
            r = rq.get("https://api.openai.com/v1/models",
                       headers={"Authorization": f"Bearer {key}"}, timeout=15)
            if r.status_code == 401:
                return "that API key was rejected by OpenAI"
        elif pid == "gemini":
            r = rq.get("https://generativelanguage.googleapis.com/v1beta/models",
                       params={"key": key, "pageSize": 1}, timeout=15)
            if r.status_code in (400, 401, 403):
                return "that API key was rejected by Google (Gemini)"
    except Exception:
        pass  # network hiccup — accept the key, it will be retried on use
    return None


@app.get("/api/settings")
def get_settings():
    providers = []
    for pid, cfg in tr.PROVIDERS.items():
        key = tr.provider_key(pid)
        providers.append({
            "id": pid,
            "label": cfg["label"],
            "model": cfg["model"](),
            "key_set": bool(key),
            "hint": f"{key[:8]}…{key[-4:]}" if len(key) > 16 else None,
        })
    return jsonify({
        "engine_selected": tr.selected_engine_setting(),
        "engine": tr.active_engine(),
        "translate_mode": tr.translate_mode(),
        "providers": providers,
        "default_cover": (
            f"/static/default-cover.jpg?ts={int(DEFAULT_COVER.stat().st_mtime)}"
            if DEFAULT_COVER.exists() else None
        ),
        "lan_url": lan_url(),
        "admin_url": "http://127.0.0.1:5056",
    })


@app.get("/api/browse")
def browse():
    """List subdirectories so the UI can offer a folder picker for exports."""
    raw = request.args.get("path") or "~"
    path = Path(os.path.expanduser(raw)).resolve()
    if not path.is_dir():
        path = Path.home()
    try:
        dirs = sorted(
            (d.name for d in path.iterdir() if d.is_dir() and not d.name.startswith(".")),
            key=str.lower,
        )[:300]
    except PermissionError:
        dirs = []
    parent = str(path.parent) if path.parent != path else None
    return jsonify({"path": str(path), "parent": parent, "dirs": dirs})


@app.post("/api/settings")
def save_settings():
    data = request.get_json(silent=True) or {}

    engine = data.get("engine")
    if engine is not None:
        if engine not in ("auto", "google", *tr.PROVIDERS):
            return jsonify({"error": "unknown engine"}), 400
        upsert_env_var(tr.ENGINE_ENV, engine)
        os.environ[tr.ENGINE_ENV] = engine

    mode = data.get("translate_mode")
    if mode is not None:
        if mode not in ("regular", "syllables"):
            return jsonify({"error": "unknown translate mode"}), 400
        upsert_env_var(tr.MODE_ENV, mode)
        os.environ[tr.MODE_ENV] = mode

    cover_url = (data.get("cover_url") or "").strip()
    if cover_url:
        try:
            r = requests.get(cover_url, timeout=20)
            r.raise_for_status()
            save_square_image(r.content, DEFAULT_COVER)
        except Exception as exc:
            return jsonify({"error": f"could not fetch that image: {exc}"}), 400

    key = (data.get("api_key") or "").strip()
    if key:
        pid = data.get("provider") or "claude"
        if pid not in tr.PROVIDERS:
            return jsonify({"error": "unknown provider"}), 400
        error = _validate_key(pid, key)
        if error:
            return jsonify({"error": error}), 400
        env_name = tr.PROVIDERS[pid]["key_env"]
        upsert_env_var(env_name, key)
        os.environ[env_name] = key

    tr.reset_backend()
    return get_settings()


@app.get("/api/library")
def library():
    return jsonify(all_sets())


@app.delete("/api/library/<set_id>")
def delete_set(set_id: str):
    if not SAFE_ID_RE.match(set_id):
        return jsonify({"error": "bad id"}), 400
    folder = LIBRARY / set_id
    if not folder.is_dir():
        return jsonify({"error": "not found"}), 404
    shutil.rmtree(folder)

    def scrub(playlists):
        for pl in playlists:
            pl["songs"] = [s for s in pl["songs"] if s != set_id]

    mutate_playlists(scrub)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

def _find_pl(playlists: list[dict], pid: str) -> dict:
    for pl in playlists:
        if pl["id"] == pid:
            return pl
    raise ValueError("playlist not found")


@app.get("/api/playlists")
def playlists_list():
    return jsonify(load_playlists())


@app.post("/api/playlists")
def playlists_create():
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "empty name"}), 400
    new = {"id": "pl_" + uuid.uuid4().hex[:8], "name": name, "songs": [],
           "created": datetime.now().isoformat(timespec="seconds")}
    return jsonify(mutate_playlists(lambda pls: pls.append(new)))


@app.post("/api/playlists/<pid>")
def playlists_update(pid: str):
    data = request.get_json(silent=True) or {}

    def update(playlists):
        pl = _find_pl(playlists, pid)
        if (data.get("name") or "").strip():
            pl["name"] = data["name"].strip()
        if isinstance(data.get("songs"), list):
            existing = _existing_song_ids()
            seen: list[str] = []
            for s in data["songs"]:
                if s in existing and s not in seen:
                    seen.append(s)
            pl["songs"] = seen

    try:
        return jsonify(mutate_playlists(update))
    except ValueError:
        return jsonify({"error": "not found"}), 404


@app.delete("/api/playlists/<pid>")
def playlists_delete(pid: str):
    def remove(playlists):
        playlists[:] = [p for p in playlists if p["id"] != pid]

    return jsonify(mutate_playlists(remove))


@app.post("/api/playlists/<pid>/duplicate")
def playlists_duplicate(pid: str):
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()

    def dup(playlists):
        src = _find_pl(playlists, pid)
        playlists.append({
            "id": "pl_" + uuid.uuid4().hex[:8],
            "name": name or (src["name"] + " (copy)"),
            "songs": list(src["songs"]),
            "created": datetime.now().isoformat(timespec="seconds"),
        })

    try:
        return jsonify(mutate_playlists(dup))
    except ValueError:
        return jsonify({"error": "not found"}), 404


@app.post("/api/playlists/<pid>/songs")
def playlists_add_song(pid: str):
    song = ((request.get_json(silent=True) or {}).get("song_id") or "").strip()
    if song not in _existing_song_ids():
        return jsonify({"error": "song not found"}), 404

    def add(playlists):
        pl = _find_pl(playlists, pid)
        if song in pl["songs"]:
            raise ValueError("already in playlist")
        pl["songs"].append(song)

    try:
        return jsonify(mutate_playlists(add))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409


PL_ID_RE = re.compile(r"^(library|pl_[a-z0-9]+)$")


def incoming_image() -> bytes | None:
    """Image bytes from an uploaded file, or from a JSON {url} as fallback."""
    file = request.files.get("file")
    if file:
        return file.read()
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if url:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        return r.content
    return None


@app.post("/api/playlists/<pid>/cover")
def playlists_cover(pid: str):
    """Set a playlist's cover from an uploaded file ('library' is a valid pid)."""
    if not PL_ID_RE.match(pid):
        return jsonify({"error": "bad id"}), 400
    if pid != "library" and not any(p["id"] == pid for p in load_playlists()):
        return jsonify({"error": "not found"}), 404
    try:
        data = incoming_image()
        if not data:
            return jsonify({"error": "no image given"}), 400
        PLCOVERS.mkdir(exist_ok=True)
        save_square_image(data, PLCOVERS / f"{pid}.jpg")
    except Exception as exc:
        return jsonify({"error": f"could not use that image: {exc}"}), 400
    return jsonify(load_playlists())


@app.post("/api/settings/cover")
def settings_cover_upload():
    try:
        data = incoming_image()
        if not data:
            return jsonify({"error": "no image given"}), 400
        save_square_image(data, DEFAULT_COVER)
    except Exception as exc:
        return jsonify({"error": f"could not use that image: {exc}"}), 400
    return get_settings()


@app.post("/api/library/<set_id>/cover")
def song_cover(set_id: str):
    folder = LIBRARY / set_id
    if not SAFE_ID_RE.match(set_id) or not folder.is_dir():
        return jsonify({"error": "not found"}), 404
    try:
        data = incoming_image()
        if not data:
            return jsonify({"error": "no image given"}), 400
        save_square_image(data, folder / "cover.jpg")
    except Exception as exc:
        return jsonify({"error": f"could not use that image: {exc}"}), 400
    return jsonify({"ok": True})


@app.get("/api/library/<set_id>/timing")
def get_timing(set_id: str):
    """Singing-mode timing, computed lazily on first request for old songs."""
    folder = LIBRARY / set_id
    if not SAFE_ID_RE.match(set_id) or not folder.is_dir():
        return jsonify({"error": "not found"}), 404
    meta_file = folder / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    data = timing_mod.ensure_timing(folder, meta)
    if not data:
        return jsonify({"error": "no timing available"}), 404
    return jsonify(data)


@app.post("/api/library/<set_id>/timing/rebuild")
def rebuild_timing(set_id: str):
    """Recompute timing.json from scratch, exactly like a fresh download."""
    folder = LIBRARY / set_id
    if not SAFE_ID_RE.match(set_id) or not folder.is_dir():
        return jsonify({"error": "not found"}), 404
    (folder / "timing.json").unlink(missing_ok=True)
    meta_file = folder / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    data = timing_mod.ensure_timing(folder, meta)  # also re-fetches an LRC if missing
    if not data:
        return jsonify({"error": "no timing available"}), 404
    return jsonify(data)


@app.post("/api/library/<set_id>/sing_offset")
def set_sing_offset(set_id: str):
    """Per-song singing-mode sync nudge, tied to the timing version+method."""
    folder = LIBRARY / set_id
    if not SAFE_ID_RE.match(set_id) or not folder.is_dir():
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    try:
        offset = round(float(data.get("offset", 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "bad offset"}), 400
    key = str(data.get("key") or "")[:40]
    meta_file = folder / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    if offset == 0:
        meta.pop("sing_offset", None)
        meta.pop("sing_offset_key", None)
    else:
        meta["sing_offset"] = offset
        meta["sing_offset_key"] = key
    write_meta(folder, meta)
    return jsonify({"ok": True})


@app.post("/api/library/<set_id>/timing/manual")
def manual_timing(set_id: str):
    """Store a hand-recorded timing (one spacebar tap per syllable)."""
    folder = LIBRARY / set_id
    if not SAFE_ID_RE.match(set_id) or not folder.is_dir():
        return jsonify({"error": "not found"}), 404
    lines_in = (request.get_json(silent=True) or {}).get("lines")
    if not isinstance(lines_in, list) or not lines_in:
        return jsonify({"error": "no lines"}), 400
    lines = []
    try:
        for ln in lines_in:
            slots = sorted(round(float(x), 2) for x in (ln.get("slots") or []))[:80]
            s = round(float(ln.get("s", slots[0] if slots else 0.0)), 2)
            e = round(float(ln.get("e", slots[-1] if slots else s)), 2)
            lines.append({"s": s, "e": max(e, s + 0.4), "slots": slots})
    except (TypeError, ValueError):
        return jsonify({"error": "bad line data"}), 400
    data = {"version": timing_mod.TIMING_VERSION, "method": "manual", "lines": lines}
    (folder / "timing.json").write_text(json.dumps(data), encoding="utf-8")
    return jsonify(data)


@app.post("/api/library/<set_id>/edit")
def edit_song(set_id: str):
    folder = LIBRARY / set_id
    if not SAFE_ID_RE.match(set_id) or not folder.is_dir():
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    meta_file = folder / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    if (data.get("title") or "").strip():
        meta["title"] = data["title"].strip()
    if "artist" in data:
        meta["artist"] = (data.get("artist") or "").strip() or None
    write_meta(folder, meta)
    return jsonify({"ok": True, "title": meta.get("title"), "artist": meta.get("artist")})


@app.post("/api/library/<set_id>/lyrics")
def edit_lyrics(set_id: str):
    folder = LIBRARY / set_id
    if not SAFE_ID_RE.match(set_id) or not folder.is_dir():
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    lang = (data.get("lang") or "").strip().lower()
    text = (data.get("text") or "").rstrip()
    if not text:
        return jsonify({"error": "empty lyrics"}), 400
    if lang and not LANG_CODE_RE.match(lang):
        return jsonify({"error": "bad language"}), 400
    name = f"lyrics.{lang}.txt" if lang else "lyrics.txt"
    (folder / name).write_text(text + "\n", encoding="utf-8")

    result = {"ok": True}
    if not lang:  # original edited: re-detect its language
        (folder / "timing.json").unlink(missing_ok=True)  # line count may change
        _, body = split_lyrics_header(text)
        detected = tr.detect_language(body)
        if detected:
            meta_file = folder / "meta.json"
            meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
            meta["lyrics_lang"] = detected
            write_meta(folder, meta)
            result["lyrics_lang"] = detected
    return jsonify(result)


# ---------------------------------------------------------------------------
# LAN party mode: roles, profiles and live sing-mode messages
# ---------------------------------------------------------------------------

def _save_session() -> None:
    SESSION_FILE.write_text(
        json.dumps(session_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_session() -> None:
    try:
        session_state.update(json.loads(SESSION_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    session_state.setdefault("party", {})
    for key, default in (("allow_comments", True), ("show_scores", True), ("score_wait", 10)):
        session_state["party"].setdefault(key, default)


def lan_url() -> str:
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"
    return f"http://{ip}:5056"


LOCAL_ADDRS = ("127.0.0.1", "::1")


def _is_local_request() -> bool:
    """Only the machine running the server can reach it via localhost, so
    locality IS the admin credential: two URLs, two roles."""
    return request.remote_addr in LOCAL_ADDRS


def _session_payload(cid: str) -> dict:
    user = session_state["users"].get(cid, {})
    return {
        "role": "admin" if _is_local_request() else "spectator",
        "alias": user.get("alias"),
        "has_avatar": (AVATARS / f"{cid}.jpg").exists(),
    }


@app.post("/api/session/join")
def session_join():
    cid = ((request.get_json(silent=True) or {}).get("client_id") or "").strip()
    if not CID_RE.match(cid):
        return jsonify({"error": "bad client id"}), 400
    return jsonify(_session_payload(cid))


@app.post("/api/session/profile")
def session_profile():
    cid = (request.form.get("client_id") or "").strip()
    alias = (request.form.get("alias") or "").strip()[:24]
    if not CID_RE.match(cid) or not alias:
        return jsonify({"error": "alias required"}), 400
    file = request.files.get("file")
    if file:
        try:
            AVATARS.mkdir(exist_ok=True)
            save_square_image(file.read(), AVATARS / f"{cid}.jpg", size=128)
        except Exception as exc:
            return jsonify({"error": f"bad avatar image: {exc}"}), 400
    with session_lock:
        session_state["users"][cid] = {"alias": alias}
        _save_session()
    return jsonify(_session_payload(cid))


@app.get("/avatar/<cid>")
def avatar(cid: str):
    if not CID_RE.match(cid) or not (AVATARS / f"{cid}.jpg").exists():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(AVATARS, f"{cid}.jpg", conditional=True)


@app.post("/api/messages")
def messages_post():
    global _msg_seq
    data = request.get_json(silent=True) or {}
    cid = (data.get("client_id") or "").strip()
    kind = "reaction" if data.get("kind") == "reaction" else "text"
    # reactions are a single emoji (ZWJ sequences can span ~16 UTF-16 units)
    text = (data.get("text") or "").strip()[:16 if kind == "reaction" else 120]
    if not CID_RE.match(cid) or not text:
        return jsonify({"error": "bad message"}), 400
    alias = session_state["users"].get(cid, {}).get("alias")
    if not alias:
        return jsonify({"error": "join first"}), 403
    # emoji reactions stay available even when free-text comments are off
    if kind == "text" and not session_state["party"].get("allow_comments", True):
        return jsonify({"error": "comments disabled"}), 403
    with session_lock:
        _msg_seq += 1
        live_messages.append({
            "id": _msg_seq, "cid": cid, "alias": alias, "text": text, "kind": kind,
            "has_avatar": (AVATARS / f"{cid}.jpg").exists(),
        })
        del live_messages[:-100]
    return jsonify({"ok": True, "id": _msg_seq})


# ---- star voting (one vote per device, changeable, per song round) ----

@app.post("/api/votes/round")
def votes_new_round():
    if not _is_local_request():
        return jsonify({"error": "admin only"}), 403
    with session_lock:
        vote_state["round"] += 1
        vote_state["votes"] = {}
    return jsonify({"round": vote_state["round"]})


@app.post("/api/votes")
def votes_post():
    data = request.get_json(silent=True) or {}
    cid = (data.get("client_id") or "").strip()
    stars = data.get("stars")
    if not CID_RE.match(cid) or not isinstance(stars, int) or not 0 <= stars <= 5:
        return jsonify({"error": "bad vote"}), 400
    if not session_state["users"].get(cid, {}).get("alias"):
        return jsonify({"error": "join first"}), 403
    with session_lock:
        if stars == 0:  # tapping the current score again withdraws the vote
            vote_state["votes"].pop(cid, None)
        else:
            vote_state["votes"][cid] = stars
    return jsonify({"ok": True, "round": vote_state["round"]})


@app.get("/api/votes")
def votes_get():
    votes = list(vote_state["votes"].values())
    return jsonify({
        "round": vote_state["round"],
        "count": len(votes),
        "mean": round(sum(votes) / len(votes), 2) if votes else None,
    })


@app.get("/api/party")
def party_get():
    cid = (request.args.get("client_id") or "").strip()
    return jsonify({
        **session_state["party"],
        "round": vote_state["round"],
        "my_vote": vote_state["votes"].get(cid),
        "lan_url": lan_url(),
    })


@app.post("/api/session/party")
def party_set():
    data = request.get_json(silent=True) or {}
    if not _is_local_request():
        return jsonify({"error": "admin only"}), 403
    with session_lock:
        for key in ("allow_comments", "show_scores"):
            if isinstance(data.get(key), bool):
                session_state["party"][key] = data[key]
        if isinstance(data.get("score_wait"), int):
            session_state["party"]["score_wait"] = max(3, min(60, data["score_wait"]))
        _save_session()
    return party_get()


@app.get("/api/messages")
def messages_get():
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0
    fresh = [m for m in live_messages if m["id"] > since][-30:]
    return jsonify({"messages": fresh, "last": _msg_seq})


# ---------------------------------------------------------------------------
# Playlist sharing: export / import as ZIP
# ---------------------------------------------------------------------------

_SHARE_EXTRA_FILES = ("meta.json", "timing.json", "lyrics.lrc")
_STORED_EXTS = {".mp3", ".jpg"}  # already compressed: repack without deflate


def _share_file_ok(name: str) -> bool:
    return allowed_file(name) or name in _SHARE_EXTRA_FILES


@app.get("/api/playlists/<pid>/export")
def playlist_export(pid: str):
    if pid == "library":
        name = "Library"
        song_ids = [m["id"] for m in reversed(all_sets())]  # chronological
    else:
        pl = next((p for p in load_playlists() if p["id"] == pid), None)
        if not pl:
            return jsonify({"error": "not found"}), 404
        name, song_ids = pl["name"], list(pl["songs"])

    stamp = datetime.now().isoformat(timespec="seconds")
    manifest = {"format": 1, "name": name, "exported_at": stamp, "songs": []}
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir=str(BASE))
    try:
        with zipfile.ZipFile(tmp, "w") as z:
            for sid in song_ids:
                folder = LIBRARY / sid
                if not is_complete_song(folder):
                    continue
                meta_file = folder / "meta.json"
                meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
                manifest["songs"].append({
                    "id": sid, "video_id": meta.get("video_id"),
                    "title": meta.get("title"), "artist": meta.get("artist"),
                })
                for f in sorted(folder.iterdir()):
                    if f.is_file() and _share_file_ok(f.name):
                        comp = zipfile.ZIP_STORED if f.suffix in _STORED_EXTS else zipfile.ZIP_DEFLATED
                        z.write(f, f"songs/{sid}/{f.name}", compress_type=comp)
            z.writestr("manifest.json",
                       json.dumps(manifest, ensure_ascii=False, indent=2),
                       compress_type=zipfile.ZIP_DEFLATED)
        tmp.close()
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    safe = re.sub(r"[^\w \-]", "", name).strip() or "playlist"
    response = send_file(tmp.name, as_attachment=True,
                         download_name=f"{safe} {stamp[:10]}.zip",
                         mimetype="application/zip")
    response.call_on_close(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
    return response


@app.post("/api/import")
def playlist_import():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "no file"}), 400
    workdir = Path(tempfile.mkdtemp(prefix=".tmp-import-", dir=str(LIBRARY)))
    try:
        zip_path = workdir / "in.zip"
        file.save(zip_path)
        with zipfile.ZipFile(zip_path) as z:
            if any(n.startswith("/") or ".." in n for n in z.namelist()):
                return jsonify({"error": "unsafe zip"}), 400
            z.extractall(workdir / "x")
        root = workdir / "x"
        # Safari auto-unzips downloads; re-compressing with Finder nests
        # everything in a top-level folder and adds __MACOSX junk. Find the
        # real root by locating manifest.json wherever it landed.
        if not (root / "manifest.json").exists():
            hits = [p for p in root.rglob("manifest.json") if "__MACOSX" not in p.parts]
            if not hits:
                return jsonify({"error": "manifest.json not found in zip"}), 400
            root = hits[0].parent
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

        existing = all_sets()
        by_video = {m["video_id"]: m["id"] for m in existing if m.get("video_id")}
        key = lambda t, a: _norm_lang_name(f"{t or ''}|{a or ''}")
        by_name = {key(m.get("title"), m.get("artist")): m["id"] for m in existing}

        ordered, new_count, reused = [], 0, 0
        base_time = datetime.now()
        for i, song in enumerate(manifest.get("songs", [])):
            # reuse an already-present song instead of importing a duplicate
            match = by_video.get(song.get("video_id")) or by_name.get(key(song.get("title"), song.get("artist")))
            if match:
                ordered.append(match)
                reused += 1
                continue
            src = root / "songs" / song["id"]
            if not ((src / "original.mp3").exists() and (src / "instrumental.mp3").exists()):
                continue
            slug = slugify(f"{song.get('artist') or ''} {song.get('title') or song['id']}") or song["id"]
            dest = unique_folder(slug)
            dest.mkdir(parents=True)
            for f in src.iterdir():
                # skip .DS_Store / ._AppleDouble droppings from Finder zips
                if f.is_file() and _share_file_ok(f.name) and not f.name.startswith("."):
                    shutil.copy2(f, dest / f.name)
            meta_file = dest / "meta.json"
            meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
            # sequential timestamps: they appear as freshly downloaded, in order
            meta["created"] = (base_time + timedelta(seconds=i)).isoformat(timespec="seconds")
            write_meta(dest, meta)
            if meta.get("video_id"):
                by_video[meta["video_id"]] = dest.name
            by_name[key(meta.get("title"), meta.get("artist"))] = dest.name
            ordered.append(dest.name)
            new_count += 1

        pl_name = f"{manifest.get('name') or 'Imported'} ({(manifest.get('exported_at') or '')[:10]})".strip()
        new_pl = {"id": "pl_" + uuid.uuid4().hex[:8], "name": pl_name, "songs": ordered,
                  "created": datetime.now().isoformat(timespec="seconds")}
        mutate_playlists(lambda pls: pls.append(new_pl))
        return jsonify({"ok": True, "playlist": pl_name, "playlist_id": new_pl["id"],
                        "new": new_count, "reused": reused})
    except zipfile.BadZipFile:
        return jsonify({"error": "not a valid zip"}), 400
    except Exception as exc:
        import traceback
        traceback.print_exc()  # keep the real cause visible in the server log
        return jsonify({"error": str(exc)}), 400
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.get("/plcover/<pid>")
def plcover(pid: str):
    if not PL_ID_RE.match(pid) or not (PLCOVERS / f"{pid}.jpg").exists():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(PLCOVERS, f"{pid}.jpg", conditional=True)


@app.delete("/api/playlists/<pid>/songs/<song>")
def playlists_remove_song(pid: str, song: str):
    def remove(playlists):
        pl = _find_pl(playlists, pid)
        pl["songs"] = [s for s in pl["songs"] if s != song]

    try:
        return jsonify(mutate_playlists(remove))
    except ValueError:
        return jsonify({"error": "not found"}), 404


# official codes like "ca" / "zh-cn", or custom variants prefixed "x-"
LANG_CODE_RE = re.compile(r"^(?:[a-z]{2,3}(?:-[a-z]{2,4})?|x-[a-z0-9-]{1,40})$")

# names a custom variant may NOT take (official languages, any spelling)
_OFFICIAL_LANG_NAMES = set(tr.LANG_NAMES) | {
    "english", "spanish", "espanol", "catalan", "catala", "french", "francais",
    "german", "deutsch", "italian", "italiano", "portuguese", "portugues",
    "japanese", "dutch", "nederlands", "polish", "polski", "russian",
    "swedish", "svenska", "korean", "chinese",
} | {re.sub(r"[^a-z0-9]", "", n.lower()) for n in tr.LANG_NAMES.values()}


def _norm_lang_name(name: str) -> str:
    flat = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", flat.lower())


def allowed_file(name: str) -> bool:
    if name in ("original.mp3", "instrumental.mp3", "lyrics.txt", "cover.jpg"):
        return True
    m = re.fullmatch(r"lyrics\.(.+)\.txt", name)
    return bool(m and LANG_CODE_RE.match(m.group(1)))


@app.get("/files/<set_id>/<name>")
def files(set_id: str, name: str):
    if not SAFE_ID_RE.match(set_id) or not allowed_file(name):
        return jsonify({"error": "bad path"}), 400
    # conditional=True enables HTTP Range requests, needed for audio seeking
    return send_from_directory(LIBRARY / set_id, name, conditional=True)


@app.post("/api/library/<set_id>/export")
def export_set(set_id: str):
    """Copy files of a saved song to a destination folder on this machine."""
    folder = LIBRARY / set_id
    if not SAFE_ID_RE.match(set_id) or not folder.is_dir():
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    dest = (data.get("dest") or "").strip()
    names = [n for n in (data.get("files") or []) if allowed_file(n)]
    if not dest:
        return jsonify({"error": "no destination folder given"}), 400
    if not names:
        return jsonify({"error": "no files selected"}), 400
    dest_dir = Path(os.path.expanduser(dest))
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return jsonify({"error": f"cannot use destination folder: {exc}"}), 400

    meta_file = folder / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    base = " - ".join(filter(None, [meta.get("artist"), meta.get("title")])) or set_id
    base = re.sub(r'[\\/:*?"<>|]', "", base).strip() or set_id

    exported = []
    for name in names:
        src = folder / name
        if not src.exists():
            continue
        if name == "original.mp3":
            out = f"{base}.mp3"
        elif name == "instrumental.mp3":
            out = f"{base} (instrumental).mp3"
        elif name == "lyrics.txt":
            out = f"{base} (lyrics).txt"
        elif name == "cover.jpg":
            out = f"{base} (cover).jpg"
        else:
            code = name.split(".", 2)[1]
            label = code[2:].replace("-", " ").title() if code.startswith("x-") else code.upper()
            out = f"{base} (lyrics {label}).txt"
        shutil.copy2(src, dest_dir / out)
        exported.append(out)
    return jsonify({"exported": exported, "dest": str(dest_dir)})


if __name__ == "__main__":
    LIBRARY.mkdir(exist_ok=True)
    PLCOVERS.mkdir(exist_ok=True)
    cleanup_tmp()
    cleanup_incomplete()
    migrate_legacy()
    repair_metadata()
    backfill_covers()
    backfill_lyrics_lang()
    _load_dl()
    if dl_items:  # resume a pending download queue after a restart
        threading.Timer(3.0, maybe_start_next).start()
    AVATARS.mkdir(exist_ok=True)
    _load_session()
    print("Admin (this computer): http://127.0.0.1:5056")
    print(f"Guests (same Wi-Fi):   {lan_url()}")
    app.run(host="0.0.0.0", port=5056, debug=False)
