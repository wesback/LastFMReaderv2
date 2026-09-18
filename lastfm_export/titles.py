"""Conservative derivation of clean Last.fm track titles."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

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
_TRAILING_FEATURE = re.compile(
    r"(?P<base>.+?)\s+(?:feat\.|featuring|ft\.)\s+"
    r"(?P<artist>.+?)\s*$",
    re.IGNORECASE,
)
_TRAILING_PAREN_FEATURE = re.compile(
    r"(?P<base>.+?)\s+\(\s*(?:feat\.|featuring|ft\.)\s+"
    r"(?P<artist>[^()]+?)\s*\)\s*$",
    re.IGNORECASE,
)
_TRAILING_BRACKET_FEATURE = re.compile(
    r"(?P<base>.+?)\s+\[\s*(?:feat\.|featuring|ft\.)\s+"
    r"(?P<artist>[^\[\]]+?)\s*\]\s*$",
    re.IGNORECASE,
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


@dataclass(frozen=True)
class TitleEnrichment:
    """Raw and derived title fields for one Last.fm track."""

    track: str
    track_title_clean: str
    featured_artists: list[str] | None


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


def enrich_title(
    track: str,
    annotation_keywords: Iterable[str] | None = None,
) -> TitleEnrichment:
    """Extract a trailing featuring credit before deriving the clean title.

    The source track is retained verbatim.  A credit is represented as one
    artist entry because this stage recognizes the credit marker, rather than
    attempting general artist-name parsing.
    """
    if not isinstance(track, str):
        raise TypeError("track must be a string")

    keywords = (
        None
        if annotation_keywords is None
        else tuple(annotation_keywords)
    )
    title, featured_artist = _extract_trailing_feature(track, keywords)
    return TitleEnrichment(
        track=track,
        track_title_clean=clean_title(title, keywords),
        featured_artists=(
            [featured_artist] if featured_artist is not None else None
        ),
    )


def _extract_trailing_feature(
    track: str,
    annotation_keywords: Iterable[str] | None,
) -> tuple[str, str | None]:
    match = _TRAILING_FEATURE.fullmatch(track)
    if match is not None:
        patterns = _compile_keyword_patterns(
            DEFAULT_ANNOTATION_KEYWORDS
            if annotation_keywords is None
            else annotation_keywords
        )
        base = match.group("base").rstrip()
        credit = match.group("artist").strip()
        if not credit:
            return track, None

        annotations = ""
        while True:
            candidate = _trailing_annotation(credit)
            if candidate is None:
                break
            annotation_base, segment = candidate
            if not _contains_keyword(segment, patterns):
                break
            annotations = credit[len(annotation_base) :] + annotations
            credit = annotation_base.rstrip()

        if not credit:
            return track, None
        return f"{base}{annotations}", credit

    patterns = _compile_keyword_patterns(
        DEFAULT_ANNOTATION_KEYWORDS
        if annotation_keywords is None
        else annotation_keywords
    )
    core, annotations = _strip_trailing_annotations(track, patterns)
    for pattern in (_TRAILING_PAREN_FEATURE, _TRAILING_BRACKET_FEATURE):
        match = pattern.fullmatch(core)
        if match is not None:
            credit = match.group("artist").strip()
            if credit:
                return f"{match.group('base').rstrip()}{annotations}", credit

    return track, None


def _strip_trailing_annotations(
    title: str,
    patterns: tuple[re.Pattern[str], ...],
) -> tuple[str, str]:
    annotations = ""
    while True:
        candidate = _trailing_annotation(title)
        if candidate is None:
            break
        base, segment = candidate
        if not _contains_keyword(segment, patterns):
            break
        annotations = title[len(base) :] + annotations
        title = base.rstrip()
    return title, annotations


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

    def capitalize_word(match: re.Match[str]) -> str:
        start = match.start()
        if (
            start >= 2
            and lower_title[start - 1] in {"'", "\u2019"}
            and lower_title[start - 2].isalpha()
        ):
            return match.group(0)
        word = match.group(0)
        return word[0].upper() + word[1:]

    return _WORD.sub(capitalize_word, lower_title)
