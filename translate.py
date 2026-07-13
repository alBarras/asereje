"""Lyrics translation with pluggable AI engines.

AI engines (Claude, OpenAI, Gemini) run a singable three-step pipeline:
free Google translation as a meaning reference -> singable adaptation
(line structure + per-line syllable counts preserved) -> self-review pass
for language purity and syllable drift.

The free Google Translate web endpoint is the zero-config fallback, and can
be forced from the app settings regardless of configured AI keys.
"""

import os
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent

LANG_NAMES = {
    "en": "English", "es": "Spanish", "ca": "Catalan", "fr": "French",
    "de": "German", "it": "Italian", "pt": "Portuguese", "ja": "Japanese",
    "nl": "Dutch", "pl": "Polish", "ru": "Russian", "sv": "Swedish",
    "ko": "Korean", "zh-cn": "Chinese (Simplified)",
}


def _load_dotenv() -> None:
    env = BASE / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

ENGINE_ENV = "ASEREJE_ENGINE"  # "auto" | provider id | "google"

PROVIDERS = {
    "claude": {
        "label": "Claude",
        "key_env": "ANTHROPIC_API_KEY",
        "model": lambda: os.environ.get("ASEREJE_TRANSLATE_MODEL", "claude-opus-4-8"),
    },
    "openai": {
        "label": "OpenAI",
        "key_env": "OPENAI_API_KEY",
        "model": lambda: os.environ.get("ASEREJE_OPENAI_MODEL", "gpt-5-mini"),
    },
    "gemini": {
        "label": "Gemini",
        "key_env": "GEMINI_API_KEY",
        "model": lambda: os.environ.get("ASEREJE_GEMINI_MODEL", "gemini-2.5-flash"),
    },
}

_failed: set[str] = set()  # providers whose key was rejected this session
_claude_client = None


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code.lower(), code)


def detect_language(text: str) -> str | None:
    try:
        from langdetect import detect

        return detect(text).lower()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Engine selection
# ---------------------------------------------------------------------------

def provider_key(pid: str) -> str:
    return os.environ.get(PROVIDERS[pid]["key_env"], "")


def selected_engine_setting() -> str:
    sel = os.environ.get(ENGINE_ENV, "auto")
    return sel if sel in ("auto", "google", *PROVIDERS) else "auto"


def effective_engine() -> str:
    """Resolve the engine the next translation will actually use."""
    sel = selected_engine_setting()
    if sel == "google":
        return "google"
    if sel in PROVIDERS and provider_key(sel) and sel not in _failed:
        return sel
    for pid in PROVIDERS:  # auto: first configured, still-working AI
        if provider_key(pid) and pid not in _failed:
            return pid
    return "google"


def engine_label(engine: str) -> str:
    if engine == "google":
        return "Google Translate"
    cfg = PROVIDERS[engine]
    return f"{cfg['label']} ({cfg['model']()})"


def active_engine() -> str:
    return engine_label(effective_engine())


def reset_backend() -> None:
    """Forget cached clients/failure state (e.g. after keys changed)."""
    global _claude_client
    _claude_client = None
    _failed.clear()


# ---------------------------------------------------------------------------
# Prompts (shared by every AI provider)
# ---------------------------------------------------------------------------

MODE_ENV = "ASEREJE_TRANSLATE_MODE"


def translate_mode() -> str:
    mode = os.environ.get(MODE_ENV, "regular")
    return mode if mode in ("regular", "syllables") else "regular"


_SINGABLE_TEMPLATE = """You are a professional song-lyric adapter. You produce SINGABLE translations meant to be sung over the original melody.

Rules, in priority order:
1. Keep the line structure exactly: same number of lines, blank lines in the same places.
{RULE2}
3. Write 100% in the target language. Not a single word from the source language or any other language may remain, with exactly two exceptions: proper names, and untranslatable vocables/scat syllables (e.g. "Aserejé ja de jé"), which stay unchanged.
4. Preserve the meaning naturally; prefer idiomatic phrasing over literal wording.

Reply with ONLY the translated lyrics — no preamble, no notes, no line numbers."""

_REVIEW_TEMPLATE = """You are a meticulous proofreader of singable song-lyric translations. You receive the original lyrics and a draft translation, and you fix violations while touching as little as possible.

Checks, all against the original:
1. Language purity: every word of the draft must be in the target language, except proper names and untranslatable vocables/scat syllables. Replace any leftover foreign word with a natural target-language equivalent of the same syllable count.
2. Structure: same number of lines and blank lines as the original.
{RULE3}
4. Naturalness: fix awkward or ungrammatical phrasing{NATURAL_CAVEAT}.

Reply with ONLY the final corrected lyrics — no preamble, no notes, no explanations."""


