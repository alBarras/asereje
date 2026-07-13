# ASEREJÉ — developer context

Local karaoke studio. Flask backend + ONE single-page UI (`static/index.html`:
all HTML, CSS, JS and i18n in a single file, no build step). Server on port
**5056**, bound to 0.0.0.0. UI languages: Catalan (default) / English / Spanish.

## Run & dev workflow

- `.venv/bin/python app.py` — prints admin URL (localhost) and LAN guest URL.
- Frontend-only changes need just a browser reload; restart the server only for
  `app.py` / `timing.py` / `translate.py` changes.
- Kill reliably with `lsof -tiTCP:5056 -sTCP:LISTEN | xargs kill -9` (pkill by
  name misses listeners). **Before restarting, check `/api/queue`** — killing
  mid-download loses the user's job (incomplete folders get auto-deleted).
- `.env` holds `ANTHROPIC_API_KEY` etc. (git-ignored, also settable from the UI).

## Data model — `library/<slug>/`

| File | Notes |
|---|---|
| `original.mp3`, `instrumental.mp3` | Sample-exact twins: the original is re-encoded through the same decode→44.1kHz→LAME path as the instrumental, so the Web Audio player can crossfade them on one clock with zero drift. Never re-encode one without the other. |
| `lyrics.txt` | Original lyrics. May start with a header: line 1 title, then `[bracketed]` credit lines. The header-skip rule is duplicated in THREE places that must stay in sync: `stripLyricsHeader` (client), `_strip_header` (timing.py), `split_lyrics_header` (app.py). |
| `lyrics.<lang>.txt` | Official-language translation (e.g. `lyrics.ca.txt`). |
| `lyrics.x-<slug>.txt` | Custom user variant (prefix `x-`), grouped separately in the UI. |
| `lyrics.lrc` | Synced LRC from lrclib.net when available (feeds the hybrid timing path). |
| `timing.json` | `{version, method, lines: [{s, e, slots: [sec,…]}]}` — one entry per NON-EMPTY post-header lyric line. Language-agnostic single source of truth; every displayed language maps its syllables onto the same line windows/slots by line index. `method` ∈ whisper / hybrid / energy / manual. |
| `cover.jpg` | Square 600px. |
| `meta.json` | title, artist, channel, duration, video_id, query, created, lyrics_lang, and optionally `sing_offset` + `sing_offset_key`. |

`TIMING_VERSION` (timing.py, currently 8): files below it regenerate lazily on
first request. Timing methods cascade: Whisper word-alignment (accepted if
≥35% tokens matched or ≥60% lines anchored) → hybrid (Whisper anchors calibrate
LRC line times) → energy heuristics.

**Sync nudge semantics**: the ± nudge in sing mode is stored in the song's
`meta.json` as `sing_offset` with `sing_offset_key = "v<version><method>"`. If
the timing regenerates (version bump, ⟳ rebuild, manual recording/edit), the
key no longer matches and the offset resets — never carry a stale correction
onto new timing. localStorage keeps a per-device fallback cache.

**Translations must preserve line structure** (line k ↔ line k) or timing
breaks. The AI pipeline (translate.py) is 3-step: Google reference → syllable-
matched adaptation → review pass; modes `regular` (±1 syllable) / `syllables`
(exact, clipped words allowed).

## Server state (all git-ignored)

`playlists.json`, `download_queue.json` (persisted, survives restarts, resumes
pending downloads), `session.json` (spectator profiles + party settings:
allow_comments, show_scores, score_wait), `avatars/`, `plcovers/`.

Roles are decided by request origin, not stored ids: **localhost = admin, LAN
IP = spectator** (`_is_local_request`). Votes live in memory per round
(`vote_state`), messages in a 100-entry ring buffer; both reset on restart.

## Frontend conventions (static/index.html)

- **Never use native `alert/confirm/prompt`** — they freeze the AudioContext.
  Use `uiAlert` / `uiConfirm` / `uiPrompt` (promise-based modal).
- i18n: `I18N` dict + `data-i18n` attributes + `t(key, vars)`. Every new UI
  string needs ca/en/es entries.
- Audio: ONE shared `AudioContext`; gain-node crossfade; `playBoth` guarded by
  a `playReq` token (pause/dismiss bump it to cancel in-flight plays);
  `startSources()` always stops previous sources first; `actx.onstatechange`
  converts OS audio interruptions into a clean pause. Buffer loads retry 3×.
- z-index layers: sing mode 80, addMenu/toast 200, spectator view 150, crop
  editor 250, modal 300, stickers 45 (above the score overlay at 40).
- localStorage keys: clientId, playMode, userQueue, playHistory, singOffsets,
  exportDest, uiLang, libCoverBust, customReacts, singLineMode.
- Heavy visuals (confetti) render on a single `<canvas>` per effect — 90
  individually-animated DOM nodes caused frame freezes.

## API map (selected)

- Jobs: `POST /api/process` (search/URL → download → Demucs → lyrics →
  translate → timing), `GET /api/status/<job>`, download queue under
  `/api/queue…` (server-side worker, auto-skips duplicates).
- Library: `GET /api/library`, `/api/library/<id>/lyrics|edit|translate|
  timing|timing/rebuild|timing/manual|sing_offset|export`, `DELETE /api/library/<id>`.
- Playlists: CRUD under `/api/playlists…`, `GET /api/playlists/<id>/export`
  (ZIP: STORED for mp3/jpg, DEFLATED for text), `POST /api/import` (tolerates
  Finder re-zips: nested root, `__MACOSX`, dotfiles; dedups by video_id then
  normalized title|artist).
- Party: `/api/session/join|profile`, `/api/party`, `/api/session/party`,
  `/api/messages` (kind: text|reaction; reactions bypass allow_comments),
  `/api/votes` (+`/round`, stars 0 withdraws).

## Testing conventions

- curl against `http://127.0.0.1:5056`; LAN-IP requests simulate spectators.
- Create scratch song folders (copy two mp3s + minimal meta.json) instead of
  mutating real songs; delete test playlists/songs/session users afterwards.
- Whisper timing rebuild takes ~15–60 s per song on CPU.

Non-commercial personal POC — no distribution of downloaded content.
