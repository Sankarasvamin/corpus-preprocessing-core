use crate::{parse_record, validate_jsonl, RecordV1};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use unicode_normalization::UnicodeNormalization;

pub const PARSER_VERSION: &str = "parser-v1";
pub const PIPELINE_VERSION: &str = "pipeline-v1";
const NULL_SENTINEL: &str = "__NULL__";
const MISSING_SENTINEL: &str = "__MISSING__";
const RAW_FIELDS: [&str; 9] = [
    "record_id", "dataset_id", "event_date", "record_type", "title", "raw_text",
    "category_signal", "source_batch", "synthetic",
];
const TSV_FIELDS: [&str; 9] = [
    "record_id", "dataset_id", "event_date_json", "record_type", "title_json",
    "raw_text_json", "category_signal_json", "source_batch", "synthetic",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessingError {
    pub code: String,
    pub message: String,
}

impl ProcessingError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self { code: code.into(), message: message.into() }
    }
}

impl fmt::Display for ProcessingError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "[{}] {}", self.code, self.message)
    }
}

impl std::error::Error for ProcessingError {}

fn io_error(error: impl fmt::Display) -> ProcessingError {
    ProcessingError::new("io_error", error.to_string())
}

pub fn canonical_json(value: &Value) -> String {
    serde_json::to_string(value).expect("JSON value serialization must succeed")
}

pub fn sha256_bytes(value: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(value))
}

pub fn sha256_text(value: &str) -> String {
    sha256_bytes(value.as_bytes())
}

pub fn sha256_file(path: &Path) -> Result<String, ProcessingError> {
    let mut file = File::open(path).map_err(io_error)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer).map_err(io_error)?;
        if read == 0 { break; }
        digest.update(&buffer[..read]);
    }
    Ok(format!("sha256:{:x}", digest.finalize()))
}

pub fn semantic_fingerprint(values: &[Value]) -> String {
    let mut digest = Sha256::new();
    for value in values {
        digest.update(canonical_json(value).as_bytes());
        digest.update(b"\n");
    }
    format!("sha256:{:x}", digest.finalize())
}

pub fn write_jsonl(path: &Path, values: &[Value]) -> Result<(), ProcessingError> {
    let mut file = File::create(path).map_err(io_error)?;
    for value in values {
        file.write_all(canonical_json(value).as_bytes()).map_err(io_error)?;
        file.write_all(b"\n").map_err(io_error)?;
    }
    Ok(())
}

pub fn write_json(path: &Path, value: &Value) -> Result<(), ProcessingError> {
    fs::write(path, canonical_json(value) + "\n").map_err(io_error)
}

pub fn load_json(path: &Path) -> Result<Value, ProcessingError> {
    let data = fs::read_to_string(path).map_err(io_error)?;
    serde_json::from_str(&data).map_err(|error| ProcessingError::new("invalid_json", error.to_string()))
}

