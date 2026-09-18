import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lastfm_export.client import RecentTracksWindow
from lastfm_export.state import CheckpointStore, Lease
from lastfm_export.workflow import (
    FullResyncRunCoordinator,
    IncrementalRunCoordinator,
    ReconciliationWorkflow,
    calendar_year_chunks,
    select_reconciliation_workflow,
)


RUN_START = 1_768_867_200  # 2026-01-20T00:00:00Z
WATERMARK = 1_768_003_200  # 2026-01-10T00:00:00Z
EXPECTED_FROM = 1_767_398_400  # 2026-01-03T00:00:00Z


class FakeExtraction:
    def __init__(self) -> None:
        self.calls: list[tuple[str, RecentTracksWindow]] = []

    def extract(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
    ) -> list[str]:
        self.calls.append((username, window))
        return ["extracted"]


class FakeLanding:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, RecentTracksWindow, list[str]]] = []
        self.fail = fail

    def land(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        records: list[str],
    ) -> None:
        self.calls.append((username, window, records))
        if self.fail:
            raise RuntimeError("landing failed")


class FailingChunkLanding(FakeLanding):
    def land(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        records: list[str],
    ) -> None:
        super().land(username, window=window, records=records)
        if len(self.calls) == 2:
            raise RuntimeError("landing failed")


class FullResyncRunCoordinatorTests(unittest.TestCase):
    def test_calendar_year_chunks_are_contiguous_for_extraction_and_landing(
        self,
    ) -> None:
        interval = RecentTracksWindow(
            1_577_836_800,  # 2020-01-01T00:00:00Z
            1_672_531_200,  # 2023-01-01T00:00:00Z
        )
        chunks = calendar_year_chunks(interval)

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            extraction = FakeExtraction()
            landing = FakeLanding()
            coordinator = FullResyncRunCoordinator(
                store,
                extraction,
                landing,
                clock=lambda: 1_672_531_200,
            )

            self.assertEqual(
                coordinator.run(
                    "alice",
                    start=interval.from_timestamp,
                    end=interval.to_timestamp,
                ),
                chunks,
            )
            self.assertEqual(
                [call[1] for call in extraction.calls],
                list(chunks),
            )
            self.assertEqual(
                [call[1] for call in landing.calls],
                list(chunks),
            )
            self.assertEqual(chunks[0].from_timestamp, interval.from_timestamp)
            self.assertEqual(chunks[-1].to_timestamp, interval.to_timestamp)
            for previous, current in zip(chunks, chunks[1:]):
                self.assertEqual(previous.to_timestamp, current.from_timestamp)
            self.assertIsNotNone(store.acquire_lease("alice", ttl_seconds=1))

    def test_failed_chunk_is_resumable_without_repeating_committed_chunk(
        self,
    ) -> None:
        interval = RecentTracksWindow(
            int(datetime(2020, 7, 1, tzinfo=timezone.utc).timestamp()),
            int(datetime(2022, 7, 1, tzinfo=timezone.utc).timestamp()),
        )
        chunks = calendar_year_chunks(interval)

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            extraction = FakeExtraction()
            landing = FailingChunkLanding()
            coordinator = FullResyncRunCoordinator(
                store,
                extraction,
                landing,
                clock=lambda: interval.to_timestamp,
            )

            with self.assertRaisesRegex(RuntimeError, "landing failed"):
                coordinator.run(
                    "alice",
                    start=interval.from_timestamp,
                    end=interval.to_timestamp,
                )

            self.assertEqual(
                store.get_committed_full_resync_chunks(
                    "alice",
                    interval=interval,
                ),
                (chunks[0],),
            )

            coordinator.run(
                "alice",
                start=interval.from_timestamp,
                end=interval.to_timestamp,
            )

            self.assertEqual(
                [call[1] for call in extraction.calls],
                [chunks[0], chunks[1], chunks[1], chunks[2]],
            )
            self.assertEqual(
                store.get_committed_full_resync_chunks(
                    "alice",
                    interval=interval,
                ),
                chunks,
            )
            self.assertEqual(
                store.get_last_full_resync_at("alice"),
                interval.to_timestamp,
            )

    def test_full_resync_releases_lease_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            landing = FakeLanding(fail=True)
            coordinator = FullResyncRunCoordinator(
                store,
                FakeExtraction(),
                landing,
                clock=lambda: 1_700_000_000,
            )

            with self.assertRaisesRegex(RuntimeError, "landing failed"):
                coordinator.run(
                    "alice",
                    start=1_577_836_800,
                    end=1_609_459_200,
                )

            self.assertIsNotNone(store.acquire_lease("alice", ttl_seconds=1))


