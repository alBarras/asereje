# Building shareable ASEREJÉ apps (Windows + macOS)

Friends double-click one file — no Python, no terminal. This document explains
how the packaging works and the exact routine to produce new builds after any
code change.

## How it works (the 30-second version)

- **PyInstaller** freezes the whole Python runtime (Flask, PyTorch, Demucs,
  faster-whisper, yt-dlp, ffmpeg…) into a self-contained app folder.
  `desktop.py` is the entry point: it shows a tray/menu-bar icon, starts the
  server on port 5056 and opens the admin UI in the default browser. Guests on
  the same Wi-Fi join exactly as before (the guest URL is in the tray menu).
- **PyInstaller cannot cross-compile.** A Windows .exe can only be built on
  Windows, an Intel-Mac app on an Intel Mac, an Apple-Silicon app on Apple
  Silicon. That's why builds run on **GitHub Actions** (three runners:
  `windows-latest`, `macos-latest` = arm64, `macos-15-intel` = the last Intel
  image, available until Aug 2027).
- **Intel Macs need pinned deps**: PyTorch stopped shipping Intel-Mac wheels
  after 2.2.2, so that build uses `packaging/constraints-macos-intel.txt`
  (torch/torchaudio 2.2.2 + numpy 1.x). The other two targets use
  requirements.txt as-is.
- When frozen, the app folder is read-only (macOS may even run it from a
  read-only mount). All writable state — `library/`, playlists, session,
  queue, `.env` — moves to a per-user data folder (`runtime_paths.py`):
  - macOS: `~/Library/Application Support/ASEREJE`
  - Windows: `%LOCALAPPDATA%\ASEREJE`
  - A debug log lives there too: `asereje.log`.
  In dev nothing changes (data stays next to the code).
- ffmpeg is pre-fetched at build time and shipped inside the bundle. The
  Demucs model (~1 GB) and the Whisper model still download on first use into
  the user's home cache — so the **first song needs internet and is slow**;
  after that it's all local.

## Releasing new builds after a code change

```bash
git add -A && git commit -m "whatever changed"
git tag v1.0.1            # any tag starting with v
git push origin main --tags
```

That's it. The `build-executables` workflow builds all three apps (~20-40 min)
and attaches them to a **GitHub Release** named after the tag:

- `ASEREJE-windows-x64.zip`
- `ASEREJE-macos-apple-silicon.zip`
- `ASEREJE-macos-intel.zip`

The repo is public, so just send friends the Release page URL
(https://github.com/alBarras/asereje/releases/latest) and tell them which zip
matches their machine.

Want builds without tagging a release? Actions tab → *build-executables* →
*Run workflow* — the zips appear under that run's **Artifacts** instead.

### Cost

The repo is public, so GitHub-hosted runners (including macOS) are **free with
no minute limit**. Regular `git push` never triggers builds anyway — only `v*`
tags and manual runs do. Since the repo is public, never commit songs, lyrics
files, or anything from `library/` (all git-ignored), and keep the README's
non-commercial framing.

## Building locally (your own Mac only — Apple Silicon)

Useful to test packaging changes without spending CI minutes:

```bash
.venv/bin/pip install -r packaging/requirements-build.txt
.venv/bin/python -c "from static_ffmpeg import run; run.get_or_fetch_platform_executables_else_raise()"
.venv/bin/pyinstaller --noconfirm packaging/asereje.spec
open dist/ASEREJE.app        # or: ditto -c -k --keepParent dist/ASEREJE.app ASEREJE.zip
```

`build/` and `dist/` are git-ignored. The .app is ~750 MB on disk; the zip is
~260 MB (Windows will be larger — its CPU torch build is heavier).

## What your friends have to do (send them this)

**macOS** — unzip, drag `ASEREJE.app` to Applications (optional), double-click.
The app is not notarized (that needs a paid Apple Developer account), so the
first launch is blocked: **right-click the app → Open → Open**. On newer macOS
that option may not appear — then it's **System Settings → Privacy & Security
→ scroll down → "Open Anyway"** next to the ASEREJE message, once. A music
note appears in the menu bar and the browser opens by itself (first launch
takes ~20 s). Quit from the menu-bar icon.

**Windows** — unzip, open the `ASEREJE` folder, double-click `ASEREJE.exe`.
SmartScreen will warn because the app is unsigned: click **More info → Run
anyway**, once. A tray icon appears and the browser opens.

**Both**: on first launch the app shows a one-time setup screen that downloads
the vocal-separation engine (~330 MB) with a progress bar before letting them
in (cancelling is allowed, but the app stays locked until it's done). The
Whisper timing model fetches quietly in the background right after. Updating
to a new version = delete the old app/folder, unzip the new one — their song
library and downloaded models are stored separately and survive.

## Files involved

| File | Role |
|---|---|
| `desktop.py` | Frozen-app entry point: tray icon + server + browser. |
| `runtime_paths.py` | Decides the writable data dir (dev vs frozen). |
| `packaging/asereje.spec` | PyInstaller recipe (bundled data, hidden imports, .app metadata). |
| `packaging/requirements-build.txt` | pyinstaller + pystray. |
| `packaging/constraints-macos-intel.txt` | Old-torch pins for the Intel-Mac build. |
| `packaging/icon.icns` / `icon.ico` | App icon (macOS / Windows), generated from `static/logo.png` (which is also the favicon and tray icon). |
| `.github/workflows/build-executables.yml` | The 3-platform build + Release. |

## Gotchas when changing code

- New Python package? Add it to `requirements.txt` and check it publishes
  wheels for all three targets (Intel-Mac is the fragile one).
- New data file loaded via a path relative to the code? Add it to `datas` in
  `packaging/asereje.spec`, and keep *written* files under `DATA` from
  `runtime_paths.py`, never next to the code.
- Dynamic imports (strings resolved to classes) may need `hiddenimports` in
  the spec — the symptom is a `ModuleNotFoundError` only in the frozen app.
- GitHub Release assets are capped at 2 GiB per file; the zips are well under
  that today, but bundling model weights would blow past it.
