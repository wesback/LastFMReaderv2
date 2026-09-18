"""Loading and validation of the exporter's TOML configuration."""

from __future__ import annotations

import os
import tomllib
import warnings
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    """Raised when an exporter configuration cannot be loaded or validated."""


_SUPPORTED_FORMATS = frozenset(
    {"parquet", "csv", "jsonl", "json", "json_lines", "ndjson"}
)
_SUPPORTED_DESTINATION_SCHEMES = frozenset({"file", "s3", "az", "abfss"})


@dataclass(frozen=True)
class UserConfig:
    """Validated settings for one tracked Last.fm user."""

    username: str
    destination: str
    timezone: ZoneInfo

    @property
    def timezone_name(self) -> str:
        """Return the configured IANA name."""
        return self.timezone.key

    @property
    def zoneinfo(self) -> ZoneInfo:
        """Compatibility alias for consumers that name the resolved value."""
        return self.timezone


@dataclass(frozen=True)
class ExporterConfig:
    """Validated global settings and tracked users.

    ``api_key_env`` is deliberately only the name of an environment variable.
    The value stored in that variable is checked while loading but is never
    copied into this object.
    """

    api_key_env: str
    format: str
    destination: str
    overlap: int | float
    reconciliation_cadence: int | float
    users: tuple[UserConfig, ...]

    @property
    def api_key_env_var(self) -> str:
        """Compatibility alias for the environment-variable reference."""
        return self.api_key_env

    def user(self, username: str) -> UserConfig:
        """Return a configured user or raise a deterministic validation error."""
        for user in self.users:
            if user.username == username:
                return user
        raise ConfigurationError(f"unknown selected user: {username!r}")


def load_config(
    path: str | Path,
    *,
    selected_user: str | None = None,
    environ: Mapping[str, str] | None = None,
    require_api_key: bool = True,
) -> ExporterConfig:
    """Load, validate, and normalize a TOML exporter configuration.

    Set ``require_api_key`` to ``False`` for read-only commands such as status
    that inspect durable state without making Last.fm requests.

    The canonical TOML shape is::

        [global]
        api_key_env = "LASTFM_API_KEY"
        format = "parquet"
        destination = "file:///exports"
        overlap = 7
        reconciliation_cadence = 7

        [[users]]
        username = "alice"
        timezone = "Europe/Brussels"
        destination = "file:///alice"

    ``[defaults]`` and ``[[tracked_users]]`` are accepted as equivalent
    section names so configuration can use the terminology from the product
    requirements. Top-level global fields are also accepted for a small,
    unambiguous configuration.
    """
    config_path = Path(path)
    try:
        with config_path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"malformed TOML configuration: {error}") from error
    except OSError as error:
        raise ConfigurationError(
            f"unable to read configuration file {config_path}: {error}"
        ) from error

    if not isinstance(document, dict):
        raise ConfigurationError("configuration must be a TOML table")

    global_values = _global_values(document)
    api_key_env = _required_string(global_values, "api_key_env")
    format_name = _required_string(global_values, "format")
    _validate_format(format_name)
    destination = _required_string(global_values, "destination")
    _validate_destination(destination)
    overlap = _required_setting(global_values, "overlap")
    reconciliation_cadence = _required_setting(
        global_values,
        "reconciliation_cadence",
    )

    records = _user_records(document)
    if not records:
        raise ConfigurationError("configuration field 'users' must contain one or more users")

    users: list[UserConfig] = []
    seen_usernames: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ConfigurationError(f"users[{index}] must be a TOML table")
        username = _required_string(
            record,
            "username",
            field_name=f"users[{index}].username",
        )
        if username in seen_usernames:
            raise ConfigurationError(f"duplicate tracked user: {username!r}")
        seen_usernames.add(username)

        user_destination = record.get("destination", destination)
        if not isinstance(user_destination, str) or not user_destination.strip():
            raise ConfigurationError(
                f"users[{index}].destination must be a non-empty string"
            )
        _validate_destination(
            user_destination,
            field_name=f"users[{index}].destination",
        )

        timezone_name = record.get("timezone", record.get("time_zone"))
        if timezone_name is None:
            timezone_name = "UTC"
            warnings.warn(
                f"tracked user {username!r} has no timezone; using UTC",
                UserWarning,
                stacklevel=2,
            )
        elif not isinstance(timezone_name, str) or not timezone_name.strip():
            raise ConfigurationError(
                f"users[{index}].timezone must be a non-empty IANA time zone"
            )

        try:
            user_zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ConfigurationError(
                f"users[{index}].timezone has invalid IANA time zone "
                f"{timezone_name!r}"
            ) from error

        users.append(
            UserConfig(
                username=username,
                destination=user_destination,
                timezone=user_zone,
            )
        )

    environment = os.environ if environ is None else environ
    if require_api_key and not environment.get(api_key_env):
        raise ConfigurationError(
            f"API-key environment variable {api_key_env!r} is unset"
        )

    config = ExporterConfig(
        api_key_env=api_key_env,
        format=format_name,
        destination=destination,
        overlap=overlap,
        reconciliation_cadence=reconciliation_cadence,
        users=tuple(users),
    )
    if selected_user is not None:
        config.user(selected_user)
    return config


