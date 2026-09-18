"""Installable Last.fm scrobble exporter package."""

from .titles import DEFAULT_ANNOTATION_KEYWORDS, clean_title

__version__ = "0.1.0"

__all__ = ["DEFAULT_ANNOTATION_KEYWORDS", "__version__", "clean_title"]
