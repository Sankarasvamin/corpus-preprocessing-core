use corpus_preprocessing_core::{parse_record, profile_jsonl, RecordType};
use serde_json::{json, Value};
use std::fs;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

fn record() -> Value {
    json!({
        "record_id": "r-1",
        "dataset_id": "category_a",
        "source_file": "raw/example.jsonl",
        "source_offset": 0,
        "record_type": "article",
        "event_date": "2026-08-20",
        "title": "标题",
        "raw_text": "正文",
        "clean_text": null,
        "metadata": {},
        "parser_version": "test-v1",
        "schema_version": "record-v1"
    })
}

#[test]
fn record_v1_deserializes() {
    let parsed = parse_record(&record().to_string()).unwrap();
    assert_eq!(parsed.record_type, RecordType::Article);
    assert_eq!(parsed.clean_text, None);
}

#[test]
fn missing_required_field_is_rejected() {
    let mut value = record();
    value.as_object_mut().unwrap().remove("record_id");
    let error = parse_record(&value.to_string()).unwrap_err();
    assert_eq!(error.error_type, "missing_field");
    assert_eq!(error.field.as_deref(), Some("record_id"));
}

#[test]
fn invalid_record_type_is_rejected() {
    let mut value = record();
    value["record_type"] = json!("other");
    let error = parse_record(&value.to_string()).unwrap_err();
    assert_eq!(error.error_type, "invalid_value");
    assert_eq!(error.field.as_deref(), Some("record_type"));
}

#[test]
fn profile_statistics_are_correct() {
    let first = record();
    let mut second = record();
    second["dataset_id"] = json!("category_b");
    second["record_type"] = json!("comment");
    second["event_date"] = Value::Null;
    second["title"] = Value::Null;
    second["raw_text"] = json!("  ");
    let input = format!("{}\n{}\n", first, second);
    let profile = profile_jsonl(&input).unwrap();
    assert_eq!(profile.total_records, 2);
    assert_eq!(profile.by_dataset_id["category_b"], 1);
    assert_eq!(profile.by_event_date["<null>"], 1);
    assert_eq!(profile.missing_title_count, 1);
    assert_eq!(profile.missing_or_empty_body_count, 1);
    assert_eq!(profile.unique_record_id_count, 1);
    assert_eq!(profile.duplicate_record_id_count, 1);
}

#[test]
fn cli_success_and_failure_exit_codes() {
    let unique = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
    let directory = std::env::temp_dir().join(format!("corpus-core-test-{}-{unique}", std::process::id()));
    fs::create_dir(&directory).unwrap();
    let valid = directory.join("valid.jsonl");
    let invalid = directory.join("invalid.jsonl");
    let output = directory.join("profile.json");
    fs::write(&valid, format!("{}\n", record())).unwrap();
    fs::write(&invalid, "{\"record_id\":\n").unwrap();
    let binary = env!("CARGO_BIN_EXE_corpus-preprocessing-core");

    let ok = Command::new(binary).args(["validate", "--input"]).arg(&valid).output().unwrap();
    let bad = Command::new(binary).args(["validate", "--input"]).arg(&invalid).output().unwrap();
    let profiled = Command::new(binary)
        .args(["profile", "--input"])
        .arg(&valid)
        .arg("--output")
        .arg(&output)
        .output()
        .unwrap();

    assert!(ok.status.success());
    assert!(profiled.status.success());
    assert_eq!(serde_json::from_str::<Value>(&fs::read_to_string(output).unwrap()).unwrap()["total_records"], 1);
    assert!(!bad.status.success());
    assert!(String::from_utf8_lossy(&bad.stderr).contains("line 1 [invalid_json]"));
    fs::remove_dir_all(directory).unwrap();
}