fn values<T: Serialize>(items: &[T]) -> Vec<Value> {
    items.iter().map(|item| serde_json::to_value(item).expect("serialization must succeed")).collect()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FileManifestEntryV1 {
    pub relative_path: String,
    pub byte_size: u64,
    pub sha256: String,
    pub extension: String,
    pub detected_format: String,
    pub encoding_status: String,
    pub file_status: String,
    pub schema_version: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct QuarantineRecordV1 {
    pub source_file: String,
    pub source_offset: u64,
    pub detected_format: String,
    pub error_code: String,
    pub message: String,
    pub raw_fragment_hash: String,
    pub parser_version: String,
    pub schema_version: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CleaningEventV1 {
    pub record_id: String,
    pub stage: String,
    pub rule_id: String,
    pub rule_version: String,
    pub match_method: String,
    pub action: String,
    pub reason_code: String,
    pub decision: String,
    pub matched_span: Option<String>,
    pub removed_chars: usize,
    pub score: f64,
    pub before_hash: String,
    pub after_hash: String,
    pub metrics: Value,
    pub algorithm_version: String,
    pub schema_version: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SamplePlanV1 {
    pub plan_id: String,
    pub seed: u64,
    pub target_size: usize,
    pub strata_keys: Vec<String>,
    pub allocation_method: String,
    pub minimum_per_stratum: usize,
    pub filters: Map<String, Value>,
    pub algorithm_version: String,
    pub schema_version: String,
}

impl SamplePlanV1 {
    pub fn from_value(value: Value) -> Result<Self, ProcessingError> {
        let plan: Self = serde_json::from_value(value)
            .map_err(|error| ProcessingError::new("invalid_plan", error.to_string()))?;
        if plan.schema_version != "sample-plan-v1" {
            return Err(ProcessingError::new("invalid_plan", "schema_version must be sample-plan-v1"));
        }
        if !matches!(plan.allocation_method.as_str(), "simple_random" | "proportional" | "equal" | "minimum_then_proportional") {
            return Err(ProcessingError::new("invalid_plan", "unknown allocation_method"));
        }
        if plan.target_size == 0 {
            return Err(ProcessingError::new("invalid_plan", "target_size must be positive"));
        }
        let unique: BTreeSet<_> = plan.strata_keys.iter().collect();
        if unique.len() != plan.strata_keys.len() {
            return Err(ProcessingError::new("invalid_plan", "strata_keys must be unique"));
        }
        Ok(plan)
    }
}

fn format_for(extension: &str) -> &'static str {
    match extension {
        ".tsv" => "tsv",
        ".json" => "json",
        ".jsonl" => "jsonl",
        ".html" => "html",
        _ => "unknown",
    }
}

fn ignored(path: &Path, root: &Path) -> bool {
    path.strip_prefix(root).ok().is_some_and(|relative| {
        relative.components().any(|part| matches!(part.as_os_str().to_str(), Some("target" | "__pycache__" | ".pytest_cache" | ".git")))
    })
}

fn collect_files(root: &Path, directory: &Path, output: Option<&Path>, files: &mut Vec<PathBuf>) -> Result<(), ProcessingError> {
    let mut entries: Vec<_> = fs::read_dir(directory).map_err(io_error)?.collect::<Result<_, _>>().map_err(io_error)?;
    entries.sort_by_key(|entry| entry.file_name());
    for entry in entries {
        let path = entry.path();
        if ignored(&path, root) || output.is_some_and(|candidate| path == candidate) { continue; }
        let kind = entry.file_type().map_err(io_error)?;
        if kind.is_dir() {
            collect_files(root, &path, output, files)?;
        } else if kind.is_file() {
            files.push(path);
        }
    }
    Ok(())
}

fn relative_posix(path: &Path, root: &Path) -> String {
    path.strip_prefix(root).expect("path must be under root").components()
        .map(|part| part.as_os_str().to_string_lossy()).collect::<Vec<_>>().join("/")
}

pub fn scan_directory(input_dir: &Path, output_path: Option<&Path>) -> Result<Vec<FileManifestEntryV1>, ProcessingError> {
    let root = input_dir.canonicalize().map_err(io_error)?;
    let output = output_path.and_then(|path| path.canonicalize().ok());
    let mut files = Vec::new();
    collect_files(&root, &root, output.as_deref(), &mut files)?;
    files.sort_by_key(|path| relative_posix(path, &root));
    let mut result = Vec::new();
    for path in files {
        let relative_path = relative_posix(&path, &root);
        let extension = path.extension().and_then(|value| value.to_str())
            .map(|value| format!(".{}", value.to_ascii_lowercase())).unwrap_or_default();
        let detected_format = format_for(&extension).to_owned();
        let data = match fs::read(&path) {
            Ok(data) => data,
            Err(_) => {
                result.push(FileManifestEntryV1 {
                    relative_path, byte_size: 0, sha256: sha256_bytes(b""), extension,
                    detected_format, encoding_status: "empty".to_owned(), file_status: "io_error".to_owned(),
                    schema_version: "file-manifest-entry-v1".to_owned(),
                });
                continue;
            }
        };
        let (encoding_status, valid_utf8) = if data.is_empty() {
            ("empty", true)
        } else if data.starts_with(&[0xef, 0xbb, 0xbf]) {
            ("utf8_bom", std::str::from_utf8(&data[3..]).is_ok())
        } else {
            ("utf8", std::str::from_utf8(&data).is_ok())
        };
        let encoding_status = if valid_utf8 { encoding_status } else { "invalid_utf8" };
        let file_status = if data.is_empty() { "empty" }
            else if detected_format == "unknown" { "unsupported" }
            else if !valid_utf8 { "encoding_error" }
            else { "ok" };
        result.push(FileManifestEntryV1 {
            relative_path, byte_size: data.len() as u64, sha256: sha256_file(&path)?, extension,
            detected_format, encoding_status: encoding_status.to_owned(), file_status: file_status.to_owned(),
            schema_version: "file-manifest-entry-v1".to_owned(),
        });
    }
    Ok(result)
}

fn quarantine(source_file: &str, offset: u64, format: &str, code: &str, message: &str, fragment: &[u8]) -> QuarantineRecordV1 {
    QuarantineRecordV1 {
        source_file: source_file.to_owned(), source_offset: offset, detected_format: format.to_owned(),
        error_code: code.to_owned(), message: message.to_owned(), raw_fragment_hash: sha256_bytes(fragment),
        parser_version: PARSER_VERSION.to_owned(), schema_version: "quarantine-record-v1".to_owned(),
    }
}

fn raw_to_record(raw: Value, source_file: &str, offset: u64, format: &str) -> Result<RecordV1, ProcessingError> {
    let object = raw.as_object().ok_or_else(|| ProcessingError::new("type_error", "raw record must be an object"))?;
    if object.keys().any(|field| !RAW_FIELDS.contains(&field.as_str())) {
        return Err(ProcessingError::new("unknown_schema_drift", "unknown raw fields or schema drift"));
    }
    for field in RAW_FIELDS {
        if !object.contains_key(field) {
            return Err(ProcessingError::new("missing_field", format!("required raw field is missing: {field}")));
        }
    }
    if !object["synthetic"].is_boolean() {
        return Err(ProcessingError::new("type_error", "synthetic must be boolean"));
    }
    let record = json!({
        "record_id": object["record_id"],
        "dataset_id": object["dataset_id"],
        "source_file": source_file,
        "source_offset": offset,
        "record_type": object["record_type"],
        "event_date": object["event_date"],
        "title": object["title"],
        "raw_text": object["raw_text"],
        "clean_text": null,
        "metadata": {
            "category_signal": object["category_signal"],
            "source_batch": object["source_batch"],
            "source_format": format,
            "synthetic": object["synthetic"]
        },
        "parser_version": PARSER_VERSION,
        "schema_version": "record-v1"
    });
    parse_record(&canonical_json(&record)).map_err(|error| {
        ProcessingError::new(error.error_type, format!("invalid RecordV1 field: {}", error.field.as_deref().unwrap_or("record")))
    })
}

fn trim_line_end(mut line: Vec<u8>) -> Vec<u8> {
    while matches!(line.last(), Some(b'\n' | b'\r')) { line.pop(); }
    line
}

fn parse_jsonl(path: &Path, relative: &str) -> Result<(Vec<RecordV1>, Vec<QuarantineRecordV1>), ProcessingError> {
    let mut reader = BufReader::new(File::open(path).map_err(io_error)?);
    let mut records = Vec::new();
    let mut isolated = Vec::new();
    let mut offset = 0_u64;
    loop {
        let mut line = Vec::new();
        if reader.read_until(b'\n', &mut line).map_err(io_error)? == 0 { break; }
        let mut fragment = trim_line_end(line);
        if offset == 0 && fragment.starts_with(&[0xef, 0xbb, 0xbf]) { fragment.drain(..3); }
        match std::str::from_utf8(&fragment) {
            Err(_) => isolated.push(quarantine(relative, offset, "jsonl", "invalid_encoding", "record is not valid UTF-8", &fragment)),
            Ok(text) => match serde_json::from_str::<Value>(text) {
                Err(_) => isolated.push(quarantine(relative, offset, "jsonl", "invalid_json", "invalid JSON record", &fragment)),
                Ok(raw) => match raw_to_record(raw, relative, offset, "jsonl") {
                    Ok(record) => records.push(record),
                    Err(error) => isolated.push(quarantine(relative, offset, "jsonl", &error.code, &error.message, &fragment)),
                },
            },
        }
        offset += 1;
    }
    Ok((records, isolated))
}

fn parse_tsv(path: &Path, relative: &str) -> Result<(Vec<RecordV1>, Vec<QuarantineRecordV1>), ProcessingError> {
    let mut reader = BufReader::new(File::open(path).map_err(io_error)?);
    let mut header = Vec::new();
    reader.read_until(b'\n', &mut header).map_err(io_error)?;
    let mut header = trim_line_end(header);
    if header.starts_with(&[0xef, 0xbb, 0xbf]) { header.drain(..3); }
    let expected = TSV_FIELDS.join("\t");
    let header_text = match std::str::from_utf8(&header) {
        Ok(value) => value,
        Err(_) => return Ok((vec![], vec![quarantine(relative, 0, "tsv", "invalid_encoding", "file is not valid UTF-8", &header)])),
    };
    if header_text != expected {
        return Ok((vec![], vec![quarantine(relative, 0, "tsv", "unknown_schema_drift", "unknown TSV schema", &header)]));
    }
    let mut records = Vec::new();
    let mut isolated = Vec::new();
    let mut offset = 0_u64;
    loop {
        let mut line = Vec::new();
        if reader.read_until(b'\n', &mut line).map_err(io_error)? == 0 { break; }
        let fragment = trim_line_end(line);
        let text = match std::str::from_utf8(&fragment) {
            Ok(value) => value,
            Err(_) => {
                isolated.push(quarantine(relative, offset, "tsv", "invalid_encoding", "record is not valid UTF-8", &fragment));
                offset += 1;
                continue;
            }
        };
        let fields: Vec<_> = text.split('\t').collect();
        if fields.len() != TSV_FIELDS.len() {
            isolated.push(quarantine(relative, offset, "tsv", "malformed_tsv", "TSV column count does not match header", &fragment));
            offset += 1;
            continue;
        }
        let parsed_fields = (|| -> Result<Value, serde_json::Error> {
            Ok(json!({
                "record_id": fields[0], "dataset_id": fields[1], "event_date": serde_json::from_str::<Value>(fields[2])?,
                "record_type": fields[3], "title": serde_json::from_str::<Value>(fields[4])?,
                "raw_text": serde_json::from_str::<Value>(fields[5])?, "category_signal": serde_json::from_str::<Value>(fields[6])?,
                "source_batch": fields[7], "synthetic": match fields[8] { "true" => Value::Bool(true), "false" => Value::Bool(false), _ => Value::Null }
            }))
        })();
        match parsed_fields {
            Err(_) => isolated.push(quarantine(relative, offset, "tsv", "malformed_tsv", "invalid JSON-encoded TSV field", &fragment)),
            Ok(raw) => match raw_to_record(raw, relative, offset, "tsv") {
                Ok(record) => records.push(record),
                Err(error) => isolated.push(quarantine(relative, offset, "tsv", &error.code, &error.message, &fragment)),
            },
        }
        offset += 1;
    }
    Ok((records, isolated))
}

fn decode_entities(text: &str) -> String {
    let named = BTreeMap::from([
        ("amp", "&"), ("apos", "'"), ("copy", "©"), ("gt", ">"), ("lt", "<"),
        ("mdash", "—"), ("nbsp", " "), ("ndash", "–"), ("quot", "\""),
    ]);
    let mut output = String::new();
    let mut index = 0;
    while index < text.len() {
        if text.as_bytes()[index] == b'&' {
            if let Some(relative_end) = text[index + 1..].find(';') {
                let end = index + 1 + relative_end;
                let token = &text[index + 1..end];
                let decoded = if let Some(hex) = token.strip_prefix("#x") {
                    u32::from_str_radix(hex, 16).ok().and_then(char::from_u32).map(|value| value.to_string())
                } else if let Some(decimal) = token.strip_prefix('#') {
                    decimal.parse::<u32>().ok().and_then(char::from_u32).map(|value| value.to_string())
                } else {
                    named.get(token).map(|value| (*value).to_owned())
                };
                if let Some(value) = decoded {
                    output.push_str(&value);
                    index = end + 1;
                    continue;
                }
            }
        }
        let character = text[index..].chars().next().expect("valid char boundary");
        output.push(character);
        index += character.len_utf8();
    }
    output
}

fn html_attribute(line: &str, name: &str) -> Result<String, ProcessingError> {
    let needle = format!("{name}=\"");
    let start = line.find(&needle).ok_or_else(|| ProcessingError::new("invalid_html", "invalid HTML record"))? + needle.len();
    let end = line[start..].find('"').ok_or_else(|| ProcessingError::new("invalid_html", "invalid HTML record"))? + start;
    Ok(decode_entities(&line[start..end]))
}

fn html_raw(line: &str) -> Result<Value, ProcessingError> {
    let parse_json_attr = |name: &str| -> Result<Value, ProcessingError> {
        serde_json::from_str(&html_attribute(line, name)?)
            .map_err(|_| ProcessingError::new("invalid_html", "invalid HTML record"))
    };
    Ok(json!({
        "record_id": html_attribute(line, "data-record-id")?,
        "dataset_id": html_attribute(line, "data-dataset-id")?,
        "event_date": parse_json_attr("data-event-date-json")?,
        "record_type": html_attribute(line, "data-record-type")?,
        "title": parse_json_attr("data-title-json")?,
        "raw_text": parse_json_attr("data-raw-text-json")?,
        "category_signal": parse_json_attr("data-category-signal-json")?,
        "source_batch": html_attribute(line, "data-source-batch")?,
        "synthetic": html_attribute(line, "data-synthetic")? == "true"
    }))
}

fn parse_html(path: &Path, relative: &str) -> Result<(Vec<RecordV1>, Vec<QuarantineRecordV1>), ProcessingError> {
    let reader = BufReader::new(File::open(path).map_err(io_error)?);
    let mut records = Vec::new();
    let mut isolated = Vec::new();
    let mut offset = 0_u64;
    for line in reader.split(b'\n') {
        let mut fragment = trim_line_end(line.map_err(io_error)?);
        if offset == 0 && fragment.starts_with(&[0xef, 0xbb, 0xbf]) { fragment.drain(..3); }
        let text = match std::str::from_utf8(&fragment) {
            Ok(value) => value,
            Err(_) => {
                isolated.push(quarantine(relative, offset, "html", "invalid_encoding", "record is not valid UTF-8", &fragment));
                continue;
            }
        };
        if !text.to_ascii_lowercase().contains("<article") { continue; }
        match html_raw(text).and_then(|raw| raw_to_record(raw, relative, offset, "html")) {
            Ok(record) => records.push(record),
            Err(error) => isolated.push(quarantine(relative, offset, "html", &error.code, &error.message, &fragment)),
        }
        offset += 1;
    }
    Ok((records, isolated))
}

fn parse_json_array(path: &Path, relative: &str) -> Result<(Vec<RecordV1>, Vec<QuarantineRecordV1>), ProcessingError> {
    let data = fs::read(path).map_err(io_error)?;
    let payload = if data.starts_with(&[0xef, 0xbb, 0xbf]) { &data[3..] } else { &data[..] };
    let text = match std::str::from_utf8(payload) {
        Ok(value) => value,
        Err(_) => return Ok((vec![], vec![quarantine(relative, 0, "json", "invalid_encoding", "file is not valid UTF-8", payload)])),
    };
    let array: Value = match serde_json::from_str(text) {
        Ok(value) => value,
        Err(_) => return Ok((vec![], vec![quarantine(relative, 0, "json", "invalid_json", "invalid JSON array", payload)])),
    };
    let Some(items) = array.as_array() else {
        return Ok((vec![], vec![quarantine(relative, 0, "json", "type_error", "JSON input must be an array", payload)]));
    };
    let mut records = Vec::new();
    let mut isolated = Vec::new();
    for (offset, raw) in items.iter().enumerate() {
        let fragment = canonical_json(raw);
        match raw_to_record(raw.clone(), relative, offset as u64, "json") {
            Ok(record) => records.push(record),
            Err(error) => isolated.push(quarantine(relative, offset as u64, "json", &error.code, &error.message, fragment.as_bytes())),
        }
    }
    Ok((records, isolated))
}

pub fn parse_directory(input_dir: &Path, config: &Value) -> Result<(Vec<RecordV1>, Vec<QuarantineRecordV1>), ProcessingError> {
    if config.get("parser_version").and_then(Value::as_str) != Some(PARSER_VERSION) {
        return Err(ProcessingError::new("invalid_parsing_config", format!("parser_version must be {PARSER_VERSION}")));
    }
    let root = input_dir.canonicalize().map_err(io_error)?;
    let mut records = Vec::new();
    let mut isolated = Vec::new();
    for entry in scan_directory(&root, None)? {
        let path = root.join(&entry.relative_path);
        if entry.file_status == "empty" { continue; }
        if entry.file_status == "unsupported" {
            let data = fs::read(&path).map_err(io_error)?;
            isolated.push(quarantine(&entry.relative_path, 0, "unknown", "unsupported_format", "unsupported file format", &data));
            continue;
        }
        if entry.file_status == "encoding_error" {
            let data = fs::read(&path).map_err(io_error)?;
            isolated.push(quarantine(&entry.relative_path, 0, &entry.detected_format, "invalid_encoding", "file is not valid UTF-8", &data));
            continue;
        }
        let (mut parsed, mut quarantined) = match entry.detected_format.as_str() {
            "jsonl" => parse_jsonl(&path, &entry.relative_path)?,
            "tsv" => parse_tsv(&path, &entry.relative_path)?,
            "html" => parse_html(&path, &entry.relative_path)?,
            "json" => parse_json_array(&path, &entry.relative_path)?,
            _ => unreachable!(),
        };
        records.append(&mut parsed);
        isolated.append(&mut quarantined);
    }
    records.sort_by(|left, right| (&left.source_file, left.source_offset).cmp(&(&right.source_file, right.source_offset)));
    isolated.sort_by(|left, right| (&left.source_file, left.source_offset, &left.error_code).cmp(&(&right.source_file, right.source_offset, &right.error_code)));
    Ok((records, isolated))
}

fn field_value(record: &Value, path: &str) -> Value {
    let mut value = record;
    for part in path.split('.') {
        let Some(next) = value.as_object().and_then(|object| object.get(part)) else {
            return Value::String(MISSING_SENTINEL.to_owned());
        };
        value = next;
    }
    if value.is_null() { Value::String(NULL_SENTINEL.to_owned()) } else { value.clone() }
}

fn matches_filters(record: &Value, filters: &Map<String, Value>) -> bool {
    filters.iter().all(|(path, expected)| {
        let actual = field_value(record, path);
        if let Some(items) = expected.as_array() {
            items.iter().map(|item| if item.is_null() { Value::String(NULL_SENTINEL.to_owned()) } else { item.clone() }).any(|item| item == actual)
        } else {
            let normalized = if expected.is_null() { Value::String(NULL_SENTINEL.to_owned()) } else { expected.clone() };
            actual == normalized
        }
    })
}

pub fn rank_hex(record_id: &str, seed: u64, algorithm_version: &str) -> String {
    let value = format!("{algorithm_version}\u{1f}{seed}\u{1f}{record_id}");
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

fn allocate_weighted(capacities: &BTreeMap<String, usize>, quotas: &mut BTreeMap<String, usize>, mut remaining: usize, equal: bool) -> Result<(), ProcessingError> {
    while remaining > 0 {
        let active: Vec<_> = capacities.keys().filter(|key| quotas[*key] < capacities[*key]).cloned().collect();
        if active.is_empty() {
            return Err(ProcessingError::new("allocation_error", "unable to allocate target"));
        }
        let weights: BTreeMap<_, _> = active.iter().map(|key| {
            (key.clone(), if equal { 1 } else { capacities[key] - quotas[key] })
        }).collect();
        let weight_sum: usize = weights.values().sum();
        let round_remaining = remaining;
        let mut remainders = BTreeMap::new();
        let mut added = 0;
        for key in &active {
            let share = round_remaining * weights[key];
            let amount = (share / weight_sum).min(capacities[key] - quotas[key]);
            *quotas.get_mut(key).expect("quota exists") += amount;
            added += amount;
            remainders.insert(key.clone(), share % weight_sum);
        }
        remaining -= added;
        let mut ranked = active.clone();
        ranked.sort_by(|left, right| remainders[right].cmp(&remainders[left]).then_with(|| left.cmp(right)));
        for key in ranked {
            if remaining == 0 { break; }
            if quotas[&key] < capacities[&key] {
                *quotas.get_mut(&key).expect("quota exists") += 1;
                remaining -= 1;
            }
        }
    }
    Ok(())
}

pub fn allocate_quotas(capacities: &BTreeMap<String, usize>, plan: &SamplePlanV1) -> Result<BTreeMap<String, usize>, ProcessingError> {
    if plan.target_size > capacities.values().sum() {
        return Err(ProcessingError::new("target_exceeds_population", "target_size exceeds filtered population"));
    }
    let mut quotas: BTreeMap<_, _> = capacities.keys().map(|key| (key.clone(), 0)).collect();
    if plan.allocation_method == "simple_random" {
        let key = capacities.keys().next().ok_or_else(|| ProcessingError::new("target_exceeds_population", "target_size exceeds filtered population"))?;
        quotas.insert(key.clone(), plan.target_size);
        return Ok(quotas);
    }
    if plan.allocation_method == "minimum_then_proportional" {
        for (key, capacity) in capacities {
            quotas.insert(key.clone(), (*capacity).min(plan.minimum_per_stratum));
        }
        if quotas.values().sum::<usize>() > plan.target_size {
            return Err(ProcessingError::new("infeasible_minimum", "minimum allocations exceed target_size"));
        }
    }
    let remaining = plan.target_size - quotas.values().sum::<usize>();
    allocate_weighted(capacities, &mut quotas, remaining, plan.allocation_method == "equal")?;
    Ok(quotas)
}

pub fn sample_records(records: &[RecordV1], plan: &SamplePlanV1) -> Result<(Vec<RecordV1>, Value), ProcessingError> {
    let serialized = values(records);
    let population: Vec<_> = records.iter().zip(serialized.iter()).filter(|(_, value)| matches_filters(value, &plan.filters)).collect();
    if plan.target_size > population.len() {
        return Err(ProcessingError::new("target_exceeds_population", "target_size exceeds filtered population"));
    }
    let mut strata: BTreeMap<String, Vec<RecordV1>> = BTreeMap::new();
    for (record, value) in &population {
        let key = if plan.allocation_method == "simple_random" {
            "[]".to_owned()
        } else {
            canonical_json(&Value::Array(plan.strata_keys.iter().map(|path| field_value(value, path)).collect()))
        };
        strata.entry(key).or_default().push((*record).clone());
    }
    let capacities: BTreeMap<_, _> = strata.iter().map(|(key, items)| (key.clone(), items.len())).collect();
    let quotas = allocate_quotas(&capacities, plan)?;
    let mut selected = Vec::new();
    for (key, items) in &strata {
        let mut ranked = items.clone();
        ranked.sort_by(|left, right| {
            rank_hex(&left.record_id, plan.seed, &plan.algorithm_version).cmp(&rank_hex(&right.record_id, plan.seed, &plan.algorithm_version))
                .then_with(|| left.record_id.cmp(&right.record_id))
        });
        selected.extend(ranked.into_iter().take(quotas[key]));
    }
    selected.sort_by(|left, right| {
        rank_hex(&left.record_id, plan.seed, &plan.algorithm_version).cmp(&rank_hex(&right.record_id, plan.seed, &plan.algorithm_version))
            .then_with(|| left.record_id.cmp(&right.record_id))
    });
    let mut population_values: Vec<_> = population.iter().map(|(_, value)| (*value).clone()).collect();
    population_values.sort_by_key(|value| value["record_id"].as_str().unwrap_or_default().to_owned());
    let selected_values = values(&selected);
    let mut selected_ids: Vec<_> = selected.iter().map(|record| Value::String(record.record_id.clone())).collect();
    selected_ids.sort_by_key(|value| value.as_str().unwrap_or_default().to_owned());
    let strata_report: Vec<_> = capacities.iter().map(|(key, population)| json!({
        "key": key, "population": population, "quota": quotas[key], "selected": quotas[key]
    })).collect();
    let report = json!({
        "algorithm_version": plan.algorithm_version,
        "allocation_method": plan.allocation_method,
        "filtered_population_size": population.len(),
        "plan_fingerprint": sha256_text(&canonical_json(&serde_json::to_value(plan).expect("plan serializes"))),
        "plan_id": plan.plan_id,
        "population_fingerprint": semantic_fingerprint(&population_values),
        "population_size": records.len(),
        "schema_version": "sample-report-v1",
        "seed": plan.seed,
        "selected_id_fingerprint": semantic_fingerprint(&selected_ids),
        "output_fingerprint": semantic_fingerprint(&selected_values),
        "strata": strata_report,
        "strata_keys": plan.strata_keys,
        "target_size": plan.target_size
    });
    Ok((selected, report))
}

fn remove_script_style(text: &str) -> String {
    let mut result = text.to_owned();
    for tag in ["script", "style"] {
        loop {
            let lower = result.to_ascii_lowercase();
            let needle = format!("<{tag}");
            let Some(start) = lower.find(&needle) else { break; };
            let after_name = start + needle.len();
            let next = lower[after_name..].chars().next();
            if next.is_some_and(|character| character.is_ascii_alphanumeric()) {
                let next_start = after_name;
                if let Some(found) = lower[next_start..].find(&needle) {
                    let skip = next_start + found + 1;
                    result.replace_range(start..skip, "<");
                    continue;
                }
                break;
            }
            let Some(open_end_relative) = lower[after_name..].find('>') else { break; };
            let open_end = after_name + open_end_relative + 1;
            let closing = format!("</{tag}");
            let Some(close_relative) = lower[open_end..].find(&closing) else { break; };
            let close_start = open_end + close_relative;
            let Some(close_end_relative) = lower[close_start..].find('>') else { break; };
            let close_end = close_start + close_end_relative + 1;
            result.replace_range(start..close_end, "");
        }
    }
    result
}

fn parse_tag(text: &str) -> Option<(usize, String)> {
    if !text.starts_with('<') { return None; }
    if text.starts_with("<!--") {
        return text.find("-->").map(|end| (end + 3, "comment".to_owned()));
    }
    let bytes = text.as_bytes();
    let mut index = 1;
    if bytes.get(index) == Some(&b'/') { index += 1; }
    let start = index;
    while bytes.get(index).is_some_and(u8::is_ascii_alphanumeric) { index += 1; }
    if index == start || !bytes[start].is_ascii_alphabetic() { return None; }
    if bytes.get(index).is_some_and(|byte| byte.is_ascii_alphanumeric() || *byte == b'_') { return None; }
    let end = text[index..].find('>')? + index + 1;
    Some((end, text[start..index].to_ascii_lowercase()))
}

fn normalize_html(text: &str) -> String {
    let block_tags = ["p", "div", "article", "section", "li", "blockquote", "pre", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "br"];
    let mut output = String::new();
    let mut index = 0;
    while index < text.len() {
        if let Some((length, tag)) = parse_tag(&text[index..]) {
            if tag != "comment" && block_tags.contains(&tag.as_str()) && !output.is_empty() && !output.ends_with('\n') {
                output.push('\n');
            }
            index += length;
        } else {
            let character = text[index..].chars().next().expect("valid char boundary");
            output.push(character);
            index += character.len_utf8();
        }
    }
    decode_entities(&output)
}

fn normalize_whitespace(text: &str) -> String {
    let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
    let mut lines = Vec::new();
    let mut current = String::new();
    for character in normalized.chars() {
        if character == '\n' {
            lines.push(current.trim().to_owned());
            current.clear();
        } else if character.is_whitespace() {
            if !current.is_empty() && !current.ends_with(' ') { current.push(' '); }
        } else {
            current.push(character);
        }
    }
    lines.push(current.trim().to_owned());
    let mut collapsed: Vec<String> = Vec::new();
    for line in lines {
        if !line.is_empty() || collapsed.is_empty() || !collapsed.last().is_some_and(String::is_empty) {
            collapsed.push(line);
        }
    }
    collapsed.join("\n").trim().to_owned()
}

fn strings(value: &Value, key: &str) -> Result<Vec<String>, ProcessingError> {
    value.get(key).and_then(Value::as_array)
        .ok_or_else(|| ProcessingError::new("invalid_cleaning_config", format!("{key} must be an array")))?
        .iter().map(|item| item.as_str().map(str::to_owned).ok_or_else(|| ProcessingError::new("invalid_cleaning_config", format!("{key} values must be strings")))).collect()
}

fn remove_templates(mut text: String, templates: &Value) -> Result<(String, Vec<String>), ProcessingError> {
    let mut matched = Vec::new();
    for prefix in strings(templates, "prefixes")? {
        if text.starts_with(&prefix) {
            text = text[prefix.len()..].to_owned();
            matched.push(prefix);
        }
    }
    for suffix in strings(templates, "suffixes")? {
        if text.ends_with(&suffix) {
            text.truncate(text.len() - suffix.len());
            matched.push(suffix);
        }
    }
    let exact_lines: BTreeSet<_> = strings(templates, "lines")?.into_iter().collect();
    let mut kept = Vec::new();
    for line in text.split('\n') {
        if exact_lines.contains(line) { matched.push(line.to_owned()); } else { kept.push(line); }
    }
    text = kept.join("\n");
    let blocks = templates.get("blocks").and_then(Value::as_array)
        .ok_or_else(|| ProcessingError::new("invalid_cleaning_config", "blocks must be an array"))?;
    for block in blocks {
        let start_marker = block.get("start").and_then(Value::as_str).ok_or_else(|| ProcessingError::new("invalid_cleaning_config", "block start must be a string"))?;
        let end_marker = block.get("end").and_then(Value::as_str).ok_or_else(|| ProcessingError::new("invalid_cleaning_config", "block end must be a string"))?;
        if let Some(start) = text.find(start_marker) {
            if let Some(relative_end) = text[start + start_marker.len()..].find(end_marker) {
                let end = start + start_marker.len() + relative_end + end_marker.len();
                matched.push(text[start..end].to_owned());
                text.replace_range(start..end, "");
            }
        }
    }
    Ok((text.trim().to_owned(), matched))
}

fn cleaning_rules(config: &Value) -> Result<Vec<Value>, ProcessingError> {
    if config.get("schema_version").and_then(Value::as_str) != Some("cleaning-config-v1") {
        return Err(ProcessingError::new("invalid_cleaning_config", "schema_version must be cleaning-config-v1"));
    }
    let mut rules = config.get("rules").and_then(Value::as_array).cloned()
        .ok_or_else(|| ProcessingError::new("invalid_cleaning_config", "rules must be a non-empty list"))?;
    if rules.is_empty() { return Err(ProcessingError::new("invalid_cleaning_config", "rules must be a non-empty list")); }
    rules.sort_by_key(|rule| rule["priority"].as_u64().unwrap_or(u64::MAX));
    let priorities: Vec<_> = rules.iter().map(|rule| rule["priority"].as_u64()).collect();
    if priorities.iter().any(Option::is_none) || priorities.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(ProcessingError::new("duplicate_priority", "cleaning rule priorities must be unique"));
    }
    if rules.iter().any(|rule| rule["match_method"] == "fuzzy") {
        return Err(ProcessingError::new("fuzzy_not_supported", "fuzzy rules are not enabled in deterministic-clean-v1"));
    }
    Ok(rules)
}

fn cleaning_event(record_id: &str, rule: &Value, config: &Value, before: &str, after: &str, decision: &str, matched_span: Option<String>) -> CleaningEventV1 {
    CleaningEventV1 {
        record_id: record_id.to_owned(), stage: "deterministic_cleaning".to_owned(),
        rule_id: rule["rule_id"].as_str().unwrap().to_owned(), rule_version: config["rule_version"].as_str().unwrap().to_owned(),
        match_method: rule["match_method"].as_str().unwrap().to_owned(), action: rule["action"].as_str().unwrap().to_owned(),
        reason_code: rule["reason_code"].as_str().unwrap().to_owned(), decision: decision.to_owned(), matched_span,
        removed_chars: before.chars().count().saturating_sub(after.chars().count()), score: 1.0,
        before_hash: sha256_text(before), after_hash: sha256_text(after), metrics: json!({"priority": rule["priority"]}),
        algorithm_version: config["algorithm_version"].as_str().unwrap().to_owned(), schema_version: "cleaning-event-v1".to_owned(),
    }
}

pub fn clean_record(record: &RecordV1, config: &Value) -> Result<(RecordV1, Vec<CleaningEventV1>), ProcessingError> {
    let rules = cleaning_rules(config)?;
    let mut text = record.raw_text.clone();
    let mut events = Vec::new();
    for rule in rules {
        let before = text.clone();
        let mut matched = None;
        match rule["rule_id"].as_str().unwrap_or_default() {
            "remove_script_style" => {
                text = remove_script_style(&text);
                if text != before { matched = Some("script/style".to_owned()); }
            }
            "normalize_html" => text = normalize_html(&text),
            "remove_zero_width" => {
                let zero_width = ['\u{200b}', '\u{200c}', '\u{200d}', '\u{feff}', '\u{2060}'];
                let found: BTreeSet<_> = text.chars().filter(|character| zero_width.contains(character)).map(|character| format!("U+{:04X}", character as u32)).collect();
                text.retain(|character| !zero_width.contains(&character));
                if !found.is_empty() { matched = Some(found.into_iter().collect::<Vec<_>>().join(",")); }
            }
            "normalize_nfkc" => text = text.nfkc().collect(),
            "normalize_whitespace" => text = normalize_whitespace(&text),
            "remove_exact_templates" => {
                let (cleaned, spans) = remove_templates(text, &config["exact_templates"])?;
                text = cleaned;
                if !spans.is_empty() { matched = Some(spans.join(";")); }
            }
            "flag_empty" => {
                if text.trim().is_empty() { events.push(cleaning_event(&record.record_id, &rule, config, &text, &text, "review", None)); }
                continue;
            }
            "flag_pure_symbol" => {
                if !text.is_empty() && !text.chars().any(char::is_alphanumeric) {
                    events.push(cleaning_event(&record.record_id, &rule, config, &text, &text, "review", None));
                }
                continue;
            }
            "flag_short" => {
                let threshold = config["short_text_threshold"].as_u64().unwrap_or_default() as usize;
                if !text.is_empty() && text.chars().count() < threshold {
                    events.push(cleaning_event(&record.record_id, &rule, config, &text, &text, "review", None));
                }
                continue;
            }
            other => return Err(ProcessingError::new("unknown_cleaning_rule", format!("unknown rule_id: {other}"))),
        }
        if text != before {
            events.push(cleaning_event(&record.record_id, &rule, config, &before, &text, "applied", matched));
        }
    }
    let mut cleaned = record.clone();
    cleaned.clean_text = Some(text);
    Ok((cleaned, events))
}

pub fn clean_records(records: &[RecordV1], config: &Value) -> Result<(Vec<RecordV1>, Vec<CleaningEventV1>), ProcessingError> {
    let mut cleaned = Vec::new();
    let mut events = Vec::new();
    for record in records {
        let (result, mut record_events) = clean_record(record, config)?;
        cleaned.push(result);
        events.append(&mut record_events);
    }
    Ok((cleaned, events))
}

pub fn changed_characters(before: &str, after: &str) -> usize {
    let left: Vec<_> = before.chars().collect();
    let right: Vec<_> = after.chars().collect();
    let common = left.len().min(right.len());
    (0..common).filter(|index| left[*index] != right[*index]).count() + left.len().abs_diff(right.len())
}

pub fn paired_sample(before: &[RecordV1], after: &[RecordV1], events: &[CleaningEventV1]) -> Vec<Value> {
    let after_by_id: BTreeMap<_, _> = after.iter().map(|record| (record.record_id.as_str(), record)).collect();
    let mut rules: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for event in events {
        rules.entry(&event.record_id).or_default().push(&event.rule_id);
    }
    before.iter().map(|record| {
        let cleaned = after_by_id[record.record_id.as_str()];
        let after_text = cleaned.clean_text.as_deref().unwrap_or_default();
        json!({
            "after": cleaned.clean_text,
            "before": record.raw_text,
            "changed": record.raw_text != after_text,
            "changed_characters": changed_characters(&record.raw_text, after_text),
            "record_id": record.record_id,
            "rule_ids": rules.get(record.record_id.as_str()).cloned().unwrap_or_default()
        })
    }).collect()
}

pub fn read_records(path: &Path) -> Result<Vec<RecordV1>, ProcessingError> {
    let input = fs::read_to_string(path).map_err(io_error)?;
    validate_jsonl(&input).map_err(|error| ProcessingError::new(error.error.error_type, error.to_string()))
}

pub fn clean_to_files(input_path: &Path, config_path: &Path, output_path: &Path, events_path: &Path, cache_manifest_path: &Path) -> Result<(Vec<RecordV1>, Vec<CleaningEventV1>, bool), ProcessingError> {
    let config = load_json(config_path)?;
    cleaning_rules(&config)?;
    let input_fingerprint = sha256_file(input_path)?;
    let config_fingerprint = sha256_text(&canonical_json(&config));
    let algorithm_version = config["algorithm_version"].as_str().unwrap_or_default();
    let cache_key = sha256_text(&[algorithm_version, &input_fingerprint, &config_fingerprint].join("\u{1f}"));
    if cache_manifest_path.exists() && output_path.exists() && events_path.exists() {
        let cached = load_json(cache_manifest_path)?;
        if cached["cache_key"] == cache_key
            && cached["input_fingerprint"] == input_fingerprint
            && cached["config_fingerprint"] == config_fingerprint
            && cached["cleaned_output_hash"] == sha256_file(output_path)?
            && cached["events_output_hash"] == sha256_file(events_path)?
        {
            let cleaned: Vec<RecordV1> = fs::read_to_string(output_path).map_err(io_error)?.lines()
                .map(|line| serde_json::from_str(line).map_err(|error| ProcessingError::new("invalid_json", error.to_string()))).collect::<Result<_, _>>()?;
            let events: Vec<CleaningEventV1> = fs::read_to_string(events_path).map_err(io_error)?.lines()
                .map(|line| serde_json::from_str(line).map_err(|error| ProcessingError::new("invalid_json", error.to_string()))).collect::<Result<_, _>>()?;
            return Ok((cleaned, events, true));
        }
    }
    let records = read_records(input_path)?;
    let (cleaned, events) = clean_records(&records, &config)?;
    write_jsonl(output_path, &values(&cleaned))?;
    write_jsonl(events_path, &values(&events))?;
    write_json(cache_manifest_path, &json!({
        "algorithm_version": algorithm_version,
        "cache_key": cache_key,
        "cleaned_output_hash": sha256_file(output_path)?,
        "config_fingerprint": config_fingerprint,
        "events_output_hash": sha256_file(events_path)?,
        "input_fingerprint": input_fingerprint,
        "schema_version": "clean-cache-v1"
    }))?;
    Ok((cleaned, events, false))
}

pub fn pipeline(input_dir: &Path, parsing_config_path: &Path, cleaning_config_path: &Path, sample_plan_path: &Path, output_dir: &Path) -> Result<Value, ProcessingError> {
    pipeline_with_fuzzy(input_dir, parsing_config_path, cleaning_config_path, sample_plan_path, output_dir, None)
}

pub fn pipeline_with_fuzzy(input_dir: &Path, parsing_config_path: &Path, cleaning_config_path: &Path, sample_plan_path: &Path, output_dir: &Path, fuzzy_config_path: Option<&Path>) -> Result<Value, ProcessingError> {
    fs::create_dir_all(output_dir).map_err(io_error)?;
    let parsing_config = load_json(parsing_config_path)?;
    let cleaning_config = load_json(cleaning_config_path)?;
    let plan = SamplePlanV1::from_value(load_json(sample_plan_path)?)?;
    let file_manifest_path = output_dir.join("file-manifest.jsonl");
    let manifest = scan_directory(input_dir, Some(&file_manifest_path))?;
    write_jsonl(&file_manifest_path, &values(&manifest))?;
    let (records, quarantine) = parse_directory(input_dir, &parsing_config)?;
    let parsed_path = output_dir.join("parsed-records.jsonl");
    let quarantine_path = output_dir.join("quarantine.jsonl");
    write_jsonl(&parsed_path, &values(&records))?;
    write_jsonl(&quarantine_path, &values(&quarantine))?;
    let (sampled, sample_report) = sample_records(&records, &plan)?;
    let sampled_path = output_dir.join("sampled-before.jsonl");
    let sample_report_path = output_dir.join("sample-report.json");
    write_jsonl(&sampled_path, &values(&sampled))?;
    write_json(&sample_report_path, &sample_report)?;
    let cleaned_path = output_dir.join("cleaned-records.jsonl");
    let events_path = output_dir.join("cleaning-events.jsonl");
    let (cleaned, events, _) = clean_to_files(
        &parsed_path, cleaning_config_path, &cleaned_path, &events_path, &output_dir.join(".clean-cache.json")
    )?;
    let pairs_path = output_dir.join("sampled-pairs.jsonl");
    write_jsonl(&pairs_path, &paired_sample(&sampled, &cleaned, &events))?;
    let mut output_names = vec![
        "file-manifest.jsonl", "parsed-records.jsonl", "quarantine.jsonl", "sampled-before.jsonl",
        "cleaned-records.jsonl", "cleaning-events.jsonl", "sampled-pairs.jsonl", "sample-report.json",
    ];
    let mut fuzzy_config_value = None;
    if let Some(path) = fuzzy_config_path {
        let (fuzzy_config, config_value) = crate::fuzzy::load_config(path)?;
        let (fuzzy_cleaned, fuzzy_events, fuzzy_decisions) = crate::fuzzy::fuzzy_clean_records(&cleaned, &fuzzy_config)?;
        let decision_values: Vec<_> = fuzzy_decisions.iter().map(|item| serde_json::to_value(item).unwrap()).collect();
        let review_values: Vec<_> = decision_values.iter().zip(&fuzzy_decisions)
            .filter(|(_, item)| item.decision == "review").map(|(value, _)| value.clone()).collect();
        let mut counts = BTreeMap::<String, usize>::new();
        for item in &fuzzy_decisions { *counts.entry(item.decision.clone()).or_default() += 1; }
        let fuzzy_report = json!({
            "algorithm_version": fuzzy_config.algorithm_version,
            "counts": counts,
            "decision_fingerprint": semantic_fingerprint(&decision_values),
            "record_count": cleaned.len(),
            "schema_version": "fuzzy-decision-report-v1",
            "thresholds": fuzzy_config.thresholds,
        });
        write_jsonl(&output_dir.join("fuzzy-cleaned-records.jsonl"), &values(&fuzzy_cleaned))?;
        write_jsonl(&output_dir.join("fuzzy-cleaning-events.jsonl"), &values(&fuzzy_events))?;
        write_jsonl(&output_dir.join("fuzzy-decisions.jsonl"), &decision_values)?;
        write_jsonl(&output_dir.join("fuzzy-review-queue.jsonl"), &review_values)?;
        write_json(&output_dir.join("fuzzy-decision-report.json"), &fuzzy_report)?;
        output_names.extend([
            "fuzzy-cleaned-records.jsonl", "fuzzy-cleaning-events.jsonl", "fuzzy-decisions.jsonl",
            "fuzzy-review-queue.jsonl", "fuzzy-decision-report.json",
        ]);
        fuzzy_config_value = Some(config_value);
    }
    let mut outputs = Map::new();
    for name in output_names {
        outputs.insert(name.to_owned(), Value::String(sha256_file(&output_dir.join(name))?));
    }
    let mut run_manifest = json!({
        "algorithm_version": if fuzzy_config_value.is_some() { "pipeline-v1+fuzzy-template-clean-v1" } else { PIPELINE_VERSION },
        "cleaning_config_fingerprint": sha256_text(&canonical_json(&cleaning_config)),
        "input_fingerprint": semantic_fingerprint(&values(&manifest)),
        "outputs": outputs,
        "parsing_config_fingerprint": sha256_text(&canonical_json(&parsing_config)),
        "plan_fingerprint": sha256_text(&canonical_json(&serde_json::to_value(&plan).expect("plan serializes"))),
        "schema_version": "run-manifest-v1"
    });
    if let Some(config) = fuzzy_config_value {
        run_manifest["fuzzy_config_fingerprint"] = Value::String(sha256_text(&canonical_json(&config)));
    }
    write_json(&output_dir.join("run-manifest.json"), &run_manifest)?;
    Ok(run_manifest)
}
