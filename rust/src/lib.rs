use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::fmt;

pub mod phase2;
pub mod fuzzy;

const REQUIRED_FIELDS: [&str; 12] = [
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
];

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum RecordType {
    Article,
    Comment,
    Reply,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecordV1 {
    pub record_id: String,
    pub dataset_id: String,
    pub source_file: String,
    pub source_offset: u64,
    pub record_type: RecordType,
    pub event_date: Option<String>,
    pub title: Option<String>,
    pub raw_text: String,
    pub clean_text: Option<String>,
    pub metadata: Map<String, Value>,
    pub parser_version: String,
    pub schema_version: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationError {
    pub error_type: &'static str,
    pub field: Option<String>,
    pub message: String,
}

impl ValidationError {
    fn new(error_type: &'static str, field: Option<&str>, message: impl Into<String>) -> Self {
        Self {
            error_type,
            field: field.map(str::to_owned),
            message: message.into(),
        }
    }
}

impl fmt::Display for ValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.field {
            Some(field) => write!(formatter, "[{}] field={}: {}", self.error_type, field, self.message),
            None => write!(formatter, "[{}]: {}", self.error_type, self.message),
        }
    }
}

impl std::error::Error for ValidationError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LineValidationError {
    pub line_number: usize,
    pub error: ValidationError,
}

impl fmt::Display for LineValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "line {} {}", self.line_number, self.error)
    }
}

impl std::error::Error for LineValidationError {}

fn required_string<'a>(object: &'a Map<String, Value>, field: &str, allow_empty: bool) -> Result<&'a str, ValidationError> {
    let value = object[field]
        .as_str()
        .ok_or_else(|| ValidationError::new("type_error", Some(field), "expected string"))?;
    if !allow_empty && value.is_empty() {
        return Err(ValidationError::new("invalid_value", Some(field), "must not be empty"));
    }
    Ok(value)
}

fn nullable_string(object: &Map<String, Value>, field: &str) -> Result<(), ValidationError> {
    let value = &object[field];
    if value.is_null() || value.is_string() {
        Ok(())
    } else {
        Err(ValidationError::new("type_error", Some(field), "expected string or null"))
    }
}

fn valid_date(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 10
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| index == 4 || index == 7 || byte.is_ascii_digit())
}

pub fn parse_record(line: &str) -> Result<RecordV1, ValidationError> {
    let value: Value = serde_json::from_str(line)
        .map_err(|error| ValidationError::new("invalid_json", None, error.to_string()))?;
    let object = value
        .as_object()
        .ok_or_else(|| ValidationError::new("type_error", None, "record must be a JSON object"))?;

    for field in REQUIRED_FIELDS {
        if !object.contains_key(field) {
            return Err(ValidationError::new("missing_field", Some(field), "required field is missing"));
        }
    }
    if let Some(field) = object.keys().find(|field| !REQUIRED_FIELDS.contains(&field.as_str())) {
        return Err(ValidationError::new("unexpected_field", Some(field), "field is not defined by record-v1"));
    }

    for field in ["record_id", "dataset_id", "source_file", "parser_version"] {
        required_string(object, field, false)?;
    }
    required_string(object, "raw_text", true)?;
    let schema_version = required_string(object, "schema_version", true)?;
    if schema_version != "record-v1" {
        return Err(ValidationError::new("invalid_value", Some("schema_version"), "expected record-v1"));
    }

    if object["source_offset"].as_u64().is_none() {
        let error_type = if object["source_offset"].is_number() {
            "invalid_value"
        } else {
            "type_error"
        };
        return Err(ValidationError::new(
            error_type,
            Some("source_offset"),
            "expected a non-negative unsigned 64-bit integer",
        ));
    }

    let record_type = required_string(object, "record_type", true)?;
    if !matches!(record_type, "article" | "comment" | "reply") {
        return Err(ValidationError::new(
            "invalid_value",
            Some("record_type"),
            "expected article, comment, or reply",
        ));
    }

    for field in ["event_date", "title", "clean_text"] {
        nullable_string(object, field)?;
    }
    if let Some(event_date) = object["event_date"].as_str() {
        if !valid_date(event_date) {
            return Err(ValidationError::new("invalid_value", Some("event_date"), "expected YYYY-MM-DD or null"));
        }
    }
    if !object["metadata"].is_object() {
        return Err(ValidationError::new("type_error", Some("metadata"), "expected object"));
    }

    serde_json::from_value(value)
        .map_err(|error| ValidationError::new("type_error", None, error.to_string()))
}

pub fn validate_jsonl(input: &str) -> Result<Vec<RecordV1>, LineValidationError> {
    input
        .lines()
        .enumerate()
        .map(|(index, line)| {
            parse_record(line).map_err(|error| LineValidationError {
                line_number: index + 1,
                error,
            })
        })
        .collect()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Profile {
    pub total_records: usize,
    pub by_dataset_id: BTreeMap<String, usize>,
    pub by_record_type: BTreeMap<String, usize>,
    pub by_event_date: BTreeMap<String, usize>,
    pub missing_title_count: usize,
    pub missing_or_empty_body_count: usize,
    pub unique_record_id_count: usize,
    pub duplicate_record_id_count: usize,
    pub schema_versions: BTreeMap<String, usize>,
}

fn increment(map: &mut BTreeMap<String, usize>, key: impl Into<String>) {
    *map.entry(key.into()).or_default() += 1;
}

pub fn profile_records(records: &[RecordV1]) -> Profile {
    let mut by_dataset_id = BTreeMap::new();
    let mut by_record_type = BTreeMap::new();
    let mut by_event_date = BTreeMap::new();
    let mut schema_versions = BTreeMap::new();
    let mut record_ids = BTreeMap::new();
    let mut missing_title_count = 0;
    let mut missing_or_empty_body_count = 0;

    for record in records {
        increment(&mut by_dataset_id, record.dataset_id.clone());
        increment(
            &mut by_record_type,
            match record.record_type {
                RecordType::Article => "article",
                RecordType::Comment => "comment",
                RecordType::Reply => "reply",
            },
        );
        increment(
            &mut by_event_date,
            record.event_date.clone().unwrap_or_else(|| "<null>".to_owned()),
        );
        increment(&mut schema_versions, record.schema_version.clone());
        increment(&mut record_ids, record.record_id.clone());
        missing_title_count += usize::from(record.title.as_deref().map_or(true, |title| title.trim().is_empty()));
        missing_or_empty_body_count += usize::from(record.raw_text.trim().is_empty());
    }

    Profile {
        total_records: records.len(),
        by_dataset_id,
        by_record_type,
        by_event_date,
        missing_title_count,
        missing_or_empty_body_count,
        unique_record_id_count: record_ids.len(),
        duplicate_record_id_count: record_ids.values().map(|count| count - 1).sum(),
        schema_versions,
    }
}

pub fn profile_jsonl(input: &str) -> Result<Profile, LineValidationError> {
    validate_jsonl(input).map(|records| profile_records(&records))
}
