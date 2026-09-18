import unittest

from lastfm_export import clean_title, enrich_title


class TitleCleaningTests(unittest.TestCase):
    def test_corrects_wholly_uppercase_and_lowercase_titles(self) -> None:
        self.assertEqual(clean_title("HELLO WORLD"), "Hello World")
        self.assertEqual(clean_title("hello world"), "Hello World")

    def test_corrects_contractions_and_possessives(self) -> None:
        expected = "Don't Stop Me Now"
        self.assertEqual(clean_title("DON'T STOP ME NOW"), expected)
        self.assertEqual(clean_title("don't stop me now"), expected)
        self.assertEqual(clean_title("I'M STILL STANDING"), "I'm Still Standing")
        self.assertEqual(
            clean_title("DON’T LOOK BACK IN ANGER"),
            "Don’t Look Back In Anger",
        )
        self.assertEqual(clean_title("ROCK 'N' ROLL"), "Rock 'N' Roll")

    def test_leaves_mixed_case_titles_unchanged(self) -> None:
        for title in ("Don't Stop Me Now", "deadmau5's P!nk Song"):
            with self.subTest(title=title):
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
        self.assertEqual(
            clean_title("(I Can't Get No) Satisfaction"),
            "(I Can't Get No) Satisfaction",
        )
        self.assertEqual(clean_title("1999"), "1999")
        self.assertEqual(
            clean_title("Happy (from Despicable Me 2)"),
            "Happy (from Despicable Me 2)",
        )
        self.assertEqual(clean_title("99 Luftballons"), "99 Luftballons")
        self.assertEqual(
            clean_title("(Sittin' On) The Dock of the Bay"),
            "(Sittin' On) The Dock of the Bay",
        )


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

    def test_extracts_parenthesized_and_bracketed_trailing_credits(self) -> None:
        cases = (
            ("Lean On (feat. MØ & DJ Snake)", "Lean On", ["MØ & DJ Snake"]),
            (
                "Get Lucky [feat. Pharrell Williams]",
                "Get Lucky",
                ["Pharrell Williams"],
            ),
            ("Song (ft. Artist)", "Song", ["Artist"]),
            ("Song (featuring Artist)", "Song", ["Artist"]),
            ("Song (FEAT. Artist)", "Song", ["Artist"]),
            ("Song (feat. Artist) (Live)", "Song", ["Artist"]),
            ("Song (Live) [feat. Artist]", "Song", ["Artist"]),
            ("Song (feat. Artist) - 2011 Remaster", "Song", ["Artist"]),
            (
                "Bad Guy (with Justin Bieber)",
                "Bad Guy (with Justin Bieber)",
                None,
            ),
            ("Song (feat.)", "Song (feat.)", None),
        )

        for track, expected_title, expected_artists in cases:
            with self.subTest(track=track):
                result = enrich_title(track)
                self.assertEqual(result.track, track)
                self.assertEqual(result.track_title_clean, expected_title)
                self.assertEqual(result.featured_artists, expected_artists)

    def test_returns_null_credit_and_preserves_source_track_without_credit(self) -> None:
        track = "SONG (LIVE)"

        result = enrich_title(track)

        self.assertIsNone(result.featured_artists)
        self.assertEqual(result.track, track)
        self.assertEqual(result.track_title_clean, "Song")
