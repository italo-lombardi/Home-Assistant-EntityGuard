"""Guards against two mechanical i18n bugs a human review misses:

1. A locale file pasted wholesale from English (forgot to translate it).
2. A key present in en.json but missing from a locale (silent English
   fallback at runtime).

Neither catches *wrong wording* — that needs a native-speaker review. These
only catch "nobody translated this at all".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

TRANSLATIONS = (
    Path(__file__).parent.parent / "custom_components" / "entity_guard" / "translations"
)

# A genuine translation matches English only on a handful of cognates/brand
# words. Measured max across all 28 locales is 1.7% (5/290); a wholesale
# English paste is 100%. 15% sits ~9x above the noise floor and far below any
# paste, so it flags a pasted file without tripping on real cognates.
_MAX_ENGLISH_RATIO = 0.15


def _flatten(obj: object, prefix: str = "") -> dict[str, str]:
    """Return {dotted.key.path: value} for every leaf string in a nested dict."""
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, str):
        out[prefix] = obj
    return out


def _load(name: str) -> dict[str, str]:
    return _flatten(json.loads((TRANSLATIONS / name).read_text(encoding="utf-8")))


def _locale_files() -> list[str]:
    return sorted(p.name for p in TRANSLATIONS.glob("*.json") if p.name != "en.json")


@pytest.fixture(scope="module")
def english() -> dict[str, str]:
    return _load("en.json")


@pytest.mark.parametrize("name", _locale_files())
def test_locale_is_not_english_paste(name: str, english: dict[str, str]) -> None:
    """A locale whose values are mostly identical to English was never
    translated (e.g. an English file copied under a locale name)."""
    locale = _load(name)
    shared = [k for k in locale if k in english]
    assert shared, f"{name}: no keys overlap en.json — structure drift"
    identical = sum(locale[k] == english[k] for k in shared)
    ratio = identical / len(shared)
    assert ratio < _MAX_ENGLISH_RATIO, (
        f"{name}: {ratio:.0%} of strings identical to English "
        f"({identical}/{len(shared)}) — looks pasted, not translated"
    )


@pytest.mark.parametrize("name", _locale_files())
def test_locale_has_every_english_key(name: str, english: dict[str, str]) -> None:
    """A key missing from a locale silently falls back to English at runtime."""
    missing = sorted(set(english) - set(_load(name)))
    assert not missing, f"{name} missing keys (would fall back to English): {missing}"
