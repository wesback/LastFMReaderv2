"""Installable Last.fm scrobble exporter package."""

from .config import ConfigurationError, ExporterConfig, UserConfig, load_config
from .client import (
    DEFAULT_RETRY_DELAY,
    LASTFM_ENDPOINT,
    RETRYABLE_ERROR_CODES,
    LastFMAPIError,
    LastFMClient,
    LastFMError,
    RecentTracksWindow,
)
from .cli import RunRequest
from .logging import (
    REDACTION_MARKER,
    RunSummary,
    SecretRedactor,
    exception_log_record,
    serialize_exception_log_record,
    serialize_log_record,
    serialize_run_summary,
)
from .progress import ProgressReporter
from .retrieval import RecentTracksPaginator, Track, retrieve_scrobbles
from .state import CheckpointStore, Lease, StateStoreError
from .workflow import (
    CheckpointPort,
    ExtractionPort,
    FullResyncCheckpointPort,
    FullResyncCoordinator,
    FullResyncRunCoordinator,
    FullResyncRunError,
    IncrementalRunCoordinator,
    IncrementalRunError,
    LandingPort,
    LandingWriterPort,
    ReconciliationWorkflow,
    RecentTracksExtraction,
    calendar_year_chunks,
    run_full_resync,
    run_incremental,
    select_reconciliation_workflow,
)
from .landing import LandingRow, build_landing_row, natural_key_surrogate, normalize_track
from .output import (
    FsspecLandingWriter,
    LocalDestinationWriter,
    LocalLandingWriter,
    NORMALIZED_COLUMNS,
    write_landing,
)
from .titles import (
    DEFAULT_ANNOTATION_KEYWORDS,
    TitleEnrichment,
    clean_title,
    enrich_title,
)

__version__ = "0.1.0"

__all__ = [
    "CheckpointStore",
    "CheckpointPort",
    "ConfigurationError",
    "DEFAULT_ANNOTATION_KEYWORDS",
    "DEFAULT_RETRY_DELAY",
    "ExporterConfig",
    "ExtractionPort",
    "FsspecLandingWriter",
    "FullResyncCheckpointPort",
    "FullResyncCoordinator",
    "FullResyncRunCoordinator",
    "FullResyncRunError",
    "Lease",
    "LASTFM_ENDPOINT",
    "RETRYABLE_ERROR_CODES",
    "REDACTION_MARKER",
    "RunSummary",
    "RunRequest",
    "LastFMAPIError",
    "LastFMClient",
    "LastFMError",
    "LandingRow",
    "LandingPort",
    "LandingWriterPort",
    "LocalDestinationWriter",
    "LocalLandingWriter",
    "NORMALIZED_COLUMNS",
    "ProgressReporter",
    "ReconciliationWorkflow",
    "RecentTracksPaginator",
    "RecentTracksExtraction",
    "RecentTracksWindow",
    "SecretRedactor",
    "StateStoreError",
    "TitleEnrichment",
    "Track",
    "UserConfig",
    "__version__",
    "clean_title",
    "build_landing_row",
    "enrich_title",
    "exception_log_record",
    "serialize_exception_log_record",
    "serialize_log_record",
    "serialize_run_summary",
    "write_landing",
    "load_config",
    "natural_key_surrogate",
    "normalize_track",
    "retrieve_scrobbles",
    "calendar_year_chunks",
    "IncrementalRunCoordinator",
    "IncrementalRunError",
    "run_full_resync",
    "run_incremental",
    "select_reconciliation_workflow",
]
