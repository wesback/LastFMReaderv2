import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lastfm_export.client import RecentTracksWindow
from lastfm_export.state import CheckpointStore, StateStoreError


class CheckpointStoreTests(unittest.TestCase):
    def test_acquire_lease_rejects_invalid_ttls_without_changing_state(self) -> None:
        invalid_ttls = (float("nan"), float("inf"), float("-inf"), 0, -1)

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            store.acquire_lease("alice", ttl_seconds=5)
            original_state = store.state_path.read_bytes()

            for ttl_seconds in invalid_ttls:
                with self.subTest(ttl_seconds=ttl_seconds):
                    with self.assertRaises(ValueError):
                        store.acquire_lease("bob", ttl_seconds=ttl_seconds)
                    self.assertEqual(store.state_path.read_bytes(), original_state)

    def test_renew_lease_rejects_invalid_ttls_without_changing_state(self) -> None:
        invalid_ttls = (float("nan"), float("inf"), float("-inf"), 0, -1)

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            lease = store.acquire_lease("alice", ttl_seconds=5)
            original_state = store.state_path.read_bytes()

            for ttl_seconds in invalid_ttls:
                with self.subTest(ttl_seconds=ttl_seconds):
                    with self.assertRaises(ValueError):
                        store.renew_lease(lease, ttl_seconds=ttl_seconds)
                    self.assertEqual(store.state_path.read_bytes(), original_state)

    def test_finite_ttls_create_and_renew_finite_json_expirations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            lease = store.acquire_lease("alice", ttl_seconds=5)
            self.assertTrue(
                math.isfinite(
                    json.loads(store.state_path.read_text())["leases"]["alice"][
                        "expires_at"
                    ]
                )
            )

            renewed = store.renew_lease(lease, ttl_seconds=10)

            self.assertIsNotNone(renewed)
            self.assertTrue(
                math.isfinite(
                    json.loads(store.state_path.read_text())["leases"]["alice"][
                        "expires_at"
                    ]
                )
            )

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

    def test_failed_atomic_checkpoint_replace_preserves_previous_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            store.record_successful_to("alice", 100)
            original_state = store.state_path.read_bytes()

            with (
                patch(
                    "lastfm_export.state.os.replace",
                    side_effect=OSError("simulated replace failure"),
                ),
                self.assertRaises(StateStoreError),
            ):
                store.record_successful_to("alice", 200)

            self.assertEqual(store.state_path.read_bytes(), original_state)
            self.assertEqual(store.get_last_successful_to("alice"), 100)

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

    def test_msvcrt_backend_excludes_duplicate_user_leases_without_fcntl(self) -> None:
        backend = SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=Mock(),
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("lastfm_export.state._fcntl", None),
                patch("lastfm_export.state._msvcrt", backend),
            ):
                store = CheckpointStore(directory)
                reopened = CheckpointStore(directory)

                lease = store.acquire_lease("alice", ttl_seconds=5)

                self.assertIsNotNone(lease)
                self.assertIsNone(reopened.acquire_lease("alice", ttl_seconds=5))

            self.assertEqual(
                [call.args[1] for call in backend.locking.call_args_list],
                [backend.LK_NBLCK, backend.LK_UNLCK] * 2,
            )

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
