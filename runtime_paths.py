"""Where ASEREJÉ keeps its writable state.

In development everything lives next to the code, exactly as before. In a
frozen (PyInstaller) build the app folder must be treated as read-only —
macOS Gatekeeper can even run the .app from a randomized read-only mount
(app translocation) — so all mutable state moves to the per-user data dir:

    macOS    ~/Library/Application Support/ASEREJE
    Windows  %LOCALAPPDATA%\\ASEREJE
    Linux    $XDG_DATA_HOME/asereje (or ~/.local/share/asereje)

ASEREJE_DATA_DIR overrides the location in both modes (used by tests).
"""

import os
import sys
from pathlib import Path

IS_FROZEN = bool(getattr(sys, "frozen", False))


def _default_data_dir() -> Path:
    if not IS_FROZEN:
        return Path(__file__).resolve().parent
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ASEREJE"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "ASEREJE"
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "asereje"


DATA = Path(os.environ.get("ASEREJE_DATA_DIR") or _default_data_dir()).resolve()
DATA.mkdir(parents=True, exist_ok=True)
