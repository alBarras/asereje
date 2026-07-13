#!/usr/bin/env python3
"""Remove vocals from an MP3 file using Demucs source separation.

Input can be a local MP3 or a YouTube URL (downloaded with yt-dlp).
Optionally fetches the song lyrics and saves them next to the audio.

Usage:
    python remove_vocals.py input.mp3 [output.mp3]
    python remove_vocals.py -url "https://www.youtube.com/watch?v=..." [-lyrics] [-o outdir]

Proof of concept for personal, non-commercial use.
"""

import argparse
import os
import re
import sys
from pathlib import Path

import certifi

# Model weights are fetched over HTTPS on first run; macOS Python builds often
# lack system CA certs, so point urllib at certifi's bundle.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import numpy as np
import soundfile as sf
import torch
from demucs.apply import apply_model
from demucs.audio import convert_audio
from demucs.pretrained import get_model

MODEL_NAME = "htdemucs_ft"
LRCLIB_API = "https://lrclib.net/api/search"

# "(Official Video)", "[4K Remaster]", "(Lyric Video)" and friends
TITLE_JUNK = re.compile(
    r"[\(\[][^\)\]]*(official|video|audio|lyric|visuali[sz]er|remaster|hd|4k)"
    r"[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
LRC_TIMESTAMP = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]\s*")


# ---------------------------------------------------------------------------
# Vocal separation
# ---------------------------------------------------------------------------

def pick_device(requested: str | None) -> str:
    if requested:
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_audio(path: Path) -> tuple[torch.Tensor, int]:
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    # soundfile gives (frames, channels); demucs wants (channels, frames)
    return torch.from_numpy(data.T), sr


def save_mp3(path: Path, wav: torch.Tensor, sr: int) -> None:
    data = wav.clamp(-0.99, 0.99).cpu().numpy().T
    sf.write(path, data, sr, format="MP3")


def remove_vocals(
    input_path: Path, output_path: Path, device: str, resync_original: bool = False
) -> None:
    print(f"Loading model '{MODEL_NAME}'...")
    model = get_model(MODEL_NAME)
    model.eval()

    print(f"Reading {input_path}...")
    wav, sr = load_audio(input_path)
    wav = convert_audio(wav, sr, model.samplerate, model.audio_channels)

    if resync_original:
        # Rewrite the original through the same resample+encoder path as the
        # instrumental so both MP3s share sample rate, frame count, and encoder
        # delay — otherwise browsers play them slightly out of sync.
        save_mp3(input_path, wav, model.samplerate)

    # Normalize as demucs.separate does, then restore scale afterwards
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    wav_norm = (wav - mean) / std

    print(f"Separating stems on {device} (this can take a while)...")
    with torch.no_grad():
        sources = apply_model(
            model,
            wav_norm[None],
            device=device,
            shifts=1,
            split=True,
            overlap=0.25,
            progress=True,
        )[0]
    sources = sources * std + mean

    # Mix every stem except the vocals back together
    instrumental = torch.zeros_like(sources[0])
    for name, stem in zip(model.sources, sources):
        if name != "vocals":
            instrumental += stem

    print(f"Writing {output_path}...")
    save_mp3(output_path, instrumental, model.samplerate)


# ---------------------------------------------------------------------------
# YouTube download
# ---------------------------------------------------------------------------

def download_from_youtube(
    url: str, outdir: Path, filename_template: str = "%(title)s.%(ext)s"
) -> tuple[Path, dict]:
    """Download a YouTube video's audio as MP3. Returns (path, video metadata)."""
    import yt_dlp
    from static_ffmpeg import run as static_ffmpeg_run

    print("Locating ffmpeg (downloaded once if missing)...")
    ffmpeg_path, _ = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(outdir / filename_template),
        "restrictfilenames": True,
        "noplaylist": True,
        "ffmpeg_location": str(Path(ffmpeg_path).parent),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    print(f"Downloading audio from {url}...")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info:  # search result or playlist: take the first hit
            info = info["entries"][0]
        mp3_path = Path(ydl.prepare_filename(info)).with_suffix(".mp3")
    print(f"Saved original audio to {mp3_path}")
    return mp3_path, info


# ---------------------------------------------------------------------------
# Lyrics
# ---------------------------------------------------------------------------

def clean_title(title: str) -> str:
    return TITLE_JUNK.sub("", title).strip(" -_|")


def guess_track_and_artist(info: dict | None, fallback_name: str) -> tuple[str, str | None, int | None]:
    """Best-effort (track, artist, duration_seconds) from video metadata or filename."""
    if info:
        track = info.get("track") or clean_title(info.get("title", fallback_name))
        artist = info.get("artist") or info.get("channel") or info.get("uploader")
        # "Artist - Title" video naming convention beats channel names
        if " - " in track:
            maybe_artist, maybe_track = track.split(" - ", 1)
            track, artist = maybe_track.strip(), maybe_artist.strip()
        return track, artist, info.get("duration")
    if " - " in fallback_name:
        artist, track = fallback_name.split(" - ", 1)
        return track.strip(), artist.strip(), None
    return fallback_name, None, None


