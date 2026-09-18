import unittest

from lastfm_export import clean_title


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
