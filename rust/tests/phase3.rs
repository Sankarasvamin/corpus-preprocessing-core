use corpus_preprocessing_core::fuzzy::{
    char_jaccard, fuzzy_clean_record, levenshtein, load_config, matching_view, partial_ratio,
    ratio, read_fuzzy_input, resolve_overlaps, token_set_ratio, token_sort_ratio, validate_config,
    FuzzyDecision,
};
use corpus_preprocessing_core::phase2::{pipeline, pipeline_with_fuzzy};
use corpus_preprocessing_core::{RecordType, RecordV1};
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;
use std::process::Command;

fn root() -> PathBuf { PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf() }

fn record(id: &str, text: &str) -> RecordV1 {
    RecordV1 {
        record_id: id.to_owned(), dataset_id: "category_a".to_owned(), source_file: "test.jsonl".to_owned(),
        source_offset: 0, record_type: RecordType::Article, event_date: Some("2026-08-01".to_owned()),
        title: None, raw_text: text.to_owned(), clean_text: Some(text.to_owned()), metadata: Map::new(),
        parser_version: "test-v1".to_owned(), schema_version: "record-v1".to_owned(),
    }
}

fn decision(id: &str, label: &str, score: u32, start: usize, end: usize) -> FuzzyDecision {
    FuzzyDecision {
        combined_score: score, components: BTreeMap::new(), decision: label.to_owned(), evidence_count: 0,
        gates: BTreeMap::new(), length_ratio: 0, margin: 0, matched_span: "x".repeat(end - start),
        position: "line".to_owned(), reason_codes: Vec::new(), record_id: "x".to_owned(), span_end: end,
        span_start: start, template_id: id.to_owned(),
    }
}

#[test]
fn levenshtein_and_component_scores() {
    assert_eq!(levenshtein("", ""), 0);
    assert_eq!(levenshtein("中文", "中闻"), 1);
    assert_eq!(levenshtein("公告", "公X告"), 1);
    assert_eq!(ratio("abc", "axc"), 6667);
    assert_eq!(partial_ratio("公告", "平台公告内容"), 10000);
    assert_eq!(token_sort_ratio("News SOURCE", "source news"), 10000);
    assert_eq!(token_set_ratio("source news", "news source extra"), 10000);
    assert_eq!(token_set_ratio("", ""), 10000);
    assert_eq!(char_jaccard("abc", "abc", 3), 10000);
    assert_eq!(char_jaccard("abc", "abd", 3), 0);
    assert_eq!(char_jaccard("", "", 3), 10000);
}

#[test]
fn nfkc_matching_view_is_shared_with_templates() {
    let (config, _) = load_config(&root().join("configs/fuzzy-cleaning-v1.json")).unwrap();
    assert_eq!(matching_view("ＳＯＵＲＣＥ： ＮＥＷＳ", &config), "source:news");
    assert!(ratio("平台公告：内容仅供参考", "平台公告:内容仅供参考") < 10000);
}

#[test]
fn invalid_configs_have_stable_codes() {
    let (config, _) = load_config(&root().join("configs/fuzzy-cleaning-v1.json")).unwrap();
    let mut weights = config.clone();
    *weights.score_weights.get_mut("ratio").unwrap() = 24;
    assert_eq!(validate_config(&weights).unwrap_err().code, "invalid_fuzzy_weights");
    let mut thresholds = config.clone();
    thresholds.thresholds.review = thresholds.thresholds.auto;
    assert_eq!(validate_config(&thresholds).unwrap_err().code, "invalid_fuzzy_thresholds");
    let mut regex = config;
    regex.templates[0].compatible_regex = Some(r"^(公告)\1$".to_owned());
    assert_eq!(validate_config(&regex).unwrap_err().code, "invalid_fuzzy_regex");
}

#[test]
fn golden_inventory_decisions_and_cleaned_text() {
    let (config, _) = load_config(&root().join("configs/fuzzy-cleaning-v1.json")).unwrap();
    let (records, values) = read_fuzzy_input(&root().join("fixtures/golden/fuzzy-cleaning-v1.jsonl")).unwrap();
    assert_eq!(records.len(), 72);
    assert_eq!(values.iter().filter(|value| value["split"] == "calibration").count(), 36);
    assert_eq!(values.iter().filter(|value| value["split"] == "holdout").count(), 36);
    for (record, value) in records.iter().zip(&values) {
        let (cleaned, events, decisions) = fuzzy_clean_record(record, &config).unwrap();
        let actual = decisions.iter().map(|item| item.decision.as_str()).max_by_key(|label| match *label { "applied" => 3, "review" => 2, _ => 1 }).unwrap_or("skipped");
        if value["expected_decision"] == "applied" { assert_eq!(actual, "applied", "{}", value["case_id"]); }
        else { assert_ne!(actual, "applied", "{}", value["case_id"]); }
        assert_eq!(cleaned.clean_text.as_deref(), value["expected_cleaned_text"].as_str(), "{}", value["case_id"]);
        assert_eq!(cleaned.raw_text, value["input_text"], "{}", value["case_id"]);
        if actual != "applied" { assert!(events.iter().all(|event| event.before_hash == event.after_hash && event.removed_chars == 0)); }
    }
}

