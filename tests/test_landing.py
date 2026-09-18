import unittest
from datetime import timezone

from lastfm_export import (
    LandingRow,
    TitleEnrichment,
    natural_key_surrogate,
    normalize_track,
)


def extracted_track(
    *,
    timestamp: int = 1711845000,
    artist: str = "The Source",
    title: str = "ORIGINAL TITLE (LIVE)",
    artist_mbid: str = "artist-mbid",
    track_mbid: str = "track-mbid",
    album: str = "Album",
    album_mbid: str = "album-mbid",
    url: str = "https://last.fm/track",
) -> dict[str, object]:
    return {
        "artist": {"#text": artist, "mbid": artist_mbid},
        "name": title,
        "mbid": track_mbid,
        "album": {"#text": album, "mbid": album_mbid},
        "date": {"uts": str(timestamp)},
        "url": url,
    }


class LandingRowTests(unittest.TestCase):
    def test_normalizes_every_raw_schema_field_without_cleaning_source_values(self) -> None:
        source = extracted_track()
        enrichment = TitleEnrichment(
            track="ORIGINAL TITLE (LIVE)",
            track_title_clean="Original Title",
            featured_artists=["Guest Artist"],
        )

        row = normalize_track(source, "alice", "Europe/Brussels", enrichment)

        self.assertIsInstance(row, LandingRow)
        self.assertEqual(row.username, "alice")
        self.assertEqual(row.artist, "The Source")
        self.assertEqual(row.track, "ORIGINAL TITLE (LIVE)")
        self.assertEqual(row.artist_mbid, "artist-mbid")
        self.assertEqual(row.track_mbid, "track-mbid")
        self.assertEqual(row.album, "Album")
        self.assertEqual(row.album_mbid, "album-mbid")
        self.assertEqual(row.scrobbled_at_uts, 1711845000)
        self.assertEqual(
            row.scrobbled_at_utc.isoformat(),
            "2024-03-31T00:30:00+00:00",
        )
        self.assertEqual(
            row.scrobbled_at_local.isoformat(),
            "2024-03-31T01:30:00+01:00",
        )
        self.assertEqual(row.url, "https://last.fm/track")
        self.assertEqual(row.track_title_clean, "Original Title")
        self.assertEqual(row.featured_artists, ["Guest Artist"])
        self.assertEqual(row.scrobbled_at_utc.tzinfo, timezone.utc)
        self.assertEqual(row.as_dict()["event_id"], row.event_id)

    def test_natural_key_ignores_enrichment_and_localization_fields(self) -> None:
        first = normalize_track(
            extracted_track(),
            "alice",
            "UTC",
            TitleEnrichment("ORIGINAL TITLE (LIVE)", "Original Title", None),
        )
        second_source = extracted_track(
            artist_mbid="changed-artist",
            track_mbid="changed-track",
            album="Changed Album",
            album_mbid="changed-album",
            url="https://last.fm/changed",
        )
        second = normalize_track(
            second_source,
            "alice",
            "Europe/Brussels",
            TitleEnrichment(
                "ORIGINAL TITLE (LIVE)",
                "Different Clean Title",
                ["Different Guest"],
            ),
        )

        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(
            natural_key_surrogate(
                username="alice",
                scrobbled_at_uts=1711845000,
                artist="The Source",
                track="ORIGINAL TITLE (LIVE)",
            ),
            first.event_id,
        )

    def test_each_natural_key_component_changes_the_surrogate(self) -> None:
        values = {
            "username": "alice",
            "scrobbled_at_uts": 1711845000,
            "artist": "The Source",
            "track": "ORIGINAL TITLE (LIVE)",
        }
        original = natural_key_surrogate(**values)

        for component, replacement in (
            ("username", "bob"),
            ("scrobbled_at_uts", 1711845001),
            ("artist", "Another Source"),
            ("track", "Another Title"),
        ):
            changed = dict(values)
            changed[component] = replacement
            with self.subTest(component=component):
                self.assertNotEqual(natural_key_surrogate(**changed), original)

    def test_converts_both_sides_of_brussels_dst_transition(self) -> None:
        cases = (
            (1711845000, "2024-03-31T01:30:00+01:00"),
            (1711848600, "2024-03-31T03:30:00+02:00"),
            (1729989000, "2024-10-27T02:30:00+02:00"),
            (1729992600, "2024-10-27T02:30:00+01:00"),
        )
        for timestamp, expected_local in cases:
            with self.subTest(timestamp=timestamp):
                row = normalize_track(
                    extracted_track(timestamp=timestamp),
                    "alice",
                    "Europe/Brussels",
                    TitleEnrichment(
                        "ORIGINAL TITLE (LIVE)",
                        "Original Title",
                        None,
                    ),
                )
                self.assertEqual(row.scrobbled_at_utc.tzinfo, timezone.utc)
                self.assertEqual(
                    row.scrobbled_at_utc.isoformat(),
                    (
                        row.scrobbled_at_utc.astimezone(timezone.utc).isoformat()
                    ),
                )
                self.assertEqual(row.scrobbled_at_local.isoformat(), expected_local)


if __name__ == "__main__":
    unittest.main()