def singable_system() -> str:
    if translate_mode() == "syllables":
        rule2 = (
            "2. Each translated line must have EXACTLY the same number of syllables "
            "as its original line — this outranks natural phrasing and even meaning "
            "nuance. Count the original line's syllables, then count yours; they must "
            "be equal. You may clip or elide words (apocopes, elisions, contractions, "
            "dropped articles — e.g. « cantar' », « lov' », « 'round ») as long as "
            "every word stays intelligible when sung. Rhyming is NOT required."
        )
    else:
        rule2 = (
            "2. Match each line's syllable count to the original line's as closely as "
            "possible (ideally equal, at most one more or one fewer) so it fits the "
            "melody. Rhyming is NOT required — sacrifice rhyme, never syllable count."
        )
    return _SINGABLE_TEMPLATE.replace("{RULE2}", rule2)


def review_system() -> str:
    if translate_mode() == "syllables":
        rule3 = (
            "3. Exact syllable counts: count the syllables of every original line and "
            "of the matching draft line. Any mismatch, even by one, must be fixed — "
            "clip or elide words if needed, as long as they remain intelligible."
        )
        caveat = " (but never at the cost of the exact syllable count)"
    else:
        rule3 = (
            "3. Singability: each line's syllable count should closely match the "
            "original line's; rewrite lines that drift by more than one syllable."
        )
        caveat = ""
    return _REVIEW_TEMPLATE.replace("{RULE3}", rule3).replace("{NATURAL_CAVEAT}", caveat)


# ---------------------------------------------------------------------------
# Per-provider chat calls
# ---------------------------------------------------------------------------

def _claude_msg(system: str, user: str) -> str | None:
    global _claude_client
    import anthropic

    if _claude_client is None:
        _claude_client = anthropic.Anthropic()
    try:
        response = _claude_client.messages.create(
            model=PROVIDERS["claude"]["model"](),
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        _failed.add("claude")
        return None
    if response.stop_reason == "refusal":
        return None
    out = "".join(b.text for b in response.content if b.type == "text").strip()
    return out or None


def _openai_msg(system: str, user: str) -> str | None:
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {provider_key('openai')}"},
        json={
            "model": PROVIDERS["openai"]["model"](),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=180,
    )
    if r.status_code == 401:
        _failed.add("openai")
        return None
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip() or None


def _gemini_msg(system: str, user: str) -> str | None:
    model = PROVIDERS["gemini"]["model"]()
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": provider_key("gemini")},
        json={
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        },
        timeout=180,
    )
    if r.status_code in (401, 403):
        _failed.add("gemini")
        return None
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip() or None


_AI_CALLS = {"claude": _claude_msg, "openai": _openai_msg, "gemini": _gemini_msg}


def _ai_msg(pid: str, system: str, user: str) -> str | None:
    try:
        return _AI_CALLS[pid](system, user)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Singable pipeline (any AI provider)
# ---------------------------------------------------------------------------

def _translate_ai(pid: str, text: str, target: str) -> str | None:
    target_name = lang_name(target)

    # Step 1: free machine translation as a meaning anchor (best effort)
    try:
        reference = _translate_google(text, target)
    except Exception:
        reference = None

    user = f"Target language: {target_name}.\n\nOriginal lyrics:\n{text}"
    if reference:
        user += (
            "\n\nMachine reference translation (meaning guide ONLY — its phrasing"
            " is rough and it ignores syllable counts):\n" + reference
        )
    user += f"\n\nProduce the singable {target_name} translation now."

    # Step 2: singable adaptation
    draft = _ai_msg(pid, singable_system(), user)
    if not draft:
        return None

    # Step 3: self-review for language purity / structure / syllables
    review_user = (
        f"Target language: {target_name}.\n\nOriginal lyrics:\n{text}"
        f"\n\nDraft translation:\n{draft}"
        "\n\nApply your checks and output the final lyrics."
    )
    final = _ai_msg(pid, review_system(), review_user)
    if final and abs(len(final.splitlines()) - len(draft.splitlines())) <= 2:
        return final
    return draft  # reviewer failed or mangled the structure


# ---------------------------------------------------------------------------
# Free Google Translate
# ---------------------------------------------------------------------------

def _translate_google(text: str, target: str) -> str | None:
    from deep_translator import GoogleTranslator

    translator = GoogleTranslator(source="auto", target=target)
    out_lines: list[str] = []
    chunk: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal chunk, size
        if not chunk:
            return
        translated = translator.translate("\n".join(chunk)) or ""
        out_lines.extend(translated.splitlines())
        chunk, size = [], 0

    for line in text.splitlines():
        if size + len(line) > 4000:
            flush()
        chunk.append(line)
        size += len(line) + 1
    flush()
    result = "\n".join(out_lines).strip()
    return result or None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def translate_lyrics(text: str, target: str) -> tuple[str, str]:
    """Translate lyrics to `target` language code. Returns (text, engine)."""
    engine = effective_engine()
    if engine != "google":
        out = _translate_ai(engine, text, target)
        if out:
            return out, engine_label(engine)
    out = _translate_google(text, target)
    if out:
        return out, "Google Translate"
    raise RuntimeError(f"could not translate lyrics to '{target}'")
