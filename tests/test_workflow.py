import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from lastfm_export.client import RecentTracksWindow
from lastfm_export.state import CheckpointStore, Lease
from lastfm_export.workflow import (
    FullResyncRunCoordinator,
    IncrementalRunCoordinator,
    IncrementalRunError,
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


class FailingOnceExtraction(FakeExtraction):
    def __init__(self, failed_from: int) -> None:
        super().__init__()
        self.failed_from = failed_from
        self.failed = False

    def extract(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
    ) -> list[str]:
        records = super().extract(username, window=window)
        if window.from_timestamp == self.failed_from and not self.failed:
            self.failed = True
            raise RuntimeError("extraction failed")
        return records


class PagingExtraction:
    def __init__(
        self,
        clock: list[float],
        *,
        pages: int,
        store: CheckpointStore | None = None,
        check_concurrency_after_page: int | None = None,
        take_over_after_page: int | None = None,
    ) -> None:
        self.clock = clock
        self.pages = pages
        self.store = store
        self.check_concurrency_after_page = check_concurrency_after_page
        self.take_over_after_page = take_over_after_page
        self.second_lease = None
        self.calls: list[RecentTracksWindow] = []

    def extract(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        on_page: object = None,
    ) -> list[str]:
        self.calls.append(window)
        for page in range(1, self.pages + 1):
            self.clock[0] += 200
            if (
                self.store is not None
                and page == self.take_over_after_page
            ):
                state = json.loads(self.store.state_path.read_text())
                old_token = state["leases"][username]["token"]
                self.assert_true(self.store.release_lease(username, old_token))
                self.second_lease = self.store.acquire_lease(username)
                self.assert_true(self.second_lease is not None)
            if callable(on_page):
                on_page()
            if (
                self.store is not None
                and page == self.check_concurrency_after_page
            ):
                self.second_lease = self.store.acquire_lease(username)
        return ["extracted"]

    @staticmethod
    def assert_true(value: bool) -> None:
        if not value:
            raise AssertionError("paging extraction assertion failed")


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
    def test_full_resync_initializes_watermark_from_epoch(self) -> None:
        end = 1_609_459_200  # 2021-01-01T00:00:00Z

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            FullResyncRunCoordinator(
                store,
                FakeExtraction(),
                FakeLanding(),
                clock=lambda: end,
            ).run("alice", start=0, end=end)

            self.assertEqual(store.get_last_successful_to("alice"), end)

    def test_full_resync_advances_watermark_when_interval_covers_it(
        self,
    ) -> None:
        start = 1_577_836_800  # 2020-01-01T00:00:00Z
        end = 1_609_459_200  # 2021-01-01T00:00:00Z

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            store.record_successful_to("alice", start)

            FullResyncRunCoordinator(
                store,
                FakeExtraction(),
                FakeLanding(),
                clock=lambda: end,
            ).run("alice", start=start, end=end)

            self.assertEqual(store.get_last_successful_to("alice"), end)

    def test_full_resync_does_not_move_watermark_backwards(self) -> None:
        start = 1_577_836_800  # 2020-01-01T00:00:00Z
        end = 1_609_459_200  # 2021-01-01T00:00:00Z
        watermark = 1_640_995_200  # 2022-01-01T00:00:00Z

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            store.record_successful_to("alice", watermark)

            FullResyncRunCoordinator(
                store,
                FakeExtraction(),
                FakeLanding(),
                clock=lambda: end,
            ).run("alice", start=start, end=end)

            self.assertEqual(
                store.get_last_successful_to("alice"),
                watermark,
            )

    def test_full_resync_with_non_epoch_start_keeps_uninitialized_watermark(
        self,
    ) -> None:
        start = 1_577_836_800  # 2020-01-01T00:00:00Z
        end = 1_609_459_200  # 2021-01-01T00:00:00Z

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            FullResyncRunCoordinator(
                store,
                FakeExtraction(),
                FakeLanding(),
                clock=lambda: end,
            ).run("alice", start=start, end=end)

            self.assertIsNone(store.get_last_successful_to("alice"))

    def test_failed_full_resync_preserves_existing_watermark(self) -> None:
        start = 1_577_836_800  # 2020-01-01T00:00:00Z
        end = 1_672_531_200  # 2023-01-01T00:00:00Z
        watermark = 1_609_459_200  # 2021-01-01T00:00:00Z

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            store.record_successful_to("alice", watermark)
            coordinator = FullResyncRunCoordinator(
                store,
                FakeExtraction(),
                FailingChunkLanding(),
                clock=lambda: end,
            )

            with self.assertRaisesRegex(RuntimeError, "landing failed"):
                coordinator.run("alice", start=start, end=end)

            self.assertEqual(
                store.get_last_successful_to("alice"),
                watermark,
            )

    def test_renews_lease_between_pages_and_chunks(self) -> None:
        interval = RecentTracksWindow(
            1_577_836_800,  # 2020-01-01T00:00:00Z
            1_672_531_200,  # 2023-01-01T00:00:00Z
        )
        clock = [0.0]

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            extraction = PagingExtraction(clock, pages=3)
            coordinator = FullResyncRunCoordinator(
                store,
                extraction,
                FakeLanding(),
                clock=lambda: interval.to_timestamp,
            )

            with patch("lastfm_export.state.time.time", side_effect=lambda: clock[0]):
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
                    calendar_year_chunks(interval),
                )
                self.assertIsNotNone(store.get_last_full_resync_at("alice"))

            self.assertEqual(clock[0], 1800)

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

    def test_resume_matches_closed_chunks_when_run_end_changes(self) -> None:
        start = 1_514_764_800  # 2018-01-01T00:00:00Z
        first_end = 1_700_000_000
        second_end = 1_700_000_100
        failed_from = 1_577_836_800  # 2020-01-01T00:00:00Z

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            extraction = FailingOnceExtraction(failed_from)
            coordinator = FullResyncRunCoordinator(
                store,
                extraction,
                FakeLanding(),
                clock=lambda: second_end,
            )

            with self.assertRaisesRegex(RuntimeError, "extraction failed"):
                coordinator.run("alice", start=start, end=first_end)

            coordinator.run("alice", start=start, end=second_end)

            self.assertEqual(
                [window for _, window in extraction.calls[3:]],
                [
                    RecentTracksWindow(failed_from, 1_609_459_200),
                    RecentTracksWindow(1_609_459_200, 1_640_995_200),
                    RecentTracksWindow(1_640_995_200, 1_672_531_200),
                    RecentTracksWindow(1_672_531_200, second_end),
                ],
            )

    def test_completed_full_resync_starts_a_fresh_reconciliation(self) -> None:
        start = 1_514_764_800  # 2018-01-01T00:00:00Z
        first_end = 1_700_000_000
        second_end = 1_700_000_100

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            first_extraction = FakeExtraction()
            FullResyncRunCoordinator(
                store,
                first_extraction,
                FakeLanding(),
                clock=lambda: first_end,
            ).run("alice", start=start, end=first_end)

            second_extraction = FakeExtraction()
            FullResyncRunCoordinator(
                store,
                second_extraction,
                FakeLanding(),
                clock=lambda: second_end,
            ).run("alice", start=start, end=second_end)

            self.assertEqual(
                [window for _, window in second_extraction.calls],
                list(
                    calendar_year_chunks(
                        RecentTracksWindow(start, second_end)
                    )
                ),
            )

    def test_changed_start_only_reuses_exact_chunk_bounds(self) -> None:
        first_start = 1_514_764_800  # 2018-01-01T00:00:00Z
        second_start = 1_546_300_800  # 2019-01-01T00:00:00Z
        first_end = 1_700_000_000
        second_end = 1_700_000_100
        failed_from = 1_577_836_800  # 2020-01-01T00:00:00Z

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            extraction = FailingOnceExtraction(failed_from)
            coordinator = FullResyncRunCoordinator(
                store,
                extraction,
                FakeLanding(),
                clock=lambda: second_end,
            )

            with self.assertRaisesRegex(RuntimeError, "extraction failed"):
                coordinator.run("alice", start=first_start, end=first_end)

            coordinator.run("alice", start=second_start, end=second_end)

            self.assertEqual(
                [window for _, window in extraction.calls[3:]],
                [
                    RecentTracksWindow(failed_from, 1_609_459_200),
                    RecentTracksWindow(1_609_459_200, 1_640_995_200),
                    RecentTracksWindow(1_640_995_200, 1_672_531_200),
                    RecentTracksWindow(1_672_531_200, second_end),
                ],
            )


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

    def renew(self, ttl_seconds: float) -> "RecordingLease | None":
        if not self._owner.lease_active:
            return None
        renewed = self._lease.renew(ttl_seconds)
        return (
            None
            if renewed is None
            else RecordingLease(renewed, self._owner, self._username)
        )


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
    def test_renews_lease_after_each_page_for_a_long_run(self) -> None:
        clock = [0.0]
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            extraction = PagingExtraction(
                clock,
                pages=5,
                store=store,
                check_concurrency_after_page=2,
            )
            coordinator = IncrementalRunCoordinator(
                store,
                extraction,
                FakeLanding(),
                overlap_days=7,
                clock=lambda: RUN_START,
            )

            with patch("lastfm_export.state.time.time", side_effect=lambda: clock[0]):
                window = coordinator.run("alice")

                self.assertIsNone(extraction.second_lease)
                self.assertEqual(
                    store.get_last_successful_to("alice"),
                    window.to_timestamp,
                )
            self.assertEqual(clock[0], 1000)

    def test_lease_takeover_between_pages_aborts_without_advancing_watermark(
        self,
    ) -> None:
        clock = [0.0]
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            extraction = PagingExtraction(
                clock,
                pages=5,
                store=store,
                take_over_after_page=1,
            )
            coordinator = IncrementalRunCoordinator(
                store,
                extraction,
                FakeLanding(),
                overlap_days=7,
                clock=lambda: RUN_START,
            )

            with patch("lastfm_export.state.time.time", side_effect=lambda: clock[0]):
                with self.assertRaisesRegex(
                    IncrementalRunError,
                    "incremental run lease expired or was lost",
                ):
                    coordinator.run("alice")

                self.assertIsNone(store.get_last_successful_to("alice"))

    def test_unrenewed_lease_expires_after_default_ttl(self) -> None:
        clock = [0.0]
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory))
            with patch("lastfm_export.state.time.time", side_effect=lambda: clock[0]):
                self.assertIsNotNone(store.acquire_lease("alice"))
                clock[0] = 301
                self.assertIsNotNone(store.acquire_lease("alice"))

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
