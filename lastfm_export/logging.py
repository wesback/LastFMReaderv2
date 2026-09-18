"""Secret-safe serialization for structured exporter logs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

REDACTION_MARKER = "[REDACTED]"
SecretValues = Sequence[str]


class SecretRedactor:
    """Replace configured secret values wherever they occur in log data."""

    def __init__(self, secrets: SecretValues = ()) -> None:
        configured = tuple(secret for secret in secrets if secret)
        patterns = sorted(configured, key=len, reverse=True)
        self._pattern = (
            re.compile("|".join(re.escape(secret) for secret in patterns))
            if patterns
            else None
        )

    def redact_text(self, value: str) -> str:
        """Redact every configured secret occurrence in text."""
        if self._pattern is None:
            return value
        return self._pattern.sub(REDACTION_MARKER, value)

    def redact(self, value: Any) -> Any:
        """Return a recursively redacted, JSON-compatible value."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {
                self.redact_text(str(key)): self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            return [self.redact(item) for item in value]
        return value


@dataclass(frozen=True)
class RunSummary:
    """Counters and outcome for one user's extraction run."""

    username: str
    outcome: str
    rows_extracted: int
    rows_skipped_now_playing: int
    pages_fetched: int
    retry_count: int
    retry_causes: tuple[str, ...]
    duration_seconds: float

    def as_dict(self) -> dict[str, Any]:
        """Return the stable structured-log field names for this summary."""
        return asdict(self)


def _serialize_object(value: Mapping[str, Any], redactor: SecretRedactor) -> str:
    redacted = redactor.redact(value)
    if not isinstance(redacted, Mapping):
        raise TypeError("structured log data must be a mapping")
    return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def serialize_log_record(
    fields: Mapping[str, Any],
    *,
    secrets: SecretValues = (),
) -> str:
    """Serialize one structured log record as one redacted JSON object."""
    return _serialize_object(fields, SecretRedactor(secrets))


def serialize_run_summary(
    summary: RunSummary | Mapping[str, Any],
    *,
    secrets: SecretValues = (),
) -> str:
    """Serialize one redacted run summary as one JSON object."""
    fields = summary.as_dict() if isinstance(summary, RunSummary) else summary
    return _serialize_object(fields, SecretRedactor(secrets))


def exception_log_record(
    exception: BaseException,
    *,
    fields: Mapping[str, Any] | None = None,
    secrets: SecretValues = (),
) -> dict[str, Any]:
    """Build a structured record retaining exception type and safe text."""
    record: dict[str, Any] = dict(fields or {})
    redactor = SecretRedactor(secrets)
    exception_type = type(exception).__name__
    exception_message = redactor.redact_text(str(exception))
    record["exception"] = {
        "type": exception_type,
        "message": exception_message,
    }
    return redactor.redact(record)


def serialize_exception_log_record(
    exception: BaseException,
    *,
    fields: Mapping[str, Any] | None = None,
    secrets: SecretValues = (),
) -> str:
    """Serialize an exception-derived structured log record as JSON."""
    return _serialize_object(
        exception_log_record(exception, fields=fields, secrets=secrets),
        SecretRedactor(secrets),
    )


__all__ = [
    "REDACTION_MARKER",
    "RunSummary",
    "SecretRedactor",
    "exception_log_record",
    "serialize_exception_log_record",
    "serialize_log_record",
    "serialize_run_summary",
]