#[test]
fn applied_review_skipped_and_idempotent_text() {
    let (config, _) = load_config(&root().join("configs/fuzzy-cleaning-v1.json")).unwrap();
    let applied_input = record("applied", "平台公告:内容仅供参考\n真实正文");
    let (applied, events, _) = fuzzy_clean_record(&applied_input, &config).unwrap();
    assert_eq!(applied.clean_text.as_deref(), Some("真实正文"));
    assert!(events.iter().any(|event| event.decision == "applied" && event.removed_chars > 0));
    let review_input = record("review", "平台公吿:内容仅供参考\n真实正文");
    let (review, events, _) = fuzzy_clean_record(&review_input, &config).unwrap();
    assert_eq!(review.clean_text, review_input.clean_text);
    assert!(events.iter().any(|event| event.decision == "review"));
    let skipped_input = record("skip", "本文讨论平台公告:内容仅供参考\n真实正文");
    let (skipped, events, _) = fuzzy_clean_record(&skipped_input, &config).unwrap();
    assert_eq!(skipped.clean_text, skipped_input.clean_text);
    assert!(events.iter().any(|event| event.decision == "skipped"));
    let (repeated, _, _) = fuzzy_clean_record(&applied, &config).unwrap();
    assert_eq!(repeated.clean_text, applied.clean_text);
    assert_eq!(repeated.raw_text, applied.raw_text);
}

#[test]
fn boundary_length_margin_and_overlap_rules() {
    let (config, _) = load_config(&root().join("configs/fuzzy-cleaning-v1.json")).unwrap();
    for (text, expected) in [
        ("平台公告:内容仅供参考\n正文", "正文"),
        ("正文\n免责声明:本文仅供学习交流", "正文"),
        ("正文一\n相关推荐\n正文二", "正文一\n正文二"),
    ] {
        assert_eq!(fuzzy_clean_record(&record("x", text), &config).unwrap().0.clean_text.as_deref(), Some(expected));
    }
    assert!(fuzzy_clean_record(&record("middle", "正文提到平台公告:内容仅供参考然后继续"), &config).unwrap().2.is_empty());
    let long = format!("平台公告:{}\n正文", "很长".repeat(100));
    assert!(fuzzy_clean_record(&record("long", &long), &config).unwrap().2.iter().all(|item| item.matched_span.chars().count() <= 64));
    let selected = resolve_overlaps(vec![decision("b", "review", 9500, 0, 5), decision("a", "applied", 9000, 2, 7)]);
    assert_eq!(selected.len(), 1);
    assert_eq!(selected[0].template_id, "a");
}

#[test]
fn fuzzy_cli_success_and_invalid_regex_failure() {
    let temp = std::env::temp_dir().join(format!("cpc-phase3-cli-{}", std::process::id()));
    let _ = fs::remove_dir_all(&temp);
    let status = Command::new(env!("CARGO_BIN_EXE_corpus-preprocessing-core"))
        .args(["fuzzy-clean", "--input"]).arg(root().join("fixtures/golden/fuzzy-cleaning-v1.jsonl"))
        .args(["--config"]).arg(root().join("configs/fuzzy-cleaning-v1.json"))
        .args(["--output-dir"]).arg(&temp).status().unwrap();
    assert!(status.success());
    assert_eq!(fs::read_to_string(temp.join("cleaned-records.jsonl")).unwrap().lines().count(), 72);
    let mut config: Value = serde_json::from_str(&fs::read_to_string(root().join("configs/fuzzy-cleaning-v1.json")).unwrap()).unwrap();
    config["templates"][0]["compatible_regex"] = Value::String(r"^(公告)\1$".to_owned());
    let bad = temp.join("bad.json");
    fs::write(&bad, serde_json::to_string(&config).unwrap()).unwrap();
    let failure = Command::new(env!("CARGO_BIN_EXE_corpus-preprocessing-core"))
        .args(["fuzzy-clean", "--input"]).arg(root().join("fixtures/golden/fuzzy-cleaning-v1.jsonl"))
        .args(["--config"]).arg(&bad).args(["--output-dir"]).arg(temp.join("bad-output")).output().unwrap();
    assert!(!failure.status.success());
    assert!(String::from_utf8(failure.stderr).unwrap().contains("invalid_fuzzy_regex"));
    fs::remove_dir_all(&temp).unwrap();
}

#[test]
fn pipeline_off_is_compatible_and_on_has_fuzzy_outputs() {
    let temp = std::env::temp_dir().join(format!("cpc-phase3-pipeline-{}", std::process::id()));
    let _ = fs::remove_dir_all(&temp);
    let raw = root().join("fixtures/generated/raw");
    let parsing = root().join("configs/parsing-v1.json");
    let cleaning = root().join("configs/cleaning-v1.json");
    let plan = root().join("configs/sample-multifield-v1.json");
    let off = pipeline(&raw, &parsing, &cleaning, &plan, &temp.join("off")).unwrap();
    let explicit_off = pipeline_with_fuzzy(&raw, &parsing, &cleaning, &plan, &temp.join("explicit-off"), None).unwrap();
    assert_eq!(off, explicit_off);
    let on_dir = temp.join("on");
    let on = pipeline_with_fuzzy(&raw, &parsing, &cleaning, &plan, &on_dir, Some(&root().join("configs/fuzzy-cleaning-v1.json"))).unwrap();
    let repeated_dir = temp.join("repeated");
    let repeated = pipeline_with_fuzzy(&raw, &parsing, &cleaning, &plan, &repeated_dir, Some(&root().join("configs/fuzzy-cleaning-v1.json"))).unwrap();
    assert_eq!(on, repeated);
    assert_eq!(on["algorithm_version"], "pipeline-v1+fuzzy-template-clean-v1");
    for name in ["fuzzy-cleaned-records.jsonl", "fuzzy-cleaning-events.jsonl", "fuzzy-decisions.jsonl", "fuzzy-review-queue.jsonl", "fuzzy-decision-report.json"] {
        assert!(on_dir.join(name).is_file());
        assert_eq!(fs::read(on_dir.join(name)).unwrap(), fs::read(repeated_dir.join(name)).unwrap());
    }
    fs::remove_dir_all(&temp).unwrap();
}