class ReconciliationSelectionTests(unittest.TestCase):
    def test_cadence_due_interval_selects_full_resync_workflow(self) -> None:
        self.assertEqual(
            select_reconciliation_workflow(
                explicit_full_resync=False,
                last_full_resync_at=1_700_000_000,
                now=1_700_000_000 + 30 * 24 * 60 * 60,
                cadence_days=30,
            ),
            ReconciliationWorkflow.FULL_RESYNC,
        )

    def test_explicit_full_resync_selects_full_resync_workflow(self) -> None:
        self.assertEqual(
            select_reconciliation_workflow(
                explicit_full_resync=True,
                last_full_resync_at=1_700_000_000,
                now=1_700_000_001,
                cadence_days=30,
            ),
            ReconciliationWorkflow.FULL_RESYNC,
        )

    def test_interval_not_cadence_due_selects_no_reconciliation_workflow(
        self,
    ) -> None:
        self.assertIsNone(
            select_reconciliation_workflow(
                explicit_full_resync=False,
                last_full_resync_at=1_700_000_000,
                now=1_700_000_001,
                cadence_days=30,
            )
        )


class RecordingLease:
    def __init__(
        self,
        lease: Lease,
        owner: "RecordingCheckpointStore",
        username: str,
    ) -> None:
        self._lease = lease
        self._owner = owner
        self._username = username

    def release(self) -> bool:
        self._owner.released.append(self._username)
        return self._lease.release()


class RecordingCheckpointStore:
    def __init__(self, directory: Path) -> None:
        self._store = CheckpointStore(directory)
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.lease_active = True

    def get_last_successful_to(self, username: str) -> int | None:
        return self._store.get_last_successful_to(username)

    def record_successful_to(
        self,
        username: str,
        to: int,
        *,
        lease: RecordingLease | None = None,
    ) -> None:
        if lease is None:
            self._store.record_successful_to(username, to)
            return
        self._store.record_successful_to(
            username,
            to,
            lease=lease._lease,
        )

    def is_lease_active(self, lease: RecordingLease) -> bool:
        return self.lease_active and self._store.is_lease_active(lease._lease)

    def acquire_lease(
        self,
        username: str,
        *,
        ttl_seconds: float,
    ) -> RecordingLease | None:
        self.acquired.append(username)
        lease = self._store.acquire_lease(username, ttl_seconds=ttl_seconds)
        return (
            None
            if lease is None
            else RecordingLease(lease, self, username)
        )


class ExpiringExtraction(FakeExtraction):
    def __init__(self, store: RecordingCheckpointStore) -> None:
        super().__init__()
        self._store = store

    def extract(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
    ) -> list[str]:
        records = super().extract(username, window=window)
        self._store.lease_active = False
        return records


