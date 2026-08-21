use corpus_preprocessing_core::phase2::{
    allocate_quotas, clean_record, clean_records, clean_to_files, load_json, parse_directory,
    pipeline, sample_records, scan_directory, write_jsonl, SamplePlanV1,
};
use corpus_preprocessing_core::{RecordType, RecordV1};
use serde_json::{json, to_value, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf()
}

fn temp_dir(name: &str) -> PathBuf {
    let unique = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
    let path = std::env::temp_dir().join(format!("corpus-phase2-{name}-{}-{unique}", std::process::id()));
    fs::create_dir(&path).unwrap();
    path
}

fn record(record_id: &str) -> RecordV1 {
    RecordV1 {
        record_id: record_id.to_owned(), dataset_id: "category_a".to_owned(), source_file: "records.jsonl".to_owned(),
        source_offset: 0, record_type: RecordType::Article, event_date: Some("2026-08-01".to_owned()),
        title: Some("标题".to_owned()), raw_text: "普通正文内容".to_owned(), clean_text: None,
        metadata: serde_json::from_value(json!({"source_format":"jsonl"})).unwrap(),
        parser_version: "parser-v1".to_owned(), schema_version: "record-v1".to_owned(),
    }
}

fn plan(method: &str, target: usize, strata: &[&str], minimum: usize, seed: u64) -> SamplePlanV1 {
    SamplePlanV1 {
        plan_id: format!("test-{method}"), seed, target_size: target,
        strata_keys: strata.iter().map(|value| (*value).to_owned()).collect(), allocation_method: method.to_owned(),
        minimum_per_stratum: minimum, filters: Default::default(), algorithm_version: "hash-rank-v1".to_owned(),
        schema_version: "sample-plan-v1".to_owned(),
    }
}

fn raw(index: usize) -> Value {
    let record_type = ["article", "comment", "reply"][index % 3];
    json!({
        "record_id": format!("large-{index:05}"), "dataset_id": "category_a", "event_date": "2026-08-01",
        "record_type": record_type, "title": if index % 3 == 0 { json!("标题") } else { Value::Null },
        "raw_text": format!("正文 {index}"), "category_signal": "测试信号", "source_batch": "large", "synthetic": true
    })
}

#[test]
fn main_parse_restores_240_truth_records() {
    let config = load_json(&root().join("configs/parsing-v1.json")).unwrap();
    let (mut parsed, quarantine) = parse_directory(&root().join("fixtures/generated/raw"), &config).unwrap();
    let mut truth: Vec<RecordV1> = fs::read_to_string(root().join("fixtures/generated/normalized/records.jsonl")).unwrap()
        .lines().map(|line| serde_json::from_str(line).unwrap()).collect();
    parsed.sort_by_key(|record| record.record_id.clone());
    truth.sort_by_key(|record| record.record_id.clone());
    assert_eq!(parsed, truth);
    assert_eq!(quarantine.iter().map(|item| item.error_code.as_str()).collect::<Vec<_>>(), ["invalid_json", "missing_field", "unknown_schema_drift", "malformed_tsv"]);
}

#[test]
fn parser_cases_cover_encoding_formats_and_recovery() {
    let cases = root().join("fixtures/parser_cases");
    let manifest = scan_directory(&cases, None).unwrap();
    let statuses: BTreeMap<_, _> = manifest.iter().map(|entry| (entry.relative_path.as_str(), (entry.encoding_status.as_str(), entry.file_status.as_str()))).collect();
    assert_eq!(statuses["bom.jsonl"].0, "utf8_bom");
    assert_eq!(statuses["invalid-utf8.jsonl"].1, "encoding_error");
    assert_eq!(statuses["empty.jsonl"].1, "empty");
    assert_eq!(statuses["unknown.bin"].1, "unsupported");
    let config = load_json(&root().join("configs/parsing-v1.json")).unwrap();
    let (records, quarantine) = parse_directory(&cases, &config).unwrap();
    let ids: BTreeSet<_> = records.iter().map(|record| record.record_id.as_str()).collect();
    assert_eq!(ids, BTreeSet::from(["case-array-1", "case-array-2", "case-bom-1", "case-after-bad-json", "case-after-bad-tsv"]));
    let codes: BTreeSet<_> = quarantine.iter().map(|item| item.error_code.as_str()).collect();
    assert_eq!(codes, BTreeSet::from(["invalid_encoding", "unsupported_format", "missing_field", "invalid_json", "malformed_tsv", "unknown_schema_drift"]));
}

