#!/usr/bin/env python3
"""Entry point for the packaged (PyInstaller) desktop build.

Double-click flow: show a tray/menu-bar icon right away (the torch import
behind the Flask app takes 10-20 s), start the server, then open the admin
UI in the default browser. The tray menu has Open / guest URL / Quit; the
app has no window of its own — the browser is the UI, same as in dev.

Also runnable in dev for testing:  .venv/bin/python desktop.py
"""

import multiprocessing
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

PORT = 5056
ADMIN_URL = f"http://127.0.0.1:{PORT}"


def _chromium_candidates():
    """Installed Chromium-family browsers, most preferred first."""
    if sys.platform == "darwin":
        apps = [
            "Google Chrome.app/Contents/MacOS/Google Chrome",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "Brave Browser.app/Contents/MacOS/Brave Browser",
            "Chromium.app/Contents/MacOS/Chromium",
        ]
        roots = [Path("/Applications"), Path.home() / "Applications"]
        return [r / a for a in apps for r in roots if (r / a).exists()]
    if os.name == "nt":
        rels = [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
        ]
        roots = [os.environ.get(v) for v in
                 ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData")]
        return [Path(r) / rel for rel in rels for r in roots
                if r and (Path(r) / rel).exists()]
    names = ["google-chrome", "google-chrome-stable", "chromium",
             "chromium-browser", "microsoft-edge", "brave-browser"]
    return [p for p in (shutil.which(n) for n in names) if p]


def _open_window(url: str = ADMIN_URL) -> None:
    """Open the UI as a chromeless app window; plain browser tab as fallback."""
    for browser in _chromium_candidates():
        try:
            subprocess.Popen(
                [str(browser), f"--app={url}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except OSError:
            continue
    webbrowser.open(url)


def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def _wait_for_server(timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_in_use():
            return True
        time.sleep(0.3)
    return False


def _start_server() -> str:
    """Import the app (slow: torch), start Flask, return the guest LAN URL."""
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())

    import app as app_mod
    app_mod.init_state()
    threading.Thread(
        target=lambda: app_mod.app.run(
            host="0.0.0.0", port=PORT, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    return app_mod.lan_url()


def _make_icon_image():
    """Tray icon: the app logo, or a drawn fallback glyph if it's missing."""
    from PIL import Image, ImageDraw

    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    try:
        logo = Image.open(base / "static" / "logo.png").convert("RGBA")
        return logo.resize((128, 128), Image.LANCZOS)
    except OSError:
        pass
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(214, 40, 122, 255))
    d.ellipse((18, 40, 32, 52), fill=(255, 255, 255, 255))   # note head
    d.rectangle((29, 14, 33, 46), fill=(255, 255, 255, 255))  # stem
    d.polygon([(29, 14), (46, 20), (46, 30), (33, 24)], fill=(255, 255, 255, 255))
    return img


def _run_with_tray() -> None:
    import pystray

    state = {"lan": ""}

    def setup(icon):
        icon.visible = True
        if _port_in_use():
            # Another instance already runs: just show its UI and bow out.
            _open_window()
            icon.stop()
            return
        state["lan"] = _start_server()
        if _wait_for_server():
            _open_window()
        icon.update_menu()

    def open_ui(icon, item):
        _open_window()

    def quit_app(icon, item):
        icon.stop()

    icon = pystray.Icon(
        "asereje",
        icon=_make_icon_image(),
        title="ASEREJÉ",
        menu=pystray.Menu(
            pystray.MenuItem("Open ASEREJÉ", open_ui, default=True),
            pystray.MenuItem(lambda item: f"Guests: {state['lan'] or 'starting…'}",
                             None, enabled=False),
            pystray.MenuItem("Quit", quit_app),
        ),
    )
    icon.run(setup=setup)  # blocks until Quit (macOS needs this on main thread)
    os._exit(0)  # daemon threads (Flask, downloads) die with us


def _run_headless() -> None:
    if _port_in_use():
        _open_window()
        return
    lan = _start_server()
    if _wait_for_server():
        _open_window()
    print(f"Admin (this computer): {ADMIN_URL}")
    print(f"Guests (same Wi-Fi):   {lan}")
    print("Close this window (or Ctrl+C) to stop ASEREJÉ.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main() -> None:
    multiprocessing.freeze_support()

    from runtime_paths import DATA, IS_FROZEN
    if IS_FROZEN:
        # Windowed builds have no console; keep a log for debugging.
        log = open(DATA / "asereje.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log
        print(f"--- ASEREJÉ start {time.strftime('%Y-%m-%d %H:%M:%S')} ---")

    try:
        _run_with_tray()
    except Exception:
        import traceback
        traceback.print_exc()
        _run_headless()


if __name__ == "__main__":
    main()
