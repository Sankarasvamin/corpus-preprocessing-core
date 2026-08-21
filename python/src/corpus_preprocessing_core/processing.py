from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .core import RecordV1, ValidationError, validate_jsonl


PARSER_VERSION = "parser-v1"
PIPELINE_VERSION = "pipeline-v1"
FORMATS = {".tsv": "tsv", ".json": "json", ".jsonl": "jsonl", ".html": "html"}
IGNORED_DIRS = {"target", "__pycache__", ".pytest_cache", ".git"}
RAW_FIELDS = (
    "record_id",
    "dataset_id",
    "event_date",
    "record_type",
    "title",
    "raw_text",
    "category_signal",
    "source_batch",
    "synthetic",
)
TSV_FIELDS = (
    "record_id",
    "dataset_id",
    "event_date_json",
    "record_type",
    "title_json",
    "raw_text_json",
    "category_signal_json",
    "source_batch",
    "synthetic",
)
NULL_SENTINEL = "__NULL__"
MISSING_SENTINEL = "__MISSING__"
ZERO_WIDTH = "\u200b\u200c\u200d\ufeff\u2060"
ENTITY_MAP = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
    "nbsp": " ",
    "copy": "©",
    "ndash": "–",
    "mdash": "—",
}
BLOCK_TAGS = {"p", "div", "article", "section", "li", "blockquote", "pre", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "br"}


class ProcessingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def semantic_fingerprint(values: Iterable[Any]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def write_jsonl(path: str | Path, values: Iterable[Any]) -> None:
    Path(path).write_text("".join(canonical_json(value) + "\n" for value in values), encoding="utf-8")


def write_json(path: str | Path, value: Any) -> None:
    Path(path).write_text(canonical_json(value) + "\n", encoding="utf-8")


def read_record_dicts(path: str | Path) -> list[dict[str, Any]]:
    return [asdict(record) for record in validate_jsonl(path)]


@dataclass(frozen=True)
class FileManifestEntryV1:
    relative_path: str
    byte_size: int
    sha256: str
    extension: str
    detected_format: str
    encoding_status: str
    file_status: str
    schema_version: str = "file-manifest-entry-v1"


@dataclass(frozen=True)
class QuarantineRecordV1:
    source_file: str
    source_offset: int
    detected_format: str
    error_code: str
    message: str
    raw_fragment_hash: str
    parser_version: str = PARSER_VERSION
    schema_version: str = "quarantine-record-v1"


@dataclass(frozen=True)
class CleaningEventV1:
    record_id: str
    stage: str
    rule_id: str
    rule_version: str
    match_method: str
    action: str
    reason_code: str
    decision: str
    matched_span: str | None
    removed_chars: int
    score: float
    before_hash: str
    after_hash: str
    metrics: dict[str, Any]
    algorithm_version: str
    schema_version: str = "cleaning-event-v1"


@dataclass(frozen=True)
class SamplePlanV1:
    plan_id: str
    seed: int
    target_size: int
    strata_keys: list[str]
    allocation_method: str
    minimum_per_stratum: int
    filters: dict[str, Any]
    algorithm_version: str
    schema_version: str

    @classmethod
    def from_dict(cls, value: Any) -> "SamplePlanV1":
        required = tuple(cls.__dataclass_fields__)
        if not isinstance(value, dict):
            raise ProcessingError("invalid_plan", "sample plan must be an object")
        missing = next((field for field in required if field not in value), None)
        if missing:
            raise ProcessingError("invalid_plan", f"missing field: {missing}")
        if set(value) != set(required):
            raise ProcessingError("invalid_plan", "sample plan has unknown fields")
        if value["schema_version"] != "sample-plan-v1":
            raise ProcessingError("invalid_plan", "schema_version must be sample-plan-v1")
        if value["allocation_method"] not in {"simple_random", "proportional", "equal", "minimum_then_proportional"}:
            raise ProcessingError("invalid_plan", "unknown allocation_method")
        if isinstance(value["seed"], bool) or not isinstance(value["seed"], int) or value["seed"] < 0:
            raise ProcessingError("invalid_plan", "seed must be a non-negative integer")
        if not isinstance(value["target_size"], int) or value["target_size"] < 1:
            raise ProcessingError("invalid_plan", "target_size must be positive")
        if not isinstance(value["minimum_per_stratum"], int) or value["minimum_per_stratum"] < 0:
            raise ProcessingError("invalid_plan", "minimum_per_stratum must be non-negative")
        if not isinstance(value["strata_keys"], list) or len(value["strata_keys"]) != len(set(value["strata_keys"])):
            raise ProcessingError("invalid_plan", "strata_keys must be a unique list")
        if not isinstance(value["filters"], dict):
            raise ProcessingError("invalid_plan", "filters must be an object")
        return cls(**value)


def _encoding_status(data: bytes) -> str:
    if not data:
        return "empty"
    payload = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return "invalid_utf8"
    return "utf8_bom" if data.startswith(b"\xef\xbb\xbf") else "utf8"


def scan_directory(input_dir: str | Path, output_path: str | Path | None = None) -> list[FileManifestEntryV1]:
    root = Path(input_dir).resolve()
    output = Path(output_path).resolve() if output_path else None
    entries = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if not path.is_file() or any(part in IGNORED_DIRS for part in relative.parts) or (output and path.resolve() == output):
            continue
        extension = path.suffix.lower()
        detected_format = FORMATS.get(extension, "unknown")
        try:
            data = path.read_bytes()
            encoding_status = _encoding_status(data)
            if encoding_status == "empty":
                file_status = "empty"
            elif detected_format == "unknown":
                file_status = "unsupported"
            elif encoding_status == "invalid_utf8":
                file_status = "encoding_error"
            else:
                file_status = "ok"
            entry = FileManifestEntryV1(
                relative.as_posix(), path.stat().st_size, sha256_file(path), extension,
                detected_format, encoding_status, file_status,
            )
        except OSError:
            entry = FileManifestEntryV1(
                relative.as_posix(), 0, sha256_bytes(b""), extension,
                detected_format, "empty", "io_error",
            )
        entries.append(entry)
    return entries


def _quarantine(source_file: str, offset: int, fmt: str, code: str, message: str, fragment: bytes) -> QuarantineRecordV1:
    return QuarantineRecordV1(source_file, offset, fmt, code, message, sha256_bytes(fragment))


def _raw_to_record(raw: Any, source_file: str, offset: int, fmt: str) -> RecordV1:
    if not isinstance(raw, dict):
        raise ProcessingError("type_error", "raw record must be an object")
    unknown = sorted(set(raw) - set(RAW_FIELDS))
    if unknown:
        raise ProcessingError("unknown_schema_drift", "unknown raw fields or schema drift")
    missing = next((field for field in RAW_FIELDS if field not in raw), None)
    if missing:
        raise ProcessingError("missing_field", f"required raw field is missing: {missing}")
    if not isinstance(raw["synthetic"], bool):
        raise ProcessingError("type_error", "synthetic must be boolean")
    value = {
        "record_id": raw["record_id"],
        "dataset_id": raw["dataset_id"],
        "source_file": source_file,
        "source_offset": offset,
        "record_type": raw["record_type"],
        "event_date": raw["event_date"],
        "title": raw["title"],
        "raw_text": raw["raw_text"],
        "clean_text": None,
        "metadata": {
            "category_signal": raw["category_signal"],
            "source_batch": raw["source_batch"],
            "source_format": fmt,
            "synthetic": raw["synthetic"],
        },
        "parser_version": PARSER_VERSION,
        "schema_version": "record-v1",
    }
    try:
        return RecordV1.from_dict(value)
    except ValidationError as error:
        raise ProcessingError(error.error_type, f"invalid RecordV1 field: {error.field or 'record'}") from None


class _ArticleAttributes(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.attributes: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "article":
            self.attributes = dict(attrs)


def _html_raw(line: str) -> dict[str, Any] | None:
    if "<article" not in line.lower():
        return None
    parser = _ArticleAttributes()
    parser.feed(line)
    attrs = parser.attributes
    if attrs is None:
        raise ProcessingError("invalid_html", "invalid HTML record")
    try:
        return {
            "record_id": attrs["data-record-id"],
            "dataset_id": attrs["data-dataset-id"],
            "event_date": json.loads(attrs["data-event-date-json"] or ""),
            "record_type": attrs["data-record-type"],
            "title": json.loads(attrs["data-title-json"] or ""),
            "raw_text": json.loads(attrs["data-raw-text-json"] or ""),
            "category_signal": json.loads(attrs["data-category-signal-json"] or ""),
            "source_batch": attrs["data-source-batch"],
            "synthetic": attrs["data-synthetic"] == "true",
        }
    except (KeyError, json.JSONDecodeError, TypeError):
        raise ProcessingError("invalid_html", "invalid HTML record") from None


def _parse_jsonl(path: Path, relative: str) -> tuple[list[RecordV1], list[QuarantineRecordV1]]:
    records, quarantine = [], []
    with path.open("rb") as handle:
        for offset, raw_line in enumerate(handle):
            fragment = raw_line.rstrip(b"\r\n")
            if offset == 0 and fragment.startswith(b"\xef\xbb\xbf"):
                fragment = fragment[3:]
            try:
                line = fragment.decode("utf-8")
            except UnicodeDecodeError:
                quarantine.append(_quarantine(relative, offset, "jsonl", "invalid_encoding", "record is not valid UTF-8", fragment))
                continue
            try:
                raw = json.loads(line)
                records.append(_raw_to_record(raw, relative, offset, "jsonl"))
            except json.JSONDecodeError:
                quarantine.append(_quarantine(relative, offset, "jsonl", "invalid_json", "invalid JSON record", fragment))
            except ProcessingError as error:
                quarantine.append(_quarantine(relative, offset, "jsonl", error.code, str(error), fragment))
    return records, quarantine


def _parse_tsv(path: Path, relative: str) -> tuple[list[RecordV1], list[QuarantineRecordV1]]:
    records, quarantine = [], []
    with path.open("rb") as handle:
        header_bytes = handle.readline().rstrip(b"\r\n")
        if header_bytes.startswith(b"\xef\xbb\xbf"):
            header_bytes = header_bytes[3:]
        try:
            header = tuple(header_bytes.decode("utf-8").split("\t"))
        except UnicodeDecodeError:
            return [], [_quarantine(relative, 0, "tsv", "invalid_encoding", "file is not valid UTF-8", header_bytes)]
        if header != TSV_FIELDS:
            return [], [_quarantine(relative, 0, "tsv", "unknown_schema_drift", "unknown TSV schema", header_bytes)]
        for offset, raw_line in enumerate(handle):
            fragment = raw_line.rstrip(b"\r\n")
            try:
                values = fragment.decode("utf-8").split("\t")
            except UnicodeDecodeError:
                quarantine.append(_quarantine(relative, offset, "tsv", "invalid_encoding", "record is not valid UTF-8", fragment))
                continue
            if len(values) != len(TSV_FIELDS):
                quarantine.append(_quarantine(relative, offset, "tsv", "malformed_tsv", "TSV column count does not match header", fragment))
                continue
            try:
                raw = {
                    "record_id": values[0], "dataset_id": values[1], "event_date": json.loads(values[2]),
                    "record_type": values[3], "title": json.loads(values[4]), "raw_text": json.loads(values[5]),
                    "category_signal": json.loads(values[6]), "source_batch": values[7],
                    "synthetic": values[8] == "true" if values[8] in {"true", "false"} else None,
                }
                records.append(_raw_to_record(raw, relative, offset, "tsv"))
            except json.JSONDecodeError:
                quarantine.append(_quarantine(relative, offset, "tsv", "malformed_tsv", "invalid JSON-encoded TSV field", fragment))
            except ProcessingError as error:
                quarantine.append(_quarantine(relative, offset, "tsv", error.code, str(error), fragment))
    return records, quarantine


def _parse_html(path: Path, relative: str) -> tuple[list[RecordV1], list[QuarantineRecordV1]]:
    records, quarantine = [], []
    with path.open("rb") as handle:
        data_offset = 0
        for raw_line in handle:
            fragment = raw_line.rstrip(b"\r\n")
            try:
                line = fragment.decode("utf-8-sig" if data_offset == 0 else "utf-8")
            except UnicodeDecodeError:
                quarantine.append(_quarantine(relative, data_offset, "html", "invalid_encoding", "record is not valid UTF-8", fragment))
                continue
            if "<article" not in line.lower():
                continue
            try:
                raw = _html_raw(line)
                if raw is not None:
                    records.append(_raw_to_record(raw, relative, data_offset, "html"))
            except ProcessingError as error:
                quarantine.append(_quarantine(relative, data_offset, "html", error.code, str(error), fragment))
            data_offset += 1
    return records, quarantine


def _parse_json_array(path: Path, relative: str) -> tuple[list[RecordV1], list[QuarantineRecordV1]]:
    data = path.read_bytes()
    payload = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    try:
        value = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError:
        return [], [_quarantine(relative, 0, "json", "invalid_encoding", "file is not valid UTF-8", payload)]
    except json.JSONDecodeError:
        return [], [_quarantine(relative, 0, "json", "invalid_json", "invalid JSON array", payload)]
    if not isinstance(value, list):
        return [], [_quarantine(relative, 0, "json", "type_error", "JSON input must be an array", payload)]
    records, quarantine = [], []
    for offset, raw in enumerate(value):
        fragment = canonical_json(raw).encode("utf-8")
        try:
            records.append(_raw_to_record(raw, relative, offset, "json"))
        except ProcessingError as error:
            quarantine.append(_quarantine(relative, offset, "json", error.code, str(error), fragment))
    return records, quarantine


def parse_directory(input_dir: str | Path, config: dict[str, Any]) -> tuple[list[RecordV1], list[QuarantineRecordV1]]:
    root = Path(input_dir).resolve()
    if config.get("parser_version") != PARSER_VERSION:
        raise ProcessingError("invalid_parsing_config", f"parser_version must be {PARSER_VERSION}")
    records, quarantine = [], []
    for entry in scan_directory(root):
        path = root / entry.relative_path
        if entry.file_status == "empty":
            continue
        if entry.file_status == "unsupported":
            quarantine.append(_quarantine(entry.relative_path, 0, "unknown", "unsupported_format", "unsupported file format", path.read_bytes()))
            continue
        if entry.file_status == "encoding_error":
            quarantine.append(_quarantine(entry.relative_path, 0, entry.detected_format, "invalid_encoding", "file is not valid UTF-8", path.read_bytes()))
            continue
        parser = {"jsonl": _parse_jsonl, "tsv": _parse_tsv, "html": _parse_html, "json": _parse_json_array}[entry.detected_format]
        parsed, isolated = parser(path, entry.relative_path)
        records.extend(parsed)
        quarantine.extend(isolated)
    records.sort(key=lambda record: (record.source_file, record.source_offset))
    quarantine.sort(key=lambda item: (item.source_file, item.source_offset, item.error_code))
    return records, quarantine


def field_value(record: dict[str, Any], path: str) -> Any:
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING_SENTINEL
        value = value[part]
    return NULL_SENTINEL if value is None else value


def _matches_filters(record: dict[str, Any], filters: dict[str, Any]) -> bool:
    for path, expected in filters.items():
        actual = field_value(record, path)
        normalized = NULL_SENTINEL if expected is None else expected
        if isinstance(expected, list):
            allowed = [NULL_SENTINEL if item is None else item for item in expected]
            if actual not in allowed:
                return False
        elif actual != normalized:
            return False
    return True


def rank_hex(record_id: str, seed: int, algorithm_version: str) -> str:
    return hashlib.sha256(f"{algorithm_version}\x1f{seed}\x1f{record_id}".encode("utf-8")).hexdigest()


def _allocate_weighted(capacities: dict[str, int], quotas: dict[str, int], remaining: int, equal: bool) -> None:
    while remaining:
        active = [key for key in sorted(capacities) if quotas[key] < capacities[key]]
        if not active:
            raise ProcessingError("allocation_error", "unable to allocate target")
        weights = {key: 1 if equal else capacities[key] - quotas[key] for key in active}
        weight_sum = sum(weights.values())
        round_remaining = remaining
        remainders = {}
        added = 0
        for key in active:
            share = round_remaining * weights[key]
            amount = min(capacities[key] - quotas[key], share // weight_sum)
            quotas[key] += amount
            added += amount
            remainders[key] = share % weight_sum
        remaining -= added
        for key in sorted(active, key=lambda item: (-remainders[item], item)):
            if not remaining:
                break
            if quotas[key] < capacities[key]:
                quotas[key] += 1
                remaining -= 1


def allocate_quotas(capacities: dict[str, int], plan: SamplePlanV1) -> dict[str, int]:
    if plan.target_size > sum(capacities.values()):
        raise ProcessingError("target_exceeds_population", "target_size exceeds filtered population")
    quotas = {key: 0 for key in capacities}
    if plan.allocation_method == "simple_random":
        quotas[next(iter(capacities))] = plan.target_size
        return quotas
    if plan.allocation_method == "minimum_then_proportional":
        for key, capacity in capacities.items():
            quotas[key] = min(capacity, plan.minimum_per_stratum)
        if sum(quotas.values()) > plan.target_size:
            raise ProcessingError("infeasible_minimum", "minimum allocations exceed target_size")
    remaining = plan.target_size - sum(quotas.values())
    _allocate_weighted(capacities, quotas, remaining, plan.allocation_method == "equal")
    return quotas


def sample_records(records: list[RecordV1], plan: SamplePlanV1) -> tuple[list[RecordV1], dict[str, Any]]:
    population = [record for record in records if _matches_filters(asdict(record), plan.filters)]
    if plan.target_size > len(population):
        raise ProcessingError("target_exceeds_population", "target_size exceeds filtered population")
    strata: dict[str, list[RecordV1]] = {}
    for record in population:
        key = "[]" if plan.allocation_method == "simple_random" else canonical_json([field_value(asdict(record), path) for path in plan.strata_keys])
        strata.setdefault(key, []).append(record)
    capacities = {key: len(value) for key, value in sorted(strata.items())}
    quotas = allocate_quotas(capacities, plan)
    selected = []
    for key in sorted(strata):
        ranked = sorted(strata[key], key=lambda record: (rank_hex(record.record_id, plan.seed, plan.algorithm_version), record.record_id))
        selected.extend(ranked[:quotas[key]])
    selected.sort(key=lambda record: (rank_hex(record.record_id, plan.seed, plan.algorithm_version), record.record_id))
    population_values = sorted((asdict(record) for record in population), key=lambda value: value["record_id"])
    selected_values = [asdict(record) for record in selected]
    report = {
        "algorithm_version": plan.algorithm_version,
        "allocation_method": plan.allocation_method,
        "filtered_population_size": len(population),
        "plan_fingerprint": sha256_text(canonical_json(asdict(plan))),
        "plan_id": plan.plan_id,
        "population_fingerprint": semantic_fingerprint(population_values),
        "population_size": len(records),
        "schema_version": "sample-report-v1",
        "seed": plan.seed,
        "selected_id_fingerprint": semantic_fingerprint(sorted(record.record_id for record in selected)),
        "output_fingerprint": semantic_fingerprint(selected_values),
        "strata": [
            {"key": key, "population": capacities[key], "quota": quotas[key], "selected": quotas[key]}
            for key in sorted(strata)
        ],
        "strata_keys": plan.strata_keys,
        "target_size": plan.target_size,
    }
    return selected, report


def risk_sample(records: list[RecordV1], target: int, cleaning_config: dict[str, Any]) -> list[dict[str, Any]]:
    markers = []
    templates = cleaning_config["exact_templates"]
    markers.extend(templates["prefixes"] + templates["suffixes"] + templates["lines"])
    markers.extend(block["start"] for block in templates["blocks"])
    candidates = []
    for record in records:
        reasons = []
        text = record.raw_text
        if not text.strip():
            reasons.append("empty_body")
        if record.record_type == "article" and (record.title is None or not record.title.strip()):
            reasons.append("missing_title")
        if record.event_date is None:
            reasons.append("missing_date")
        if record.metadata.get("parser_warnings"):
            reasons.append("parser_warning")
        if text.strip() and len(text.strip()) < 12:
            reasons.append("short_text")
        normalized_text = unicodedata.normalize("NFKC", text)
        if any(marker and marker in normalized_text for marker in markers):
            reasons.append("known_template")
        if reasons:
            candidates.append({"record_id": record.record_id, "risk_reasons": sorted(set(reasons))})
    candidates.sort(key=lambda item: (-len(item["risk_reasons"]), rank_hex(item["record_id"], 0, "risk-v1"), item["record_id"]))
    return candidates[:target]


def _remove_script_style(text: str) -> str:
    pattern = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>")
    return pattern.sub("", text)


def _decode_entities(text: str) -> str:
    pattern = re.compile(r"&(#(?:x[0-9A-Fa-f]+|[0-9]+)|[A-Za-z]+);")

    def replace_entity(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith("#x"):
            value = int(token[2:], 16)
        elif token.startswith("#"):
            value = int(token[1:])
        else:
            return ENTITY_MAP.get(token, match.group(0))
        return chr(value) if 0 <= value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF else match.group(0)

    return pattern.sub(replace_entity, text)


def _normalize_html(text: str) -> str:
    output = []
    index = 0
    while index < len(text):
        if text.startswith("<!--", index):
            end = text.find("-->", index + 4)
            if end >= 0:
                index = end + 3
                continue
        if text[index] == "<":
            match = re.match(r"</?([A-Za-z][A-Za-z0-9]*)\b[^>]*>", text[index:])
            if match:
                tag = match.group(1).lower()
                if tag in BLOCK_TAGS and output and output[-1] != "\n":
                    output.append("\n")
                index += len(match.group(0))
                continue
        output.append(text[index])
        index += 1
    return _decode_entities("".join(output))


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines, current = [], []
    for char in text:
        if char == "\n":
            lines.append("".join(current).strip())
            current = []
        elif char.isspace():
            if current and current[-1] != " ":
                current.append(" ")
        else:
            current.append(char)
    lines.append("".join(current).strip())
    collapsed = []
    for line in lines:
        if line or not collapsed or collapsed[-1]:
            collapsed.append(line)
    return "\n".join(collapsed).strip()


def _remove_templates(text: str, templates: dict[str, Any]) -> tuple[str, list[str]]:
    matched = []
    for prefix in templates["prefixes"]:
        if text.startswith(prefix):
            text = text[len(prefix):]
            matched.append(prefix)
    for suffix in templates["suffixes"]:
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            matched.append(suffix)
    lines = []
    for line in text.split("\n"):
        if line in templates["lines"]:
            matched.append(line)
        else:
            lines.append(line)
    text = "\n".join(lines)
    for block in templates["blocks"]:
        start = text.find(block["start"])
        if start >= 0:
            end = text.find(block["end"], start + len(block["start"]))
            if end >= 0:
                span = text[start:end + len(block["end"])]
                text = text[:start] + text[end + len(block["end"]):]
                matched.append(span)
    return text.strip(), matched


def validate_cleaning_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    if config.get("schema_version") != "cleaning-config-v1":
        raise ProcessingError("invalid_cleaning_config", "schema_version must be cleaning-config-v1")
    rules = config.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ProcessingError("invalid_cleaning_config", "rules must be a non-empty list")
    priorities = [rule.get("priority") for rule in rules]
    if len(priorities) != len(set(priorities)):
        raise ProcessingError("duplicate_priority", "cleaning rule priorities must be unique")
    if any(rule.get("match_method") == "fuzzy" for rule in rules):
        raise ProcessingError("fuzzy_not_supported", "fuzzy rules are not enabled in deterministic-clean-v1")
    return sorted(rules, key=lambda rule: rule["priority"])


def _event(record_id: str, rule: dict[str, Any], config: dict[str, Any], before: str, after: str, decision: str, matched: str | None) -> CleaningEventV1:
    return CleaningEventV1(
        record_id=record_id,
        stage="deterministic_cleaning",
        rule_id=rule["rule_id"],
        rule_version=config["rule_version"],
        match_method=rule["match_method"],
        action=rule["action"],
        reason_code=rule["reason_code"],
        decision=decision,
        matched_span=matched,
        removed_chars=max(0, len(before) - len(after)),
        score=1.0,
        before_hash=sha256_text(before),
        after_hash=sha256_text(after),
        metrics={"priority": rule["priority"]},
        algorithm_version=config["algorithm_version"],
    )


def clean_record(record: RecordV1, config: dict[str, Any]) -> tuple[RecordV1, list[CleaningEventV1]]:
    rules = validate_cleaning_config(config)
    text = record.raw_text
    events = []
    for rule in rules:
        before = text
        matched: str | None = None
        rule_id = rule["rule_id"]
        if rule_id == "remove_script_style":
            text = _remove_script_style(text)
            matched = "script/style" if text != before else None
        elif rule_id == "normalize_html":
            text = _normalize_html(text)
        elif rule_id == "remove_zero_width":
            found = sorted({f"U+{ord(char):04X}" for char in text if char in ZERO_WIDTH})
            text = "".join(char for char in text if char not in ZERO_WIDTH)
            matched = ",".join(found) or None
        elif rule_id == "normalize_nfkc":
            text = unicodedata.normalize("NFKC", text)
        elif rule_id == "normalize_whitespace":
            text = _normalize_whitespace(text)
        elif rule_id == "remove_exact_templates":
            text, spans = _remove_templates(text, config["exact_templates"])
            matched = ";".join(spans) or None
        elif rule_id == "flag_empty":
            if not text.strip():
                events.append(_event(record.record_id, rule, config, text, text, "review", None))
            continue
        elif rule_id == "flag_pure_symbol":
            if text and not any(char.isalnum() for char in text):
                events.append(_event(record.record_id, rule, config, text, text, "review", None))
            continue
        elif rule_id == "flag_short":
            if text and len(text) < config["short_text_threshold"]:
                events.append(_event(record.record_id, rule, config, text, text, "review", None))
            continue
        else:
            raise ProcessingError("unknown_cleaning_rule", f"unknown rule_id: {rule_id}")
        if text != before:
            events.append(_event(record.record_id, rule, config, before, text, "applied", matched))
    return replace(record, clean_text=text), events


def clean_records(records: list[RecordV1], config: dict[str, Any]) -> tuple[list[RecordV1], list[CleaningEventV1]]:
    cleaned, events = [], []
    for record in records:
        result, record_events = clean_record(record, config)
        cleaned.append(result)
        events.extend(record_events)
    return cleaned, events


def changed_characters(before: str, after: str) -> int:
    common = min(len(before), len(after))
    return sum(before[index] != after[index] for index in range(common)) + abs(len(before) - len(after))


def paired_sample(before: list[RecordV1], after: list[RecordV1], events: list[CleaningEventV1]) -> list[dict[str, Any]]:
    after_by_id = {record.record_id: record for record in after}
    rules: dict[str, list[str]] = {}
    for event in events:
        rules.setdefault(event.record_id, []).append(event.rule_id)
    pairs = []
    for record in before:
        cleaned = after_by_id[record.record_id]
        pairs.append({
            "after": cleaned.clean_text,
            "before": record.raw_text,
            "changed": record.raw_text != cleaned.clean_text,
            "changed_characters": changed_characters(record.raw_text, cleaned.clean_text or ""),
            "record_id": record.record_id,
            "rule_ids": rules.get(record.record_id, []),
        })
    return pairs


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def clean_to_files(input_path: str | Path, config_path: str | Path, output_path: str | Path, events_path: str | Path, cache_manifest_path: str | Path) -> tuple[list[RecordV1], list[CleaningEventV1], bool]:
    config = load_json(config_path)
    validate_cleaning_config(config)
    input_fingerprint = sha256_file(input_path)
    config_fingerprint = sha256_text(canonical_json(config))
    cache_key = sha256_text("\x1f".join((config["algorithm_version"], input_fingerprint, config_fingerprint)))
    output_path, events_path, cache_manifest_path = Path(output_path), Path(events_path), Path(cache_manifest_path)
    if cache_manifest_path.exists() and output_path.exists() and events_path.exists():
        cached = load_json(cache_manifest_path)
        if (
            cached.get("cache_key") == cache_key
            and cached.get("input_fingerprint") == input_fingerprint
            and cached.get("config_fingerprint") == config_fingerprint
            and cached.get("cleaned_output_hash") == sha256_file(output_path)
            and cached.get("events_output_hash") == sha256_file(events_path)
        ):
            cleaned = [RecordV1.from_dict(value) for value in map(json.loads, output_path.read_text(encoding="utf-8").splitlines())]
            events = [CleaningEventV1(**value) for value in map(json.loads, events_path.read_text(encoding="utf-8").splitlines())]
            return cleaned, events, True
    records = validate_jsonl(input_path)
    cleaned, events = clean_records(records, config)
    write_jsonl(output_path, (asdict(record) for record in cleaned))
    write_jsonl(events_path, (asdict(event) for event in events))
    write_json(cache_manifest_path, {
        "algorithm_version": config["algorithm_version"],
        "cache_key": cache_key,
        "cleaned_output_hash": sha256_file(output_path),
        "config_fingerprint": config_fingerprint,
        "events_output_hash": sha256_file(events_path),
        "input_fingerprint": input_fingerprint,
        "schema_version": "clean-cache-v1",
    })
    return cleaned, events, False


def pipeline(input_dir: str | Path, parsing_config_path: str | Path, cleaning_config_path: str | Path, sample_plan_path: str | Path, output_dir: str | Path, fuzzy_config_path: str | Path | None = None) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    parsing_config = load_json(parsing_config_path)
    cleaning_config = load_json(cleaning_config_path)
    plan = SamplePlanV1.from_dict(load_json(sample_plan_path))
    manifest = scan_directory(input_dir, output / "file-manifest.jsonl")
    write_jsonl(output / "file-manifest.jsonl", (asdict(entry) for entry in manifest))
    records, quarantine = parse_directory(input_dir, parsing_config)
    write_jsonl(output / "parsed-records.jsonl", (asdict(record) for record in records))
    write_jsonl(output / "quarantine.jsonl", (asdict(item) for item in quarantine))
    sampled, sample_report = sample_records(records, plan)
    write_jsonl(output / "sampled-before.jsonl", (asdict(record) for record in sampled))
    write_json(output / "sample-report.json", sample_report)
    cleaned, events, _ = clean_to_files(
        output / "parsed-records.jsonl", cleaning_config_path,
        output / "cleaned-records.jsonl", output / "cleaning-events.jsonl", output / ".clean-cache.json",
    )
    pairs = paired_sample(sampled, cleaned, events)
    write_jsonl(output / "sampled-pairs.jsonl", pairs)
    risk = risk_sample(records, min(20, len(records)), cleaning_config)
    write_jsonl(output / "risk-sample.jsonl", risk)
    output_names = [
        "file-manifest.jsonl", "parsed-records.jsonl", "quarantine.jsonl", "sampled-before.jsonl",
        "cleaned-records.jsonl", "cleaning-events.jsonl", "sampled-pairs.jsonl", "sample-report.json",
    ]
    fuzzy_config = None
    if fuzzy_config_path is not None:
        from .fuzzy import fuzzy_clean_records

        fuzzy_config = load_json(fuzzy_config_path)
        fuzzy_cleaned, fuzzy_events, fuzzy_decisions = fuzzy_clean_records(cleaned, fuzzy_config)
        fuzzy_review = [decision for decision in fuzzy_decisions if decision["decision"] == "review"]
        fuzzy_report = {
            "algorithm_version": fuzzy_config["algorithm_version"],
            "counts": dict(sorted(Counter(item["decision"] for item in fuzzy_decisions).items())),
            "decision_fingerprint": semantic_fingerprint(fuzzy_decisions),
            "record_count": len(cleaned),
            "schema_version": "fuzzy-decision-report-v1",
            "thresholds": fuzzy_config["thresholds"],
        }
        write_jsonl(output / "fuzzy-cleaned-records.jsonl", (asdict(record) for record in fuzzy_cleaned))
        write_jsonl(output / "fuzzy-cleaning-events.jsonl", (asdict(event) for event in fuzzy_events))
        write_jsonl(output / "fuzzy-decisions.jsonl", fuzzy_decisions)
        write_jsonl(output / "fuzzy-review-queue.jsonl", fuzzy_review)
        write_json(output / "fuzzy-decision-report.json", fuzzy_report)
        output_names.extend((
            "fuzzy-cleaned-records.jsonl", "fuzzy-cleaning-events.jsonl", "fuzzy-decisions.jsonl",
            "fuzzy-review-queue.jsonl", "fuzzy-decision-report.json",
        ))
    outputs = {
        name: sha256_file(output / name)
        for name in output_names
    }
    run_manifest = {
        "algorithm_version": PIPELINE_VERSION,
        "cleaning_config_fingerprint": sha256_text(canonical_json(cleaning_config)),
        "input_fingerprint": semantic_fingerprint(asdict(entry) for entry in manifest),
        "outputs": outputs,
        "parsing_config_fingerprint": sha256_text(canonical_json(parsing_config)),
        "plan_fingerprint": sha256_text(canonical_json(asdict(plan))),
        "schema_version": "run-manifest-v1",
    }
    if fuzzy_config is not None:
        run_manifest["algorithm_version"] = "pipeline-v1+fuzzy-template-clean-v1"
        run_manifest["fuzzy_config_fingerprint"] = sha256_text(canonical_json(fuzzy_config))
    write_json(output / "run-manifest.json", run_manifest)
    return run_manifest
