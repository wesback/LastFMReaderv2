import unittest

from lastfm_export import clean_title, enrich_title


class TitleCleaningTests(unittest.TestCase):
    def test_corrects_wholly_uppercase_and_lowercase_titles(self) -> None:
        self.assertEqual(clean_title("HELLO WORLD"), "Hello World")
        self.assertEqual(clean_title("hello world"), "Hello World")

    def test_leaves_mixed_case_titles_unchanged(self) -> None:
        title = "deadmau5's P!nk Song"
        self.assertEqual(clean_title(title), title)

    def test_removes_configured_trailing_annotations_iteratively(self) -> None:
        keywords = ("live", "remastered")

        self.assertEqual(
            clean_title("Song (Live) [2024 Remastered] - Live", keywords),
            "Song",
        )

    def test_removes_only_matching_trailing_segments(self) -> None:
        title = "Song (Live) Version"

        self.assertEqual(clean_title(title, ("live",)), title)
        self.assertEqual(
            clean_title("Song [Live] - Radio Edit", ("live", "radio edit")),
            "Song",
        )

    def test_only_configured_trailing_annotations_are_removed(self) -> None:
        title = "Happy (from Despicable Me 2)"

        self.assertEqual(clean_title(title, ("live",)), title)
        self.assertEqual(clean_title(title, ("from",)), "Happy")

    def test_does_not_modify_source_track_value(self) -> None:
        track = "SONG (LIVE)"

        cleaned = clean_title(track)

        self.assertEqual(track, "SONG (LIVE)")
        self.assertEqual(cleaned, "Song")

    def test_preserves_adversarial_titles(self) -> None:
        titles = (
            "(I Can't Get No) Satisfaction",
            "1999",
            "Happy (from Despicable Me 2)",
            "99 Luftballons",
            "(Sittin' On) The Dock of the Bay",
        )

        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(clean_title(title), title)


class TitleEnrichmentTests(unittest.TestCase):
    def test_extracts_trailing_featuring_credits_before_annotations(self) -> None:
        result = enrich_title("Song feat. Guest Artist (Live)")

        self.assertEqual(result.track_title_clean, "Song")
        self.assertEqual(result.featured_artists, ["Guest Artist"])

    def test_extracts_credit_before_parenthesized_and_dash_annotations(self) -> None:
        result = enrich_title("Song feat. Guest Artist (Live) - Radio Edit")

        self.assertEqual(result.track_title_clean, "Song")
        self.assertEqual(result.featured_artists, ["Guest Artist"])

    def test_recognizes_supported_trailing_credit_markers(self) -> None:
        for marker in ("feat.", "featuring", "ft."):
            with self.subTest(marker=marker):
                result = enrich_title(f"Song {marker} Guest Artist")
                self.assertEqual(result.featured_artists, ["Guest Artist"])
                self.assertEqual(result.track_title_clean, "Song")

    def test_returns_null_credit_and_preserves_source_track_without_credit(self) -> None:
        track = "SONG (LIVE)"

        result = enrich_title(track)

        self.assertIsNone(result.featured_artists)
        self.assertEqual(result.track, track)
        self.assertEqual(result.track_title_clean, "Song")
