"""Lightweight i18n module. No external dependencies.

Usage:
    from app.i18n import t
    t("nav.dashboard", "zh")  # -> "仪表盘"
    t("nav.dashboard", "en")  # -> "Dashboard"

To add a new language:
    1. Create app/i18n/lang_XX.py with a TRANSLATIONS dict
    2. Import and register it in this file's LANGUAGES dict
"""
from app.i18n.lang_en import TRANSLATIONS as EN
from app.i18n.lang_zh import TRANSLATIONS as ZH

LANGUAGES = {
    "en": EN,
    "zh": ZH,
}

SUPPORTED_LANGUAGES = [
    ("en", "English"),
    ("zh", "中文"),
]

DEFAULT_LANGUAGE = "en"


def t(key: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """Look up a translation by dot-notation key.
    
    Falls back to English if key is missing in the target language,
    then falls back to the key itself if missing in English too.
    
    Example:
        t("nav.dashboard", "zh") -> "仪表盘"
        t("nav.dashboard", "en") -> "Dashboard"
        t("missing.key", "zh")   -> "missing.key"
    """
    result = _lookup(key, LANGUAGES.get(lang, EN))
    if result is not None:
        return result
    # Fallback to English
    if lang != DEFAULT_LANGUAGE:
        result = _lookup(key, EN)
        if result is not None:
            return result
    # Fallback to key itself
    return key


def _lookup(key: str, translations: dict) -> str | None:
    """Walk a dot-separated key path through a nested dict."""
    parts = key.split(".")
    current = translations
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current if isinstance(current, str) else None