#[test]
fn streams_10000_jsonl_and_tsv_records() {
    let directory = temp_dir("large");
    let mut jsonl = BufWriter::new(File::create(directory.join("large.jsonl")).unwrap());
    for index in 0..5000 { writeln!(jsonl, "{}", serde_json::to_string(&raw(index)).unwrap()).unwrap(); }
    drop(jsonl);
    let headers = ["record_id", "dataset_id", "event_date_json", "record_type", "title_json", "raw_text_json", "category_signal_json", "source_batch", "synthetic"];
    let mut tsv = BufWriter::new(File::create(directory.join("large.tsv")).unwrap());
    writeln!(tsv, "{}", headers.join("\t")).unwrap();
    for index in 5000..10000 {
        let item = raw(index);
        let fields = [
            item["record_id"].as_str().unwrap().to_owned(), item["dataset_id"].as_str().unwrap().to_owned(), serde_json::to_string(&item["event_date"]).unwrap(),
            item["record_type"].as_str().unwrap().to_owned(), serde_json::to_string(&item["title"]).unwrap(), serde_json::to_string(&item["raw_text"]).unwrap(),
            serde_json::to_string(&item["category_signal"]).unwrap(), item["source_batch"].as_str().unwrap().to_owned(), "true".to_owned(),
        ];
        writeln!(tsv, "{}", fields.join("\t")).unwrap();
    }
    drop(tsv);
    let config = load_json(&root().join("configs/parsing-v1.json")).unwrap();
    let (records, quarantine) = parse_directory(&directory, &config).unwrap();
    assert_eq!(records.len(), 10000);
    assert!(quarantine.is_empty());
    fs::remove_dir_all(directory).unwrap();
}

#[test]
fn hash_sampling_is_seeded_and_input_order_independent() {
    let records: Vec<_> = (0..50).map(|index| record(&format!("r-{index:03}"))).collect();
    let first = sample_records(&records, &plan("simple_random", 10, &[], 0, 11)).unwrap();
    let mut reversed = records.clone();
    reversed.reverse();
    let repeated = sample_records(&reversed, &plan("simple_random", 10, &[], 0, 11)).unwrap();
    let changed = sample_records(&records, &plan("simple_random", 10, &[], 0, 12)).unwrap();
    assert_eq!(first, repeated);
    assert_ne!(first.0.iter().map(|record| &record.record_id).collect::<BTreeSet<_>>(), changed.0.iter().map(|record| &record.record_id).collect());
}

#[test]
fn allocation_methods_small_layers_and_ties() {
    let capacities = BTreeMap::from([("[\"a\"]".to_owned(), 1), ("[\"b\"]".to_owned(), 4), ("[\"c\"]".to_owned(), 5)]);
    assert_eq!(allocate_quotas(&capacities, &plan("proportional", 5, &["dataset_id"], 0, 7)).unwrap(), BTreeMap::from([("[\"a\"]".to_owned(), 1), ("[\"b\"]".to_owned(), 2), ("[\"c\"]".to_owned(), 2)]));
    assert_eq!(allocate_quotas(&capacities, &plan("equal", 6, &["dataset_id"], 0, 7)).unwrap(), BTreeMap::from([("[\"a\"]".to_owned(), 1), ("[\"b\"]".to_owned(), 3), ("[\"c\"]".to_owned(), 2)]));
    let minimum = allocate_quotas(&capacities, &plan("minimum_then_proportional", 7, &["dataset_id"], 2, 7)).unwrap();
    assert_eq!(minimum.values().sum::<usize>(), 7);
    assert_eq!(minimum["[\"a\"]"], 1);
    let tied = allocate_quotas(&BTreeMap::from([("[\"a\"]".to_owned(), 3), ("[\"b\"]".to_owned(), 3)]), &plan("proportional", 3, &["dataset_id"], 0, 7)).unwrap();
    assert_eq!(tied, BTreeMap::from([("[\"a\"]".to_owned(), 2), ("[\"b\"]".to_owned(), 1)]));
}

