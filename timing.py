"""Singing-mode timing: when each syllable of a song is sung.

The vocals track is recovered as original - instrumental (the two MP3s are
sample-aligned twins, so plain subtraction isolates the voice well enough for
envelope analysis). An RMS envelope plus onset detection over the vocals give
"sung syllable slots"; line windows come from lrclib's synced lyrics (LRC,
saved as lyrics.lrc) when available, else from the vocal activity itself.

Output: timing.json  ->  {"version": 1, "lines": [{"s", "e", "slots": [...]}]}
Entries are aligned 1:1, in order, with the non-empty lines of lyrics.txt's
body. The frontend maps text syllables of any language onto the slots.
"""

import difflib
import json
import os
import re
from pathlib import Path

import numpy as np
import soundfile as sf

HOP = 512  # ~11.6 ms per frame at 44.1 kHz


def _mono(path: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return data.mean(axis=1), sr


def _strip_header(raw: str) -> str:
    lines = raw.splitlines()
    if len(lines) >= 2 and lines[1].startswith("["):
        i = 2
        while i < len(lines) and (
            not lines[i].strip()
            or (i < 5 and lines[i].startswith("[") and lines[i].rstrip().endswith("]"))
        ):
            i += 1
        return "\n".join(lines[i:]).strip()
    return raw.strip()


TIMING_VERSION = 8


def _interp(vals: list) -> list[float] | None:
    """Fill None gaps by linear interpolation between known values."""
    idx = [i for i, v in enumerate(vals) if v is not None]
    if not idx:
        return None
    first, last = idx[0], idx[-1]
    for a, b in zip(idx[:-1], idx[1:]):
        span = vals[b] - vals[a]
        for j in range(a + 1, b):
            vals[j] = vals[a] + span * (j - a) / (b - a)
    avg = (vals[last] - vals[first]) / max(1, last - first) if last > first else 3.0
    for j in range(first - 1, -1, -1):
        vals[j] = max(0.0, vals[j + 1] - avg)
    for j in range(last + 1, len(vals)):
        vals[j] = vals[j - 1] + avg
    return vals


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """(timestamp_seconds, text) of non-empty LRC lines, sorted by time."""
    entries = []
    for line in text.splitlines():
        m = re.match(r"\s*\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", line)
        if m and m.group(3).strip():
            entries.append((60 * int(m.group(1)) + float(m.group(2)), m.group(3).strip()))
    entries.sort(key=lambda e: e[0])
    return entries


def _norm_line(s: str) -> str:
    return re.sub(r"[^\w]+", "", s.lower())


def align_lines_to_lrc(text_lines: list[str], entries: list[tuple[float, str]]) -> list[float | None]:
    """Per-line start times by matching text content against the LRC.

    Sequential greedy matching with a bounded look-ahead (so repeated
    choruses match their own occurrence); unmatched lines are interpolated
    between their matched neighbours.
    """
    n, m = len(text_lines), len(entries)
    starts: list[float | None] = [None] * n
    j = 0
    for i, line in enumerate(text_lines):
        key = _norm_line(line)
        if not key:
            continue
        for jj in range(j, min(j + 8, m)):
            cand = _norm_line(entries[jj][1])
            if cand == key or (len(key) > 8 and (key in cand or cand in key)):
                starts[i] = entries[jj][0]
                j = jj + 1
                break
    matched = sum(1 for s in starts if s is not None)
    if matched < max(2, n * 0.3):
        return [None] * n  # alignment failed; caller falls back

    # interpolate the gaps between matched anchors
    idx = [i for i, s in enumerate(starts) if s is not None]
    first, last = idx[0], idx[-1]
    for a, b in zip(idx[:-1], idx[1:]):
        span = starts[b] - starts[a]
        for k in range(a + 1, b):
            starts[k] = starts[a] + span * (k - a) / (b - a)
    avg = (starts[last] - starts[first]) / max(1, last - first)
    for k in range(first - 1, -1, -1):
        starts[k] = max(0.0, starts[k + 1] - avg)
    for k in range(last + 1, n):
        starts[k] = starts[k - 1] + avg
    return starts


def first_strong_vocal(env: np.ndarray, peak: float, tps: float) -> float | None:
    """First moment with sustained (>=0.3s) vocal energy above 30% of peak."""
    strong = env > 0.3 * peak
    need = max(1, int(0.3 / tps))
    run = 0
    for i, on in enumerate(strong):
        run = run + 1 if on else 0
        if run >= need:
            return (i - run + 1) * tps
    return None


def compute_timing(folder: Path, anchors: dict[int, float] | None = None) -> dict | None:
    lyrics_file = folder / "lyrics.txt"
    if not lyrics_file.exists():
        return None
    body = _strip_header(lyrics_file.read_text(encoding="utf-8"))
    text_lines = [l for l in body.splitlines() if l.strip()]
    if not text_lines:
        return None

    orig, sr = _mono(folder / "original.mp3")
    inst, _ = _mono(folder / "instrumental.mp3")
    n = min(len(orig), len(inst))
    vocals = orig[:n] - inst[:n]

    frames = n // HOP
    if frames < 20:
        return None
    rms = np.sqrt((vocals[: frames * HOP].reshape(frames, HOP) ** 2).mean(axis=1))
    env = np.convolve(rms, np.ones(5) / 5, mode="same")  # ~60 ms smoothing
    tps = HOP / sr
    peak = float(env.max())
    if peak < 1e-5:
        return None

    # voice activity + onset (positive energy flux) peaks
    threshold = max(0.06 * peak, float(np.percentile(env, 55)) * 0.6)
    active = env > threshold
    flux = np.diff(env, prepend=env[:1])
    flux[flux < 0] = 0
    flux_thr = float(np.percentile(flux[active], 70)) if active.any() else 1e-6
    min_gap = max(1, int(0.09 / tps))  # >= 90 ms between syllable onsets
    onsets: list[float] = []
    last = -min_gap
    for i in range(1, frames - 1):
        if (active[i] and flux[i] >= flux_thr
                and flux[i] >= flux[i - 1] and flux[i] >= flux[i + 1]
                and i - last >= min_gap):
            onsets.append(i * tps)
            last = i

    # line windows: synced LRC when available, else weighted split of the
    # voiced region by line length
    n_lines = len(text_lines)
    act_idx = np.where(active)[0]
    voice_start = float(act_idx[0]) * tps if len(act_idx) else 0.0
    voice_end = float(act_idx[-1]) * tps if len(act_idx) else frames * tps

    lrc_file = folder / "lyrics.lrc"
    entries = parse_lrc(lrc_file.read_text(encoding="utf-8")) if lrc_file.exists() else []

    method = "energy"
    windows: list[tuple[float, float]] = []
    starts = align_lines_to_lrc(text_lines, entries) if len(entries) >= 2 else [None] * n_lines
    have_anchors = anchors and len(anchors) >= 5

    if starts and starts[0] is not None and have_anchors:
        # Hybrid: dense LRC line structure calibrated by sparse Whisper word
        # anchors measured on OUR audio. A piecewise-interpolated offset also
        # absorbs drift between masters, not just a constant shift.
        offsets: list = [None] * n_lines
        for k, t in anchors.items():
            if 0 <= k < n_lines:
                offsets[k] = t - starts[k]
        offsets = _interp(offsets)
        if offsets:
            starts = [max(0.0, s + o) for s, o in zip(starts, offsets)]
            method = "hybrid"
    elif starts and starts[0] is not None:
        # LRC only: anchor the first lyric line to the first strong sustained
        # vocal, guarded so it can't push starts off the voiced audio.
        anchor = first_strong_vocal(env, peak, tps)
        if anchor is not None:
            shift = anchor - starts[0]
            duration = frames * tps

            def coverage(times: list[float]) -> float:
                hits = 0
                for s in times:
                    j = min(frames - 1, max(0, int(s / tps)))
                    if active[max(0, j - 4): j + 5].any():
                        hits += 1
                return hits / len(times)

            shifted = [s + shift for s in starts]
            if (1.2 <= abs(shift) <= 15.0 and shifted[0] >= 0
                    and shifted[-1] < duration and coverage(shifted) >= 0.5):
                starts = shifted
    elif have_anchors:
        # no usable LRC: Whisper anchors become the line starts directly
        vals: list = [anchors.get(k) for k in range(n_lines)]
        starts = _interp(vals)
        if starts:
            method = "hybrid"

    if starts and starts[0] is not None:
        song_end = max(voice_end, (starts[-1] or 0) + 5.0)
        for i in range(n_lines):
            start = starts[i]
            end = starts[i + 1] if i + 1 < n_lines else song_end
            windows.append((start, max(end, start + 0.4)))
    else:
        weights = np.array([max(len(l), 1) for l in text_lines], dtype=float)
        cum = np.concatenate([[0.0], np.cumsum(weights)]) / weights.sum()
        span = max(voice_end - voice_start, 1.0)
        windows = [(voice_start + c0 * span, voice_start + c1 * span)
                   for c0, c1 in zip(cum[:-1], cum[1:])]

    lines = []
    for start, end in windows:
        slots = [round(x, 2) for x in onsets if start <= x < end][:80]
        # long instrumental gaps: stop the highlight shortly after the voice does
        eff_end = min(end, slots[-1] + 1.5) if slots else end
        lines.append({
            "s": round(start, 2),
            "e": round(max(eff_end, start + 0.5), 2),
            "slots": slots,
        })

    data = {"version": TIMING_VERSION, "method": method, "lines": lines}
    (folder / "timing.json").write_text(json.dumps(data), encoding="utf-8")
    return data


# ---------------------------------------------------------------------------
# Whisper word-level alignment (precision path)
# ---------------------------------------------------------------------------

_WHISPER = None


def _get_whisper():
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel

        name = os.environ.get("ASEREJE_WHISPER_MODEL", "small")
        _WHISPER = WhisperModel(name, device="cpu", compute_type="int8")
    return _WHISPER


def _vocals_16k(folder: Path) -> np.ndarray:
    orig, sr = _mono(folder / "original.mp3")
    inst, _ = _mono(folder / "instrumental.mp3")
    n = min(len(orig), len(inst))
    vocals = orig[:n] - inst[:n]
    n16 = int(n * 16000 / sr)
    return np.interp(
        np.linspace(0, n - 1, n16), np.arange(n), vocals
    ).astype(np.float32)


def _norm_word(w: str) -> str:
    return re.sub(r"[^\w]+", "", w.lower())


def _syllable_count(word: str) -> int:
    if re.search(r"[぀-ヿ一-鿿가-힯]", word):
        return max(1, len(word))
    return max(1, len(re.findall(r"[aeiouyáéíóúàèìòùâêîôûäëïöüãõœæ]+", word.lower())))


_WHISPER_LANGS = {"en", "es", "ca", "fr", "de", "it", "pt", "ja", "nl", "pl", "ru", "sv", "ko"}


def compute_timing_whisper(
    folder: Path, lang: str | None = None
) -> tuple[dict | None, dict[int, float]]:
    """Word-level timing from a local speech model over the vocals track.

    Returns (full_timing_or_None, line_anchors). When the transcript match is
    too weak for full timing, the anchors still calibrate the hybrid path.
    """
    lyrics_file = folder / "lyrics.txt"
    if not lyrics_file.exists():
        return None, {}
    body = _strip_header(lyrics_file.read_text(encoding="utf-8"))
    text_lines = [l for l in body.splitlines() if l.strip()]
    if not text_lines:
        return None, {}

    cjk = len(re.findall(r"[぀-ヿ一-鿿]", body)) > 0.3 * max(1, len(re.sub(r"\s", "", body)))

    # lyric token stream: (line_index, position_in_line, normalized_token)
    lyr_tokens: list[tuple[int, int, str]] = []
    for k, line in enumerate(text_lines):
        pos = 0
        parts = re.sub(r"[^\w]", "", line) if cjk else re.findall(r"[\w']+", line)
        for part in parts:
            token = part.lower() if cjk else _norm_word(part)
            if token:
                lyr_tokens.append((k, pos, token))
                pos += 1
    if not lyr_tokens:
        return None, {}

    model = _get_whisper()
    segments, _info = model.transcribe(
        _vocals_16k(folder),
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
        language=lang if lang in _WHISPER_LANGS else None,
        condition_on_previous_text=False,
    )
    # transcript token stream: (normalized_token, start, end, syllables)
    tr_tokens: list[tuple[str, float, float, int]] = []
    for seg in segments:
        for w in seg.words or []:
            raw = w.word.strip()
            if cjk:
                chars = [c for c in re.sub(r"[^\w]", "", raw)]
                if not chars:
                    continue
                step = (w.end - w.start) / len(chars)
                for ci, ch in enumerate(chars):
                    tr_tokens.append((ch.lower(), w.start + ci * step,
                                      w.start + (ci + 1) * step, 1))
            else:
                nw = _norm_word(raw)
                if nw:
                    tr_tokens.append((nw, w.start, w.end, _syllable_count(raw)))
    if len(tr_tokens) < 10:
        return None, {}

    matcher = difflib.SequenceMatcher(
        None, [t for _, _, t in lyr_tokens], [t for t, *_ in tr_tokens], autojunk=False
    )
    per_line: dict[int, list[tuple[float, float, int]]] = {}
    anchors: dict[int, float] = {}   # line -> estimated start, from matched words
    anchor_pos: dict[int, int] = {}  # how early in the line the anchor word sits
    matched = 0
    for block in matcher.get_matching_blocks():
        for off in range(block.size):
            line_idx, tok_pos, _ = lyr_tokens[block.a + off]
            _, ws, we, syl = tr_tokens[block.b + off]
            per_line.setdefault(line_idx, []).append((ws, we, syl))
            matched += 1
            if line_idx not in anchor_pos or tok_pos < anchor_pos[line_idx]:
                anchor_pos[line_idx] = tok_pos
                # a mid-line word implies the line started a bit earlier
                anchors[line_idx] = max(0.0, ws - 0.25 * tok_pos)

    n = len(text_lines)
    strong = matched >= 0.35 * len(lyr_tokens) or len(per_line) >= 0.6 * n
    if not strong:
        return None, anchors  # too weak alone; anchors feed the hybrid path

    starts: list[float | None] = [None] * n
    ends: list[float | None] = [None] * n
    slots_per_line: list[list[float]] = [[] for _ in range(n)]
    for k, words in per_line.items():
        words.sort()
        starts[k] = words[0][0]
        ends[k] = words[-1][1]
        for ws, we, syl in words:
            span = max(we - ws, 0.05)
            slots_per_line[k].extend(round(ws + i * span / syl, 2) for i in range(syl))

    starts = _interp(starts)
    lines = []
    for k in range(n):
        start = starts[k]
        nxt = starts[k + 1] if k + 1 < n else (ends[-1] or start) + 4.0
        end = ends[k] if ends[k] else min(nxt, start + 4.0)
        end = min(max(end, start + 0.4), max(nxt, start + 0.4))
        lines.append({
            "s": round(start, 2),
            "e": round(end + 0.2, 2),
            "slots": sorted(t for t in slots_per_line[k] if start - 0.05 <= t <= end + 1.0)[:80],
        })

    data = {"version": TIMING_VERSION, "method": "whisper", "lines": lines}
    (folder / "timing.json").write_text(json.dumps(data), encoding="utf-8")
    return data, anchors


def compute_best(folder: Path, meta: dict | None = None) -> dict | None:
    """Whisper word alignment when strong; otherwise its anchors calibrate
    the LRC-based method (hybrid); energy heuristics as the last resort."""
    anchors: dict[int, float] | None = None
    try:
        data, anchors = compute_timing_whisper(folder, (meta or {}).get("lyrics_lang"))
        if data:
            return data
    except Exception:
        anchors = None
    return compute_timing(folder, anchors)


def ensure_timing(folder: Path, meta: dict | None = None) -> dict | None:
    """Cached timing.json, computing it (and fetching an LRC) if missing."""
    timing_file = folder / "timing.json"
    if timing_file.exists():
        try:
            data = json.loads(timing_file.read_text(encoding="utf-8"))
            if data.get("version", 1) >= TIMING_VERSION:
                return data
            # stale format: fall through and recompute
        except Exception:
            pass
    if not (folder / "lyrics.lrc").exists() and meta:
        try:  # one-time attempt to get per-line timestamps for old songs
            import remove_vocals as rv

            found = rv.search_lrclib(
                meta.get("title") or "", meta.get("artist"), meta.get("duration")
            )
            if found and found[2]:
                (folder / "lyrics.lrc").write_text(found[2] + "\n", encoding="utf-8")
        except Exception:
            pass
    try:
        return compute_best(folder, meta)
    except Exception:
        return None