class IncrementalRunCoordinatorTests(unittest.TestCase):
    def test_rejects_non_finite_overlap_and_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecordingCheckpointStore(Path(directory))
            extraction = FakeExtraction()
            landing = FakeLanding()

            for overlap in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(overlap=overlap):
                    with self.assertRaisesRegex(
                        ValueError,
                        "overlap_days must be finite",
                    ):
                        IncrementalRunCoordinator(
                            store,
                            extraction,
                            landing,
                            overlap_days=overlap,
                        )

            for timestamp in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(timestamp=timestamp):
                    coordinator = IncrementalRunCoordinator(
                        store,
                        extraction,
                        landing,
                        overlap_days=7,
                        clock=lambda timestamp=timestamp: timestamp,
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "run-start must be finite",
                    ):
                        coordinator.run("alice")

                    coordinator = IncrementalRunCoordinator(
                        store,
                        extraction,
                        landing,
                        overlap_days=7,
                        clock=lambda: RUN_START,
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "since must be finite",
                    ):
                        coordinator.run("alice", since=timestamp)

    def test_overlap_window_is_identical_for_extraction_and_landing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecordingCheckpointStore(Path(directory))
            store.record_successful_to("alice", WATERMARK)
            extraction = FakeExtraction()
            landing = FakeLanding()
            coordinator = IncrementalRunCoordinator(
                store,
                extraction,
                landing,
                overlap_days=7,
                clock=lambda: RUN_START,
            )

            window = coordinator.run("alice")

            expected = RecentTracksWindow(EXPECTED_FROM, RUN_START)
            self.assertEqual(window, expected)
            self.assertEqual(extraction.calls, [("alice", expected)])
            self.assertEqual(
                landing.calls,
                [("alice", expected, ["extracted"])],
            )
            self.assertIs(extraction.calls[0][1], landing.calls[0][1])
            self.assertEqual(store.get_last_successful_to("alice"), RUN_START)
            self.assertEqual(store.acquired, ["alice"])
            self.assertEqual(store.released, ["alice"])

    def test_explicit_since_replaces_watermark_and_keeps_run_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecordingCheckpointStore(Path(directory))
            store.record_successful_to("alice", WATERMARK)
            extraction = FakeExtraction()
            landing = FakeLanding()
            coordinator = IncrementalRunCoordinator(
                store,
                extraction,
                landing,
                overlap_days=7,
                clock=lambda: RUN_START,
            )

            coordinator.run("alice", since="2026-01-05T00:00:00Z")

            expected = RecentTracksWindow(
                1_767_571_200,
                RUN_START,
            )
            self.assertEqual(extraction.calls, [("alice", expected)])
            self.assertEqual(landing.calls[0][1], expected)

    def test_fractional_overlap_rounds_up_to_preserve_configured_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecordingCheckpointStore(Path(directory))
            store.record_successful_to("alice", WATERMARK)
            extraction = FakeExtraction()
            landing = FakeLanding()
            coordinator = IncrementalRunCoordinator(
                store,
                extraction,
                landing,
                overlap_days=1.5 / (24 * 60 * 60),
                clock=lambda: RUN_START,
            )

            coordinator.run("alice")

            expected = RecentTracksWindow(WATERMARK - 2, RUN_START)
            self.assertEqual(extraction.calls, [("alice", expected)])
            self.assertEqual(landing.calls[0][1], expected)

    def test_landing_failure_preserves_checkpoint_and_releases_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecordingCheckpointStore(Path(directory))
            store.record_successful_to("alice", WATERMARK)
            extraction = FakeExtraction()
            landing = FakeLanding(fail=True)
            coordinator = IncrementalRunCoordinator(
                store,
                extraction,
                landing,
                overlap_days=7,
                clock=lambda: RUN_START,
            )

            with self.assertRaisesRegex(RuntimeError, "landing failed"):
                coordinator.run("alice")

            self.assertEqual(store.get_last_successful_to("alice"), WATERMARK)
            self.assertEqual(store.acquired, ["alice"])
            self.assertEqual(store.released, ["alice"])

    def test_expired_lease_stops_before_landing_and_checkpoint_advancement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecordingCheckpointStore(Path(directory))
            store.record_successful_to("alice", WATERMARK)
            extraction = ExpiringExtraction(store)
            landing = FakeLanding()
            coordinator = IncrementalRunCoordinator(
                store,
                extraction,
                landing,
                overlap_days=7,
                clock=lambda: RUN_START,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "lease expired or was lost",
            ):
                coordinator.run("alice")

            self.assertEqual(landing.calls, [])
            self.assertEqual(store.get_last_successful_to("alice"), WATERMARK)
            self.assertEqual(store.acquired, ["alice"])
            self.assertEqual(store.released, ["alice"])


if __name__ == "__main__":
    unittest.main()