#[test]
fn infeasible_and_target_errors_are_stable() {
    let capacities = BTreeMap::from([("a".to_owned(), 5), ("b".to_owned(), 5)]);
    assert_eq!(allocate_quotas(&capacities, &plan("minimum_then_proportional", 3, &["dataset_id"], 2, 7)).unwrap_err().code, "infeasible_minimum");
    let records = vec![record("a")];
    assert_eq!(sample_records(&records, &plan("simple_random", 2, &[], 0, 7)).unwrap_err().code, "target_exceeds_population");
}

#[test]
fn null_missing_strata_and_filters_work() {
    let mut first = record("a");
    first.event_date = None;
    first.metadata.clear();
    let mut second = record("b");
    second.metadata.clear();
    let mut third = record("c");
    third.dataset_id = "category_b".to_owned();
    third.metadata = serde_json::from_value(json!({"source_format":"tsv"})).unwrap();
    let records = vec![first, second, third];
    let (_, report) = sample_records(&records, &plan("equal", 2, &["event_date", "metadata.source_format"], 0, 7)).unwrap();
    let keys: Vec<_> = report["strata"].as_array().unwrap().iter().map(|item| item["key"].as_str().unwrap()).collect();
    assert!(keys.iter().any(|key| key.contains("__NULL__")));
    assert!(keys.iter().any(|key| key.contains("__MISSING__")));
    let mut filtered_plan = plan("simple_random", 1, &[], 0, 7);
    filtered_plan.filters.insert("dataset_id".to_owned(), json!("category_b"));
    assert_eq!(sample_records(&records, &filtered_plan).unwrap().0[0].record_id, "c");
}

#[test]
fn golden_cleaning_cases_and_raw_text_pass() {
    let config = load_json(&root().join("configs/cleaning-v1.json")).unwrap();
    for (index, line) in fs::read_to_string(root().join("fixtures/golden/cleaning-v1.jsonl")).unwrap().lines().enumerate() {
        let case: Value = serde_json::from_str(line).unwrap();
        let mut input = record(&format!("golden-{index}"));
        input.raw_text = case["raw_text"].as_str().unwrap().to_owned();
        let (cleaned, events) = clean_record(&input, &config).unwrap();
        let reviews: Vec<_> = events.iter().filter(|event| event.decision == "review").map(|event| event.reason_code.as_str()).collect();
        let expected_reviews: Vec<_> = case["expected_review_reasons"].as_array().unwrap().iter().map(|item| item.as_str().unwrap()).collect();
        assert_eq!(cleaned.clean_text.as_deref(), case["expected_clean_text"].as_str(), "{}", case["case_id"]);
        assert_eq!(reviews, expected_reviews, "{}", case["case_id"]);
        assert_eq!(cleaned.raw_text, input.raw_text);
    }
}

#[test]
fn injected_regression_priority_and_idempotency_pass() {
    let config = load_json(&root().join("configs/cleaning-v1.json")).unwrap();
    let truth: Vec<RecordV1> = fs::read_to_string(root().join("fixtures/generated/normalized/records.jsonl")).unwrap().lines().map(|line| serde_json::from_str(line).unwrap()).collect();
    let (cleaned, events) = clean_records(&truth, &config).unwrap();
    let by_id: BTreeMap<_, _> = cleaned.iter().map(|record| (record.record_id.as_str(), record.clean_text.as_deref().unwrap())).collect();
    assert_eq!(by_id["syn-0001"], "");
    assert_eq!(by_id["syn-0002"], "");
    assert_eq!(by_id["syn-0003"], "零宽字符样本");
    assert_eq!(by_id["syn-0004"], "Full-width 123 与 half-width 123");
    assert_eq!(by_id["syn-0005"], "正文含标签&实体");
    assert_eq!(by_id["syn-0006"], "正文内容");
    let (recleaned, second_events) = clean_records(&cleaned, &config).unwrap();
    assert_eq!(cleaned, recleaned);
    assert_eq!(events, second_events);
    let mut bad = config.clone();
    bad["rules"][1]["priority"] = bad["rules"][0]["priority"].clone();
    assert_eq!(clean_record(&record("bad"), &bad).unwrap_err().code, "duplicate_priority");
}

