use crate::phase2::{
    load_json, semantic_fingerprint, sha256_text, write_json, write_jsonl, CleaningEventV1,
    ProcessingError,
};
use crate::{RecordType, RecordV1};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;
use unicode_normalization::UnicodeNormalization;

const SCORE_KEYS: [&str; 5] = ["ratio", "partial_ratio", "token_sort", "token_set", "char_jaccard"];
const POSITIONS: [&str; 3] = ["prefix", "suffix", "line"];
const FORBIDDEN_REGEX: [&str; 8] = ["(?=", "(?!", "(?<=", "(?<!", "\\1", "\\2", "\\k<", "(?P="];

#[derive(Debug, Clone, Deserialize)]
pub struct FuzzyConfig {
    pub algorithm_version: String,
    pub auto_minimum_evidence: usize,
    pub best_match_margin: u32,
    pub candidate_limits: CandidateLimits,
    pub character_ngram: usize,
    pub matching_normalization: MatchingNormalization,
    pub minimum_evidence_floor: u32,
    pub protected_context_prefixes: Vec<String>,
    pub rule_version: String,
    pub schema_version: String,
    pub score_weights: BTreeMap<String, u32>,
    pub templates: Vec<Template>,
    pub thresholds: Thresholds,
}

#[derive(Debug, Clone, Deserialize)]
pub struct CandidateLimits {
    pub max_length: usize,
    pub min_length: usize,
    pub min_length_ratio: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MatchingNormalization {
    pub ascii_lower: bool,
    pub collapse_whitespace: bool,
    pub nfkc: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Template {
    pub allowed_positions: Vec<String>,
    pub canonical_text: String,
    pub compatible_regex: Option<String>,
    pub template_id: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Thresholds {
    pub auto: u32,
    pub review: u32,
}

#[derive(Debug, Clone)]
struct Candidate {
    text: String,
    start: usize,
    end: usize,
    position: String,
    regex_templates: BTreeSet<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct FuzzyDecision {
    pub combined_score: u32,
    pub components: BTreeMap<String, u32>,
    pub decision: String,
    pub evidence_count: usize,
    pub gates: BTreeMap<String, bool>,
    pub length_ratio: u32,
    pub margin: u32,
    pub matched_span: String,
    pub position: String,
    pub reason_codes: Vec<String>,
    pub record_id: String,
    pub span_end: usize,
    pub span_start: usize,
    pub template_id: String,
}

fn rounded_basis(numerator: usize, denominator: usize) -> u32 {
    if denominator == 0 { 10000 } else { ((numerator * 10000 + denominator / 2) / denominator) as u32 }
}

pub fn load_config(path: &Path) -> Result<(FuzzyConfig, Value), ProcessingError> {
    let value = load_json(path)?;
    let config: FuzzyConfig = serde_json::from_value(value.clone())
        .map_err(|error| ProcessingError::new("invalid_fuzzy_config", error.to_string()))?;
    validate_config(&config)?;
    Ok((config, value))
}

pub fn validate_config(config: &FuzzyConfig) -> Result<(), ProcessingError> {
    if config.schema_version != "fuzzy-cleaning-config-v1" {
        return Err(ProcessingError::new("invalid_fuzzy_config", "schema_version must be fuzzy-cleaning-config-v1"));
    }
    if config.score_weights.len() != SCORE_KEYS.len()
        || SCORE_KEYS.iter().any(|key| !config.score_weights.contains_key(*key))
        || config.score_weights.values().sum::<u32>() != 100
    {
        return Err(ProcessingError::new("invalid_fuzzy_weights", "score weights must be non-negative integers summing to 100"));
    }
    if config.thresholds.review >= config.thresholds.auto || config.thresholds.auto > 10000 {
        return Err(ProcessingError::new("invalid_fuzzy_thresholds", "review threshold must be lower than auto threshold"));
    }
    if config.candidate_limits.min_length == 0
        || config.candidate_limits.min_length > config.candidate_limits.max_length
        || config.candidate_limits.max_length > 256
        || config.candidate_limits.min_length_ratio > 10000
    {
        return Err(ProcessingError::new("invalid_fuzzy_limits", "candidate length limits are invalid"));
    }
    if config.character_ngram == 0 || config.templates.is_empty() {
        return Err(ProcessingError::new("invalid_fuzzy_config", "character_ngram and templates must be non-empty"));
    }
    let mut identifiers = BTreeSet::new();
    for template in &config.templates {
        if template.template_id.is_empty() || template.canonical_text.is_empty() || template.canonical_text.chars().count() > 256
            || template.allowed_positions.is_empty()
            || template.allowed_positions.iter().any(|position| !POSITIONS.contains(&position.as_str()))
            || !identifiers.insert(template.template_id.clone())
        {
            return Err(ProcessingError::new("invalid_fuzzy_config", "template id/text/positions are invalid"));
        }
        if let Some(pattern) = &template.compatible_regex {
            if !(pattern.starts_with('^') || pattern.ends_with('$'))
                || FORBIDDEN_REGEX.iter().any(|token| pattern.contains(token))
                || Regex::new(pattern).is_err()
            {
                return Err(ProcessingError::new("invalid_fuzzy_regex", format!("unsupported regex for template {}", template.template_id)));
            }
        }
    }
    Ok(())
}

fn compact_delimiters(text: &str) -> String {
    let mut output = String::new();
    let mut skip_space = false;
    for character in text.chars() {
        if matches!(character, ':' | '|') {
            while output.ends_with(' ') { output.pop(); }
            output.push(character);
            skip_space = true;
        } else if skip_space && character == ' ' {
            continue;
        } else {
            skip_space = false;
            output.push(character);
        }
    }
    output
}

pub fn matching_view(text: &str, config: &FuzzyConfig) -> String {
    let mut value = if config.matching_normalization.nfkc { text.nfkc().collect() } else { text.to_owned() };
    if config.matching_normalization.ascii_lower {
        value = value.chars().map(|character| if character.is_ascii() { character.to_ascii_lowercase() } else { character }).collect();
    }
    if config.matching_normalization.collapse_whitespace {
        value = compact_delimiters(&value.split_whitespace().collect::<Vec<_>>().join(" "));
    }
    value
}

pub fn levenshtein(left: &str, right: &str) -> usize {
    // ponytail: two-row O(mn) DP is enough for <=256-char templates; upgrade only after a benchmark proves otherwise.
    let (mut left, mut right): (Vec<char>, Vec<char>) = (left.chars().collect(), right.chars().collect());
    if left.len() > right.len() { std::mem::swap(&mut left, &mut right); }
    let mut previous: Vec<usize> = (0..=left.len()).collect();
    for (row, right_char) in right.iter().enumerate() {
        let mut current = vec![row + 1];
        for (column, left_char) in left.iter().enumerate() {
            current.push((current[column] + 1).min(previous[column + 1] + 1).min(previous[column] + usize::from(left_char != right_char)));
        }
        previous = current;
    }
    previous[left.len()]
}

pub fn ratio(left: &str, right: &str) -> u32 {
    let maximum = left.chars().count().max(right.chars().count());
    if maximum == 0 { 10000 } else { rounded_basis(maximum - levenshtein(left, right), maximum) }
}

pub fn partial_ratio(left: &str, right: &str) -> u32 {
    let (mut short, mut long): (Vec<char>, Vec<char>) = (left.chars().collect(), right.chars().collect());
    if short.len() > long.len() { std::mem::swap(&mut short, &mut long); }
    if short.is_empty() { return if long.is_empty() { 10000 } else { 0 }; }
    (0..=long.len() - short.len()).map(|start| {
        ratio(&short.iter().collect::<String>(), &long[start..start + short.len()].iter().collect::<String>())
    }).max().unwrap_or(0)
}

pub fn tokenize(text: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut ascii = String::new();
    for character in text.chars() {
        if character.is_ascii_alphanumeric() {
            ascii.push(character.to_ascii_lowercase());
        } else {
            if !ascii.is_empty() { tokens.push(std::mem::take(&mut ascii)); }
            if ('\u{4e00}'..='\u{9fff}').contains(&character) { tokens.push(character.to_string()); }
        }
    }
    if !ascii.is_empty() { tokens.push(ascii); }
    tokens
}

pub fn token_sort_ratio(left: &str, right: &str) -> u32 {
    let mut left_tokens = tokenize(left);
    let mut right_tokens = tokenize(right);
    left_tokens.sort();
    right_tokens.sort();
    ratio(&left_tokens.join(" "), &right_tokens.join(" "))
}

pub fn token_set_ratio(left: &str, right: &str) -> u32 {
    let left_set: BTreeSet<_> = tokenize(left).into_iter().collect();
    let right_set: BTreeSet<_> = tokenize(right).into_iter().collect();
    let intersection: Vec<_> = left_set.intersection(&right_set).cloned().collect();
    let left_only: Vec<_> = left_set.difference(&right_set).cloned().collect();
    let right_only: Vec<_> = right_set.difference(&left_set).cloned().collect();
    let base = intersection.join(" ");
    let mut left_variant = intersection.clone();
    left_variant.extend(left_only);
    let mut right_variant = intersection;
    right_variant.extend(right_only);
    let left_value = left_variant.join(" ");
    let right_value = right_variant.join(" ");
    ratio(&base, &left_value).max(ratio(&base, &right_value)).max(ratio(&left_value, &right_value))
}

fn ngrams(text: &str, size: usize) -> BTreeSet<String> {
    let characters: Vec<_> = text.chars().collect();
    if characters.is_empty() { return BTreeSet::new(); }
    if characters.len() < size { return BTreeSet::from([text.to_owned()]); }
    (0..=characters.len() - size).map(|index| characters[index..index + size].iter().collect()).collect()
}

pub fn char_jaccard(left: &str, right: &str, size: usize) -> u32 {
    let left_grams = ngrams(left, size);
    let right_grams = ngrams(right, size);
    let union = left_grams.union(&right_grams).count();
    if union == 0 { 10000 } else { rounded_basis(left_grams.intersection(&right_grams).count(), union) }
}

pub fn score_pair(candidate: &str, template: &str, config: &FuzzyConfig) -> BTreeMap<String, u32> {
    let candidate = matching_view(candidate, config);
    let template = matching_view(template, config);
    let mut scores = BTreeMap::from([
        ("ratio".to_owned(), ratio(&candidate, &template)),
        ("partial_ratio".to_owned(), partial_ratio(&candidate, &template)),
        ("token_sort".to_owned(), token_sort_ratio(&candidate, &template)),
        ("token_set".to_owned(), token_set_ratio(&candidate, &template)),
        ("char_jaccard".to_owned(), char_jaccard(&candidate, &template, config.character_ngram)),
    ]);
    let weighted: u32 = SCORE_KEYS.iter().map(|key| scores[*key] * config.score_weights[*key]).sum();
    scores.insert("combined".to_owned(), (weighted + 50) / 100);
    scores
}

fn line_regions(text: &str, config: &FuzzyConfig) -> Result<Vec<Candidate>, ProcessingError> {
    let mut lines = Vec::new();
    let mut offset = 0;
    for raw_line in text.split_inclusive('\n') {
        let line = raw_line.trim_end_matches(['\r', '\n']);
        let leading = line.chars().take_while(|character| character.is_whitespace()).count();
        let stripped = line.trim();
        if !stripped.is_empty() {
            let start = offset + leading;
            lines.push((start, start + stripped.chars().count(), stripped.to_owned()));
        }
        offset += raw_line.chars().count();
    }
    let mut regions: BTreeMap<(usize, usize, String), Candidate> = BTreeMap::new();
    for (index, (original_start, original_end, original_value)) in lines.iter().enumerate() {
        if lines.len() == 1 { continue; }
        let position = if index == 0 { "prefix" } else if index + 1 == lines.len() { "suffix" } else { "line" };
        let (mut start, mut end, mut value) = (*original_start, *original_end, original_value.clone());
        if value.chars().count() > config.candidate_limits.max_length {
            let characters: Vec<_> = value.chars().collect();
            if position == "suffix" {
                start = end - config.candidate_limits.max_length;
                value = characters[characters.len() - config.candidate_limits.max_length..].iter().collect();
            } else {
                end = start + config.candidate_limits.max_length;
                value = characters[..config.candidate_limits.max_length].iter().collect();
            }
        }
        regions.insert((start, end, position.to_owned()), Candidate {
            text: value, start, end, position: position.to_owned(), regex_templates: BTreeSet::new(),
        });
    }
    for (index, (start, end, value)) in lines.iter().enumerate() {
        let position = if index == 0 { "prefix" } else if index + 1 == lines.len() { "suffix" } else { "line" };
        for template in &config.templates {
            if let Some(pattern) = &template.compatible_regex {
                let regex = Regex::new(pattern).map_err(|_| ProcessingError::new("invalid_fuzzy_regex", format!("invalid regex for template {}", template.template_id)))?;
                if regex.find(value).is_some_and(|matched| matched.start() == 0 && matched.end() == value.len()) {
                    let key = (*start, *end, position.to_owned());
                    let region = regions.entry(key).or_insert_with(|| Candidate {
                        text: value.clone(), start: *start, end: *end, position: position.to_owned(), regex_templates: BTreeSet::new(),
                    });
                    region.regex_templates.insert(template.template_id.clone());
                }
            }
        }
    }
    Ok(regions.into_values().collect())
}

fn best_for_region(region: &Candidate, config: &FuzzyConfig) -> FuzzyDecision {
    let mut scored: Vec<_> = config.templates.iter().map(|template| {
        let components = score_pair(&region.text, &template.canonical_text, config);
        (components["combined"], template, components)
    }).collect();
    scored.sort_by(|left, right| right.0.cmp(&left.0).then_with(|| left.1.template_id.cmp(&right.1.template_id)));
    let (best_score, template, components) = (&scored[0].0, scored[0].1, scored[0].2.clone());
    let second_score = scored.get(1).map_or(0, |item| item.0);
    let candidate_view = matching_view(&region.text, config);
    let template_view = matching_view(&template.canonical_text, config);
    let candidate_length = candidate_view.chars().count();
    let template_length = template_view.chars().count();
    let length_ratio = rounded_basis(candidate_length.min(template_length), candidate_length.max(template_length));
    let evidence_count = SCORE_KEYS.iter().filter(|key| components[**key] >= config.minimum_evidence_floor).count();
    let protected = config.protected_context_prefixes.iter().any(|prefix| candidate_view.starts_with(&matching_view(prefix, config)));
    let margin = best_score.saturating_sub(second_score);
    let gates = BTreeMap::from([
        ("boundary".to_owned(), POSITIONS.contains(&region.position.as_str())),
        ("evidence".to_owned(), evidence_count >= config.auto_minimum_evidence),
        ("length".to_owned(), length_ratio >= config.candidate_limits.min_length_ratio),
        ("margin".to_owned(), margin >= config.best_match_margin),
        ("minimum_length".to_owned(), candidate_length >= config.candidate_limits.min_length),
        ("position".to_owned(), template.allowed_positions.contains(&region.position)),
        ("protected".to_owned(), protected),
        ("regex".to_owned(), region.regex_templates.contains(&template.template_id)),
    ]);
    let mut reasons = Vec::new();
    if protected { reasons.push("protected_context"); }
    if !gates["minimum_length"] { reasons.push("candidate_too_short"); }
    if !gates["length"] { reasons.push("length_ratio"); }
    if !gates["position"] { reasons.push("position_not_allowed"); }
    if !gates["evidence"] { reasons.push("insufficient_evidence"); }
    if !gates["margin"] { reasons.push("ambiguous_best_match"); }
    let protective_failure = protected || !gates["minimum_length"] || !gates["length"] || !gates["position"];
    let decision = if *best_score >= config.thresholds.auto && !protective_failure && gates["evidence"] && gates["margin"] {
        "applied"
    } else if *best_score >= config.thresholds.review && !protective_failure {
        "review"
    } else {
        "skipped"
    };
    if *best_score < config.thresholds.review { reasons.push("below_review_threshold"); }
    reasons.sort();
    reasons.dedup();
    FuzzyDecision {
        combined_score: *best_score, components, decision: decision.to_owned(), evidence_count, gates,
        length_ratio, margin, matched_span: region.text.clone(), position: region.position.clone(),
        reason_codes: reasons.into_iter().map(str::to_owned).collect(), record_id: String::new(),
        span_end: region.end, span_start: region.start, template_id: template.template_id.clone(),
    }
}

fn decision_rank(decision: &str) -> u8 {
    match decision { "applied" => 3, "review" => 2, _ => 1 }
}

pub fn resolve_overlaps(mut matches: Vec<FuzzyDecision>) -> Vec<FuzzyDecision> {
    matches.sort_by(|left, right| {
        decision_rank(&right.decision).cmp(&decision_rank(&left.decision))
            .then_with(|| right.combined_score.cmp(&left.combined_score))
            .then_with(|| (right.span_end - right.span_start).cmp(&(left.span_end - left.span_start)))
            .then_with(|| left.template_id.cmp(&right.template_id))
            .then_with(|| left.span_start.cmp(&right.span_start))
    });
    let mut selected: Vec<FuzzyDecision> = Vec::new();
    for item in matches {
        if !selected.iter().any(|other| item.span_start < other.span_end && other.span_start < item.span_end) {
            selected.push(item);
        }
    }
    selected.sort_by_key(|item| (item.span_start, item.span_end));
    selected
}

fn delete_span(text: &str, start: usize, end: usize) -> String {
    let mut characters: Vec<_> = text.chars().collect();
    let (mut start, mut end) = (start, end);
    if end < characters.len() && characters[end] == '\n' { end += 1; }
    else if start > 0 && characters[start - 1] == '\n' { start -= 1; }
    characters.drain(start..end);
    characters.into_iter().collect()
}

fn fuzzy_event(item: &FuzzyDecision, config: &FuzzyConfig, before: &str, after: &str) -> CleaningEventV1 {
    let mut metrics = Map::new();
    for (key, value) in &item.components { metrics.insert(key.clone(), json!(value)); }
    metrics.insert("auto_threshold".to_owned(), json!(config.thresholds.auto));
    metrics.insert("best_match_margin".to_owned(), json!(config.best_match_margin));
    metrics.insert("evidence_count".to_owned(), json!(item.evidence_count));
    metrics.insert("gates".to_owned(), json!(item.gates));
    metrics.insert("length_ratio".to_owned(), json!(item.length_ratio));
    metrics.insert("margin".to_owned(), json!(item.margin));
    metrics.insert("position".to_owned(), json!(item.position));
    metrics.insert("reason_codes".to_owned(), json!(item.reason_codes));
    metrics.insert("review_threshold".to_owned(), json!(config.thresholds.review));
    metrics.insert("span_end".to_owned(), json!(item.span_end));
    metrics.insert("span_start".to_owned(), json!(item.span_start));
    CleaningEventV1 {
        record_id: item.record_id.clone(), stage: "fuzzy_template_cleaning".to_owned(),
        rule_id: format!("fuzzy:{}", item.template_id), rule_version: config.rule_version.clone(),
        match_method: "fuzzy".to_owned(), action: "remove".to_owned(),
        reason_code: match item.decision.as_str() {
            "applied" => "fuzzy_template_auto", "review" => "fuzzy_template_review", _ => "fuzzy_template_skipped",
        }.to_owned(),
        decision: item.decision.clone(), matched_span: Some(item.matched_span.clone()),
        removed_chars: if item.decision == "applied" { before.chars().count().saturating_sub(after.chars().count()) } else { 0 },
        score: item.combined_score as f64 / 10000.0,
        before_hash: sha256_text(before), after_hash: sha256_text(after), metrics: Value::Object(metrics),
        algorithm_version: config.algorithm_version.clone(), schema_version: "cleaning-event-v1".to_owned(),
    }
}

pub fn fuzzy_clean_record(record: &RecordV1, config: &FuzzyConfig) -> Result<(RecordV1, Vec<CleaningEventV1>, Vec<FuzzyDecision>), ProcessingError> {
    validate_config(config)?;
    let source = record.clean_text.as_deref().unwrap_or(&record.raw_text);
    let mut matches = resolve_overlaps(line_regions(source, config)?.iter().map(|region| best_for_region(region, config)).collect());
    for item in &mut matches { item.record_id = record.record_id.clone(); }
    let mut text = source.to_owned();
    let mut events = Vec::new();
    let mut applied: Vec<_> = matches.iter().filter(|item| item.decision == "applied").collect();
    applied.sort_by(|left, right| right.span_start.cmp(&left.span_start));
    for item in applied {
        let before = text;
        text = delete_span(&before, item.span_start, item.span_end);
        events.push(fuzzy_event(item, config, &before, &text));
    }
    for item in &matches {
        if item.decision != "applied" { events.push(fuzzy_event(item, config, &text, &text)); }
    }
    let mut cleaned = record.clone();
    cleaned.clean_text = Some(text);
    Ok((cleaned, events, matches))
}

pub fn fuzzy_clean_records(records: &[RecordV1], config: &FuzzyConfig) -> Result<(Vec<RecordV1>, Vec<CleaningEventV1>, Vec<FuzzyDecision>), ProcessingError> {
    validate_config(config)?;
    let (mut cleaned, mut events, mut decisions) = (Vec::new(), Vec::new(), Vec::new());
    for record in records {
        let (result, mut record_events, mut record_decisions) = fuzzy_clean_record(record, config)?;
        cleaned.push(result);
        events.append(&mut record_events);
        decisions.append(&mut record_decisions);
    }
    Ok((cleaned, events, decisions))
}

pub fn read_fuzzy_input(path: &Path) -> Result<(Vec<RecordV1>, Vec<Value>), ProcessingError> {
    let data = fs::read_to_string(path).map_err(|error| ProcessingError::new("io_error", error.to_string()))?;
    let mut records = Vec::new();
    let mut values = Vec::new();
    for (index, line) in data.lines().enumerate() {
        let value: Value = serde_json::from_str(line)
            .map_err(|_| ProcessingError::new("invalid_fuzzy_input", format!("line {}: invalid JSON", index + 1)))?;
        let object = value.as_object().ok_or_else(|| ProcessingError::new("invalid_fuzzy_input", format!("line {}: expected object", index + 1)))?;
        let record = if object.get("schema_version").and_then(Value::as_str) == Some("record-v1") {
            serde_json::from_value(value.clone()).map_err(|error| ProcessingError::new("invalid_fuzzy_input", format!("line {}: {error}", index + 1)))?
        } else {
            for field in ["case_id", "record_type", "event_date", "input_text", "case_family", "split"] {
                if !object.contains_key(field) { return Err(ProcessingError::new("invalid_fuzzy_input", format!("line {}: missing Golden case fields", index + 1))); }
            }
            let record_type: RecordType = serde_json::from_value(object["record_type"].clone())
                .map_err(|error| ProcessingError::new("invalid_fuzzy_input", error.to_string()))?;
            RecordV1 {
                record_id: object["case_id"].as_str().unwrap_or_default().to_owned(), dataset_id: "fuzzy_golden".to_owned(),
                source_file: "fuzzy-cleaning-v1.jsonl".to_owned(), source_offset: index as u64, record_type,
                event_date: object["event_date"].as_str().map(str::to_owned), title: None,
                raw_text: object["input_text"].as_str().unwrap_or_default().to_owned(),
                clean_text: object["input_text"].as_str().map(str::to_owned),
                metadata: Map::from_iter([
                    ("case_family".to_owned(), object["case_family"].clone()),
                    ("golden_split".to_owned(), object["split"].clone()),
                ]),
                parser_version: "fuzzy-golden-v1".to_owned(), schema_version: "record-v1".to_owned(),
            }
        };
        values.push(value);
        records.push(record);
    }
    Ok((records, values))
}

pub fn fuzzy_clean_file(input_path: &Path, config_path: &Path, output_dir: &Path) -> Result<Value, ProcessingError> {
    let (config, _) = load_config(config_path)?;
    let (records, _) = read_fuzzy_input(input_path)?;
    let (cleaned, events, decisions) = fuzzy_clean_records(&records, &config)?;
    fs::create_dir_all(output_dir).map_err(|error| ProcessingError::new("io_error", error.to_string()))?;
    let decision_values: Vec<_> = decisions.iter().map(|item| serde_json::to_value(item).expect("decision serializes")).collect();
    let review_values: Vec<_> = decision_values.iter().zip(&decisions).filter(|(_, item)| item.decision == "review").map(|(value, _)| value.clone()).collect();
    let mut counts = BTreeMap::<String, usize>::new();
    for item in &decisions { *counts.entry(item.decision.clone()).or_default() += 1; }
    let report = json!({
        "algorithm_version": config.algorithm_version,
        "counts": counts,
        "decision_fingerprint": semantic_fingerprint(&decision_values),
        "record_count": records.len(),
        "schema_version": "fuzzy-decision-report-v1",
        "thresholds": config.thresholds,
    });
    write_jsonl(&output_dir.join("cleaned-records.jsonl"), &cleaned.iter().map(|item| serde_json::to_value(item).unwrap()).collect::<Vec<_>>())?;
    write_jsonl(&output_dir.join("cleaning-events.jsonl"), &events.iter().map(|item| serde_json::to_value(item).unwrap()).collect::<Vec<_>>())?;
    write_jsonl(&output_dir.join("decisions.jsonl"), &decision_values)?;
    write_jsonl(&output_dir.join("review-queue.jsonl"), &review_values)?;
    write_json(&output_dir.join("decision-report.json"), &report)?;
    Ok(report)
}
