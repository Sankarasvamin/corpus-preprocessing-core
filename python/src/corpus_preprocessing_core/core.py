from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = (
    "record_id",
    "dataset_id",
    "source_file",
    "source_offset",
    "record_type",
    "event_date",
    "title",
    "raw_text",
    "clean_text",
    "metadata",
    "parser_version",
    "schema_version",
)
ALLOWED_FIELDS = frozenset(REQUIRED_FIELDS)
RECORD_TYPES = frozenset({"article", "comment", "reply"})
DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MAX_SOURCE_OFFSET = 18_446_744_073_709_551_615


class ValidationError(ValueError):
    def __init__(self, error_type: str, message: str, field: str | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.field = field


class LineValidationError(ValidationError):
    def __init__(self, line_number: int, error: ValidationError):
        field = f" field={error.field}" if error.field else ""
        super().__init__(error.error_type, f"line {line_number} [{error.error_type}]{field}: {error}", error.field)
        self.line_number = line_number


@dataclass(frozen=True)
class RecordV1:
    record_id: str
    dataset_id: str
    source_file: str
    source_offset: int
    record_type: str
    event_date: str | None
    title: str | None
    raw_text: str
    clean_text: str | None
    metadata: dict[str, Any]
    parser_version: str
    schema_version: str

    @classmethod
    def from_dict(cls, value: Any) -> "RecordV1":
        if not isinstance(value, dict):
            raise ValidationError("type_error", "record must be a JSON object")

        for field in REQUIRED_FIELDS:
            if field not in value:
                raise ValidationError("missing_field", "required field is missing", field)
        unexpected = sorted(set(value) - ALLOWED_FIELDS)
        if unexpected:
            raise ValidationError("unexpected_field", "field is not defined by record-v1", unexpected[0])

        for field in ("record_id", "dataset_id", "source_file", "raw_text", "parser_version", "schema_version"):
            if not isinstance(value[field], str):
                raise ValidationError("type_error", "expected string", field)
        for field in ("record_id", "dataset_id", "source_file", "parser_version"):
            if not value[field]:
                raise ValidationError("invalid_value", "must not be empty", field)

        offset = value["source_offset"]
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValidationError("type_error", "expected integer", "source_offset")
        if not 0 <= offset <= MAX_SOURCE_OFFSET:
            raise ValidationError("invalid_value", "must be a non-negative unsigned 64-bit integer", "source_offset")

        if value["record_type"] not in RECORD_TYPES:
            raise ValidationError("invalid_value", "expected article, comment, or reply", "record_type")

        for field in ("event_date", "title", "clean_text"):
            if value[field] is not None and not isinstance(value[field], str):
                raise ValidationError("type_error", "expected string or null", field)
        if value["event_date"] is not None and not DATE_PATTERN.fullmatch(value["event_date"]):
            raise ValidationError("invalid_value", "expected YYYY-MM-DD or null", "event_date")
        if not isinstance(value["metadata"], dict):
            raise ValidationError("type_error", "expected object", "metadata")
        if value["schema_version"] != "record-v1":
            raise ValidationError("invalid_value", "expected record-v1", "schema_version")

        return cls(**value)


@dataclass(frozen=True)
class Profile:
    total_records: int
    by_dataset_id: dict[str, int]
    by_record_type: dict[str, int]
    by_event_date: dict[str, int]
    missing_title_count: int
    missing_or_empty_body_count: int
    unique_record_id_count: int
    duplicate_record_id_count: int
    schema_versions: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_jsonl(path: str | Path) -> list[RecordV1]:
    records: list[RecordV1] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise LineValidationError(
                    line_number,
                    ValidationError("invalid_json", f"{error.msg} at column {error.colno}"),
                ) from None
            try:
                records.append(RecordV1.from_dict(value))
            except ValidationError as error:
                raise LineValidationError(line_number, error) from None
    return records


def profile_records(records: list[RecordV1]) -> Profile:
    ids = Counter(record.record_id for record in records)
    return Profile(
        total_records=len(records),
        by_dataset_id=dict(sorted(Counter(record.dataset_id for record in records).items())),
        by_record_type=dict(sorted(Counter(record.record_type for record in records).items())),
        by_event_date=dict(sorted(Counter(record.event_date or "<null>" for record in records).items())),
        missing_title_count=sum(record.title is None or not record.title.strip() for record in records),
        missing_or_empty_body_count=sum(not record.raw_text.strip() for record in records),
        unique_record_id_count=len(ids),
        duplicate_record_id_count=sum(count - 1 for count in ids.values()),
        schema_versions=dict(sorted(Counter(record.schema_version for record in records).items())),
    )


def profile_jsonl(path: str | Path) -> Profile:
    return profile_records(validate_jsonl(path))