#[test]
fn four_dates_and_noop_events_pass() {
    let config = load_json(&root().join("configs/cleaning-v1.json")).unwrap();
    let truth: Vec<RecordV1> = fs::read_to_string(root().join("fixtures/generated/normalized/records.jsonl")).unwrap().lines().map(|line| serde_json::from_str(line).unwrap()).collect();
    let (cleaned, _) = clean_records(&truth, &config).unwrap();
    let dates: BTreeSet<_> = cleaned.iter().filter_map(|record| record.event_date.as_deref()).collect();
    assert_eq!(dates, BTreeSet::from(["2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22"]));
    let (_, events) = clean_record(&record("noop"), &config).unwrap();
    assert!(!events.iter().any(|event| event.decision == "applied"));
}

#[test]
fn cache_hit_corruption_and_config_invalidation_work() {
    let directory = temp_dir("cache");
    let input = directory.join("input.jsonl");
    let output = directory.join("cleaned.jsonl");
    let events = directory.join("events.jsonl");
    let cache = directory.join("cache.json");
    let mut item = record("cache");
    item.raw_text = "零\u{200b}宽".to_owned();
    write_jsonl(&input, &[to_value(item).unwrap()]).unwrap();
    let first = clean_to_files(&input, &root().join("configs/cleaning-v1.json"), &output, &events, &cache).unwrap();
    let second = clean_to_files(&input, &root().join("configs/cleaning-v1.json"), &output, &events, &cache).unwrap();
    assert!(!first.2);
    assert!(second.2);
    assert_eq!(first.0, second.0);
    assert_eq!(first.1, second.1);
    fs::write(&output, "corrupt\n").unwrap();
    assert!(!clean_to_files(&input, &root().join("configs/cleaning-v1.json"), &output, &events, &cache).unwrap().2);
    let old_key = load_json(&cache).unwrap()["cache_key"].clone();
    let changed_config = directory.join("cleaning.json");
    let mut config = load_json(&root().join("configs/cleaning-v1.json")).unwrap();
    config["short_text_threshold"] = json!(7);
    fs::write(&changed_config, serde_json::to_string(&config).unwrap()).unwrap();
    clean_to_files(&input, &changed_config, &output, &events, &cache).unwrap();
    let config_key = load_json(&cache).unwrap()["cache_key"].clone();
    assert_ne!(old_key, config_key);
    let mut changed = record("cache");
    changed.raw_text = "输入变化\u{200b}".to_owned();
    write_jsonl(&input, &[to_value(changed).unwrap()]).unwrap();
    clean_to_files(&input, &changed_config, &output, &events, &cache).unwrap();
    assert_ne!(config_key, load_json(&cache).unwrap()["cache_key"]);
    fs::remove_dir_all(directory).unwrap();
}

#[test]
fn pipeline_is_complete_and_repeatable() {
    let directory = temp_dir("pipeline");
    let first = directory.join("first");
    let second = directory.join("second");
    let one = pipeline(&root().join("fixtures/generated/raw"), &root().join("configs/parsing-v1.json"), &root().join("configs/cleaning-v1.json"), &root().join("configs/sample-multifield-v1.json"), &first).unwrap();
    let two = pipeline(&root().join("fixtures/generated/raw"), &root().join("configs/parsing-v1.json"), &root().join("configs/cleaning-v1.json"), &root().join("configs/sample-multifield-v1.json"), &second).unwrap();
    assert_eq!(one, two);
    assert_eq!(fs::read_to_string(first.join("parsed-records.jsonl")).unwrap().lines().count(), 240);
    assert_eq!(fs::read_to_string(first.join("quarantine.jsonl")).unwrap().lines().count(), 4);
    assert_eq!(fs::read_to_string(first.join("sampled-before.jsonl")).unwrap().lines().count(), 60);
    fs::remove_dir_all(directory).unwrap();
}
