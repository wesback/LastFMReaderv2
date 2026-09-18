"""Conservative derivation of clean Last.fm track titles."""

from __future__ import annotations

import re
from collections.abc import Iterable

DEFAULT_ANNOTATION_KEYWORDS: tuple[str, ...] = (
    "remaster",
    "remastered",
    "live",
    "acoustic",
    "radio edit",
    "single version",
    "deluxe",
    "bonus track",
    "mono",
    "stereo",
    "extended mix",
    "unplugged",
    "instrumental",
    "demo",
    "alternate take",
)

_TRAILING_PARENTHESES = re.compile(r"(?P<base>.*?)(?P<segment>\([^()]*\)|\[[^\[\]]*\])\s*$")
_TRAILING_DASH = re.compile(
    r"(?P<base>.+?)\s*[-\u2013\u2014]\s*(?P<segment>[^-\u2013\u2014]+)\s*$"
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def clean_title(
    track: str,
    annotation_keywords: Iterable[str] | None = None,
) -> str:
    """Derive a clean title without modifying the source track value.

    Only uniformly cased titles are title-cased.  Annotation removal is
    limited to matching trailing parenthetical, bracketed, or dash suffixes.
    Matching suffixes are removed repeatedly so stacked annotations are
    handled, while the original ``track`` remains available to callers.
    """
    if not isinstance(track, str):
        raise TypeError("track must be a string")

    keywords = (
        DEFAULT_ANNOTATION_KEYWORDS
        if annotation_keywords is None
        else tuple(annotation_keywords)
    )
    patterns = _compile_keyword_patterns(keywords)
    title = track

    while True:
        candidate = _trailing_annotation(title)
        if candidate is None:
            break
        base, segment = candidate
        if not _contains_keyword(segment, patterns):
            break
        title = base.rstrip()

    return _correct_degenerate_case(title)


def _compile_keyword_patterns(keywords: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    patterns: list[re.Pattern[str]] = []
    for keyword in keywords:
        if not isinstance(keyword, str):
            raise TypeError("annotation keywords must be strings")
        normalized = " ".join(keyword.casefold().split())
        if normalized:
            patterns.append(
                re.compile(
                    rf"(?<!\w){re.escape(normalized)}(?!\w)",
                    re.IGNORECASE,
                )
            )
    return tuple(patterns)


def _trailing_annotation(title: str) -> tuple[str, str] | None:
    match = _TRAILING_PARENTHESES.fullmatch(title)
    if match is not None and match.group("base").strip():
        return match.group("base"), match.group("segment")[1:-1]

    match = _TRAILING_DASH.fullmatch(title)
    if match is not None and match.group("base").strip():
        return match.group("base"), match.group("segment")

    return None


def _contains_keyword(
    segment: str,
    patterns: tuple[re.Pattern[str], ...],
) -> bool:
    normalized_segment = " ".join(segment.casefold().split())
    return any(pattern.search(normalized_segment) for pattern in patterns)


def _correct_degenerate_case(title: str) -> str:
    if not (title.isupper() or title.islower()):
        return title

    lower_title = title.lower()
    return _WORD.sub(
        lambda match: match.group(0)[0].upper() + match.group(0)[1:],
        lower_title,
    )
