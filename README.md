# ASEREJÉ — your custom karaoke

A local, self-hosted karaoke studio. Give it a song name or a YouTube URL and it
downloads the audio, strips the vocals with AI, fetches and translates the
lyrics, computes per-syllable timing, and serves a full karaoke night over your
Wi-Fi: big synced lyrics on the screen, and everyone's phone as a remote for
reactions, comments and star votes.

**Personal, non-commercial proof of concept.** Downloading YouTube content may
violate YouTube's Terms of Service; use only on content you have rights to.

## Features

- **Vocal separation** with [Demucs](https://github.com/adefossez/demucs)
  `htdemucs_ft` (GPU via MPS when available, CPU fallback). Original and
  instrumental are re-encoded through the same path so they are sample-exact
  twins — the player crossfades between them on a single Web Audio clock.
- **Smart YouTube search**: scores results to prefer official/"- Topic" studio
  audio and avoid live versions, covers and karaoke tracks. Server-side
  download queue with editing, reordering and duplicate detection.
- **Library**: one folder per song (`library/<slug>/`) with audio, lyrics,
  translations, cover art, timing and metadata. Self-healing; deleting folders
  by hand works too.
- **Lyrics**: fetched from [lrclib.net](https://lrclib.net) (synced LRC when
  available) or mined from the video description; add them manually when
  nothing is found.
- **Singable AI translations** (Claude / OpenAI / Gemini, or the free Google
  fallback): a 3-step pipeline (reference translation → syllable-matched
  adaptation → review pass) with two modes — natural (±1 syllable) or exact
  syllable count. Custom lyric variants supported.
- **Syllable timing**: faster-whisper transcribes the vocal stem and aligns it
  to the lyrics; falls back to an LRC-hybrid and energy heuristics
  (`timing.json`, one line-anchored source of truth shared by every language).
  A "lens" mode visualizes every syllable on an interactive timeline, with
  one-click regeneration, spacebar-recorded manual timing, and a full tick
  editor (drag, arrow-key nudge, re-color, zoom, multi-row wrap).
- **Singing mode**: big lyrics with per-syllable (or whole-line) highlighting,
  MarioKart-style countdown lights, per-song sync nudge, queue/history sidebar
  and a vocals↔instrumental balance slider.
- **LAN party mode**: the machine running the server (localhost) is the admin;
  phones on the same Wi-Fi join as spectators with alias + avatar (with a
  built-in crop/rotate editor). They send sticker comments and emoji reactions
  onto the lyrics screen and vote 1–5 stars per performance; a score screen
  with confetti and star volleys shows the verdict between songs.
- **Playlists**: CRUD, ordering, covers, multi-select, ZIP export/import with
  dedup (tolerant of Safari/Finder re-zipping).
- Localized UI: Catalan (default), English, Spanish.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
brew install deno   # optional but recommended: yt-dlp uses it for YouTube's JS challenges
```

For AI translations, create a `.env` next to `app.py` (git-ignored):

```
ANTHROPIC_API_KEY=sk-ant-...
ASEREJE_TRANSLATE_MODEL=claude-opus-4-8   # optional; haiku is ~5x cheaper, looser
ASEREJE_ENGINE=auto                        # auto | claude | openai | gemini | google
```

Keys can also be pasted in the app's settings screen; they are stored locally
and never leave the machine. With no key at all, translations fall back to the
free Google endpoint.

## Run

```bash
.venv/bin/python app.py
```

- Admin (this computer): http://127.0.0.1:5056
- Guests (phones on the same Wi-Fi): the LAN URL printed at startup and shown
  in the app's ⋯ screen. Roles are decided by origin: localhost is always the
  admin, LAN clients are always spectators.

First runs download model weights (Demucs ~1 GB, Whisper small ~500 MB) to
`~/.cache`.

## CLI

The original command-line tool still works:

```bash
.venv/bin/python remove_vocals.py input.mp3 [output.mp3]
.venv/bin/python remove_vocals.py -url "https://youtube.com/watch?v=..." -lyrics -o downloads
```

`--device {cpu,cuda,mps}` overrides compute device auto-detection.

## Layout

| Path | What it is |
|---|---|
| `app.py` | Flask server: search/download jobs, library, playlists, party endpoints |
| `remove_vocals.py` | Demucs separation, YouTube download, lyrics fetch |
| `timing.py` | Whisper/hybrid/energy syllable-timing pipeline (`timing.json`) |
| `translate.py` | Singable translation pipeline and provider registry |
| `static/index.html` | The whole UI: player, singing mode, spectator view |
| `library/` | Your songs (git-ignored) |