def search_lrclib(
    track: str, artist: str | None, duration: int | None
) -> tuple[str, str, str | None] | None:
    """Query the free lrclib.net lyrics database.

    Returns (lyrics, credit, synced_lrc_or_None) or None. The synced LRC
    carries per-line timestamps, used by singing mode.
    """
    import requests

    attempts = []
    if artist:
        attempts.append({"track_name": track, "artist_name": artist})
        attempts.append({"q": f"{artist} {track}"})
    attempts.append({"q": track})

    for params in attempts:
        try:
            resp = requests.get(LRCLIB_API, params=params, timeout=15)
            resp.raise_for_status()
            results = resp.json()
        except Exception:
            continue
        results = [r for r in results if r.get("plainLyrics") or r.get("syncedLyrics")]
        if not results:
            continue
        if duration:  # prefer the hit whose length matches the audio
            results.sort(key=lambda r: abs((r.get("duration") or 10**6) - duration))
        best = results[0]
        lyrics = best.get("plainLyrics") or LRC_TIMESTAMP.sub("", best["syncedLyrics"])
        credit = f'{best.get("artistName", "?")} - {best.get("trackName", "?")} (source: lrclib.net)'
        return lyrics.strip(), credit, best.get("syncedLyrics") or None
    return None


def lyrics_from_description(info: dict | None) -> tuple[str, str] | None:
    """Fallback: many music videos paste the lyrics into the description.

    Look for the longest run of consecutive short lines that don't look like
    links, credits, or hashtags.
    """
    desc = (info or {}).get("description") or ""
    lines = [ln.strip() for ln in desc.splitlines()]
    best: list[str] = []
    current: list[str] = []
    for ln in lines + [""]:
        looks_lyrical = (
            0 < len(ln) < 70
            and not re.search(r"https?://|www\.|#\w|©|℗|℠|@|subscribe|follow", ln, re.I)
        )
        if looks_lyrical or (ln == "" and current):
            current.append(ln)
        else:
            if len([l for l in current if l]) > len([l for l in best if l]):
                best = current
            current = []
    if len([l for l in best if l]) >= 12:
        return "\n".join(best).strip(), "extracted from the YouTube video description"
    return None


def fetch_and_save_lyrics(
    info: dict | None, audio_path: Path, out: Path | None = None
) -> Path | None:
    track, artist, duration = guess_track_and_artist(info, audio_path.stem.replace("_", " "))
    print(f"Searching lyrics for: {artist or '?'} - {track}")

    synced = None
    found = search_lrclib(track, artist, duration)
    if found:
        lyrics, credit, synced = found
    else:
        found = lyrics_from_description(info)
        if not found:
            print("No lyrics found (the track may be instrumental or too obscure).")
            return None
        lyrics, credit = found

    out = out or audio_path.with_name(audio_path.stem + "_lyrics.txt")
    if synced:  # per-line timestamps for singing mode
        out.with_name("lyrics.lrc").write_text(synced + "\n", encoding="utf-8")
    # no header block: the file is pure lyrics (credit only logged below)
    out.write_text(lyrics + "\n", encoding="utf-8")
    print(f"Saved lyrics to {out} ({credit})")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Remove vocals from an MP3 file.")
    parser.add_argument("input", type=Path, nargs="?", help="Input MP3 file")
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="Output MP3 file (default: <input>_instrumental.mp3)",
    )
    parser.add_argument(
        "-url",
        "--url",
        help="YouTube URL to download as MP3 and process (keeps the original too)",
    )
    parser.add_argument(
        "-lyrics",
        "--lyrics",
        action="store_true",
        help="Also look up the song lyrics and save them as a .txt",
    )
    parser.add_argument(
        "-o",
        "--outdir",
        type=Path,
        default=Path("."),
        help="Directory for downloaded/generated files in -url mode (default: .)",
    )
    parser.add_argument(
        "--device",
        choices=["cpu", "cuda", "mps"],
        default=None,
        help="Compute device (default: auto-detect)",
    )
    args = parser.parse_args()

    if bool(args.input) == bool(args.url):
        parser.error("provide either an input MP3 file or -url, not both/neither")

    info = None
    try:
        if args.url:
            args.outdir.mkdir(parents=True, exist_ok=True)
            input_path, info = download_from_youtube(args.url, args.outdir)
        else:
            if not args.input.is_file():
                parser.error(f"input file not found: {args.input}")
            input_path = args.input

        if args.lyrics:
            fetch_and_save_lyrics(info, input_path)

        output = args.output or input_path.with_name(input_path.stem + "_instrumental.mp3")
        remove_vocals(input_path, output, pick_device(args.device))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
