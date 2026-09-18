import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lastfm_export.client import RecentTracksWindow
from lastfm_export.state import CheckpointStore, StateStoreError


class CheckpointStoreTests(unittest.TestCase):
    def test_reads_legacy_interval_keyed_full_resync_state(self) -> None:
        interval = RecentTracksWindow(1_514_764_800, 1_700_000_000)
        chunk = RecentTracksWindow(1_514_764_800, 1_546_300_800)

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "checkpoints": {},
                        "leases": {},
                        "full_resync": {
                            "alice": {
                                "1514764800:1700000000": [
                                    {
                                        "from": chunk.from_timestamp,
                                        "to": chunk.to_timestamp,
                                    }
                                ]
                            }
                        },
                        "full_resync_completed_at": {},
                    }
                )
            )

            store = CheckpointStore(directory)
            self.assertEqual(
                store.get_committed_full_resync_chunks(
                    "alice",
                    interval=interval,
                ),
                (chunk,),
            )

    def test_persists_separate_checkpoints_when_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            store.record_successful_to("alice", 100)
            store.record_successful_to("bob", 200)

            reopened = CheckpointStore(Path(directory))

            self.assertEqual(reopened.get_last_successful_to("alice"), 100)
            self.assertEqual(reopened.get_last_successful_to("bob"), 200)

    def test_leases_are_exclusive_per_user_and_expire(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            alice_lease = store.acquire_lease("alice", ttl_seconds=5)
            reopened = CheckpointStore(Path(directory))

            self.assertIsNotNone(alice_lease)
            self.assertIsNone(reopened.acquire_lease("alice", ttl_seconds=1))
            self.assertIsNotNone(reopened.acquire_lease("bob", ttl_seconds=1))
            self.assertTrue(alice_lease.release())
            self.assertIsNotNone(reopened.acquire_lease("alice", ttl_seconds=1))

            expiring = reopened.acquire_lease("carol", ttl_seconds=0.05)
            time.sleep(0.06)
            self.assertIsNotNone(expiring)
            self.assertIsNotNone(reopened.acquire_lease("carol", ttl_seconds=1))

    def test_failed_run_does_not_replace_last_successful_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            store.record_successful_to("alice", 100)

            with self.assertRaises(RuntimeError):
                raise RuntimeError("simulated run failure")

            reopened_after_failure = CheckpointStore(Path(directory))

            self.assertEqual(
                reopened_after_failure.get_last_successful_to("alice"),
                100,
            )

    def test_expired_lease_cannot_commit_a_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            store.record_successful_to("alice", 100)

            with patch("lastfm_export.state.time.time", return_value=10.0):
                lease = store.acquire_lease("alice", ttl_seconds=1)
            self.assertIsNotNone(lease)

            with patch("lastfm_export.state.time.time", return_value=12.0):
                with self.assertRaisesRegex(
                    StateStoreError,
                    "no active lease",
                ):
                    store.record_successful_to("alice", 200, lease=lease)

            self.assertEqual(store.get_last_successful_to("alice"), 100)


if __name__ == "__main__":
    unittest.main()