def _global_values(document: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in document.items():
        if key not in {
            "global",
            "defaults",
            "export",
            "config",
            "users",
            "tracked_users",
            "tracked_user",
            "user",
        }:
            values[key] = value
    for section_name in ("global", "defaults", "export", "config"):
        section = document.get(section_name)
        if section is not None:
            if not isinstance(section, dict):
                raise ConfigurationError(
                    f"configuration section [{section_name}] must be a table"
                )
            values.update(section)
    aliases = {
        "api_key_env_var": "api_key_env",
        "api_key_environment_variable": "api_key_env",
        "overlap_days": "overlap",
        "reconciliation_cadence_days": "reconciliation_cadence",
        "reconciliation_days": "reconciliation_cadence",
    }
    for alias, canonical in aliases.items():
        if canonical not in values and alias in values:
            values[canonical] = values[alias]
    return values


def _user_records(document: dict[str, Any]) -> list[Any]:
    for key in ("users", "tracked_users", "tracked_user", "user"):
        if key in document:
            records = document[key]
            if isinstance(records, list):
                return records
            raise ConfigurationError(f"configuration field '{key}' must be an array")
    return []


def _required_string(
    values: dict[str, Any],
    field: str,
    *,
    field_name: str | None = None,
) -> str:
    display_name = field if field_name is None else field_name
    if field not in values:
        raise ConfigurationError(
            f"missing required configuration field '{display_name}'"
        )
    value = values[field]
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(
            f"configuration field '{display_name}' must be a non-empty string"
        )
    return value


def _required_setting(values: dict[str, Any], field: str) -> int | float:
    if field not in values:
        raise ConfigurationError(f"missing required configuration field '{field}'")
    value = values[field]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigurationError(
            f"configuration field '{field}' must be a number or string"
        )
    if isinstance(value, str) and not value.strip():
        raise ConfigurationError(f"configuration field '{field}' must not be empty")
    if isinstance(value, str):
        try:
            numeric_value = float(value.strip())
        except ValueError:
            raise ConfigurationError(
                f"configuration field '{field}' must be a finite non-negative number"
            ) from None
        if not isfinite(numeric_value) or numeric_value < 0:
            raise ConfigurationError(
                f"configuration field '{field}' must be a finite non-negative number"
            )
        return (
            int(numeric_value)
            if numeric_value.is_integer()
            else numeric_value
        )
    if isinstance(value, float) and not isfinite(value):
        raise ConfigurationError(
            f"configuration field '{field}' must be a finite non-negative number"
        )
    if value < 0:
        raise ConfigurationError(
            f"configuration field '{field}' must not be negative"
        )
    return value


def _validate_format(value: str) -> None:
    normalized = value.casefold().replace("-", "_").replace(" ", "_")
    if normalized not in _SUPPORTED_FORMATS:
        supported = ", ".join(sorted(_SUPPORTED_FORMATS))
        raise ConfigurationError(
            f"configuration field 'format' must be one of {supported}"
        )


def _validate_destination(
    value: str,
    *,
    field_name: str = "destination",
) -> None:
    try:
        scheme = urlsplit(value).scheme.casefold()
    except ValueError as error:
        raise ConfigurationError(
            f"configuration field '{field_name}' is not a valid destination URI"
        ) from error
    if scheme and scheme not in _SUPPORTED_DESTINATION_SCHEMES:
        supported = ", ".join(sorted(_SUPPORTED_DESTINATION_SCHEMES))
        raise ConfigurationError(
            f"configuration field '{field_name}' has unsupported destination "
            f"scheme {scheme!r}; expected one of {supported} or a local path"
        )


__all__ = [
    "ConfigurationError",
    "ExporterConfig",
    "UserConfig",
    "load_config",
]
