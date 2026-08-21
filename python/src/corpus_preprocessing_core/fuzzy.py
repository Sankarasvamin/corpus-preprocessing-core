from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .core import RecordV1
from .processing import (
    CleaningEventV1,
    ProcessingError,
    canonical_json,
    semantic_fingerprint,
    sha256_text,
    write_json,
    write_jsonl,
)


SCORE_KEYS = ("ratio", "partial_ratio", "token_sort", "token_set", "char_jaccard")
POSITIONS = {"prefix", "suffix", "line"}
FORBIDDEN_REGEX = ("(?=", "(?!", "(?<=", "(?<!", "\\1", "\\2", "\\k<", "(?P=")


@dataclass(frozen=True)
class Candidate:
    text: str
    start: int
    end: int
    position: str
    regex_templates: tuple[str, ...] = ()


def _rounded_basis(numerator: int, denominator: int) -> int:
    return 10000 if denominator == 0 else (numerator * 10000 + denominator // 2) // denominator


def matching_view(text: str, config: dict[str, Any]) -> str:
    rules = config["matching_normalization"]
    if rules["nfkc"]:
        text = unicodedata.normalize("NFKC", text)
    if rules["ascii_lower"]:
        text = "".join(char.lower() if char.isascii() else char for char in text)
    if rules["collapse_whitespace"]:
        text = " ".join(text.split())
        text = re.sub(r" *([:|]) *", r"\1", text)
    return text


def levenshtein(left: str, right: str) -> int:
    # ponytail: two-row O(mn) DP is enough for <=256-char templates; upgrade only after a benchmark proves otherwise.
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for row, right_char in enumerate(right, 1):
        current = [row]
        for column, left_char in enumerate(left, 1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (left_char != right_char)))
        previous = current
    return previous[-1]


def ratio(left: str, right: str) -> int:
    maximum = max(len(left), len(right))
    if maximum == 0:
        return 10000
    return _rounded_basis(maximum - levenshtein(left, right), maximum)


def partial_ratio(left: str, right: str) -> int:
    if len(left) > len(right):
        left, right = right, left
    if not left:
        return 10000 if not right else 0
    return max(ratio(left, right[index:index + len(left)]) for index in range(len(right) - len(left) + 1))


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    ascii_token: list[str] = []

    def flush() -> None:
        if ascii_token:
            tokens.append("".join(ascii_token).lower())
            ascii_token.clear()

    for char in text:
        if char.isascii() and char.isalnum():
            ascii_token.append(char)
        elif "\u4e00" <= char <= "\u9fff":
            flush()
            tokens.append(char)
        else:
            flush()
    flush()
    return tokens


def token_sort_ratio(left: str, right: str) -> int:
    return ratio(" ".join(sorted(tokenize(left))), " ".join(sorted(tokenize(right))))


def token_set_ratio(left: str, right: str) -> int:
    left_set, right_set = set(tokenize(left)), set(tokenize(right))
    intersection = sorted(left_set & right_set)
    left_only, right_only = sorted(left_set - right_set), sorted(right_set - left_set)
    variants = [" ".join(intersection), " ".join(intersection + left_only), " ".join(intersection + right_only)]
    return max(ratio(variants[0], variants[1]), ratio(variants[0], variants[2]), ratio(variants[1], variants[2]))


def ngrams(text: str, size: int) -> set[str]:
    if not text:
        return set()
    return {text} if len(text) < size else {text[index:index + size] for index in range(len(text) - size + 1)}


def char_jaccard(left: str, right: str, size: int = 3) -> int:
    left_grams, right_grams = ngrams(left, size), ngrams(right, size)
    union = left_grams | right_grams
    return 10000 if not union else _rounded_basis(len(left_grams & right_grams), len(union))


def score_pair(candidate: str, template: str, config: dict[str, Any]) -> dict[str, int]:
    candidate_view, template_view = matching_view(candidate, config), matching_view(template, config)
    scores = {
        "ratio": ratio(candidate_view, template_view),
        "partial_ratio": partial_ratio(candidate_view, template_view),
        "token_sort": token_sort_ratio(candidate_view, template_view),
        "token_set": token_set_ratio(candidate_view, template_view),
        "char_jaccard": char_jaccard(candidate_view, template_view, config["character_ngram"]),
    }
    weighted = sum(scores[key] * config["score_weights"][key] for key in SCORE_KEYS)
    scores["combined"] = (weighted + 50) // 100
    return scores


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("schema_version") != "fuzzy-cleaning-config-v1":
        raise ProcessingError("invalid_fuzzy_config", "schema_version must be fuzzy-cleaning-config-v1")
    weights = config.get("score_weights")
    if not isinstance(weights, dict) or set(weights) != set(SCORE_KEYS) or any(type(weights[key]) is not int or weights[key] < 0 for key in SCORE_KEYS) or sum(weights.values()) != 100:
        raise ProcessingError("invalid_fuzzy_weights", "score weights must be non-negative integers summing to 100")
    thresholds = config.get("thresholds", {})
    review, auto = thresholds.get("review"), thresholds.get("auto")
    if type(review) is not int or type(auto) is not int or not 0 <= review < auto <= 10000:
        raise ProcessingError("invalid_fuzzy_thresholds", "review threshold must be lower than auto threshold")
    limits = config.get("candidate_limits", {})
    minimum, maximum, length_ratio = limits.get("min_length"), limits.get("max_length"), limits.get("min_length_ratio")
    if type(minimum) is not int or type(maximum) is not int or not 1 <= minimum <= maximum <= 256 or type(length_ratio) is not int or not 0 <= length_ratio <= 10000:
        raise ProcessingError("invalid_fuzzy_limits", "candidate length limits are invalid")
    if type(config.get("character_ngram")) is not int or config["character_ngram"] < 1:
        raise ProcessingError("invalid_fuzzy_config", "character_ngram must be positive")
    templates = config.get("templates")
    if not isinstance(templates, list) or not templates:
        raise ProcessingError("invalid_fuzzy_config", "templates must be a non-empty list")
    identifiers = []
    for template in templates:
        identifier, canonical = template.get("template_id"), template.get("canonical_text")
        positions, pattern = template.get("allowed_positions"), template.get("compatible_regex")
        if not isinstance(identifier, str) or not identifier or not isinstance(canonical, str) or not canonical or len(canonical) > 256:
            raise ProcessingError("invalid_fuzzy_config", "template id/text is invalid")
        if not isinstance(positions, list) or not positions or not set(positions) <= POSITIONS:
            raise ProcessingError("invalid_fuzzy_config", "template positions are invalid")
        if pattern is not None:
            if not isinstance(pattern, str) or not (pattern.startswith("^") or pattern.endswith("$")) or any(token in pattern for token in FORBIDDEN_REGEX):
                raise ProcessingError("invalid_fuzzy_regex", f"unsupported regex for template {identifier}")
            try:
                re.compile(pattern)
            except re.error:
                raise ProcessingError("invalid_fuzzy_regex", f"invalid regex for template {identifier}") from None
        identifiers.append(identifier)
    if len(identifiers) != len(set(identifiers)):
        raise ProcessingError("invalid_fuzzy_config", "template_id values must be unique")
    return config


def _line_regions(text: str, config: dict[str, Any]) -> list[Candidate]:
    lines: list[tuple[int, int, str]] = []
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        leading = len(line) - len(line.lstrip())
        stripped = line.strip()
        if stripped:
            start = offset + leading
            lines.append((start, start + len(stripped), stripped))
        offset += len(raw_line)
    regions: dict[tuple[int, int, str], Candidate] = {}
    maximum = config["candidate_limits"]["max_length"]
    for index, (start, end, value) in enumerate(lines):
        if len(lines) == 1:
            continue
        position = "prefix" if index == 0 else "suffix" if index == len(lines) - 1 else "line"
        if len(value) > maximum:
            if position == "suffix":
                start, value = end - maximum, value[-maximum:]
            else:
                end, value = start + maximum, value[:maximum]
        regions[(start, end, position)] = Candidate(value, start, end, position)
    for index, (start, end, value) in enumerate(lines):
        position = "prefix" if index == 0 else "suffix" if index == len(lines) - 1 else "line"
        for template in config["templates"]:
            pattern = template.get("compatible_regex")
            if pattern and re.fullmatch(pattern, value):
                key = (start, end, position)
                previous = regions.get(key, Candidate(value, start, end, position))
                regions[key] = replace(previous, regex_templates=tuple(sorted(set(previous.regex_templates) | {template["template_id"]})))
    return sorted(regions.values(), key=lambda item: (item.start, item.end, item.position))


def _rank_decision(decision: str) -> int:
    return {"applied": 3, "review": 2, "skipped": 1}[decision]


def _best_for_region(region: Candidate, config: dict[str, Any]) -> dict[str, Any]:
    scored = []
    candidate_view = matching_view(region.text, config)
    for template in config["templates"]:
        components = score_pair(region.text, template["canonical_text"], config)
        scored.append((components["combined"], template["template_id"], template, components))
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, template_id, template, components = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    template_view = matching_view(template["canonical_text"], config)
    length_ratio = _rounded_basis(min(len(candidate_view), len(template_view)), max(len(candidate_view), len(template_view)))
    evidence = sum(components[key] >= config["minimum_evidence_floor"] for key in SCORE_KEYS)
    protected = any(candidate_view.startswith(matching_view(prefix, config)) for prefix in config["protected_context_prefixes"])
    gates = {
        "boundary": region.position in POSITIONS,
        "evidence": evidence >= config["auto_minimum_evidence"],
        "length": length_ratio >= config["candidate_limits"]["min_length_ratio"],
        "margin": best_score - second_score >= config["best_match_margin"],
        "minimum_length": len(candidate_view) >= config["candidate_limits"]["min_length"],
        "position": region.position in template["allowed_positions"],
        "protected": protected,
        "regex": template_id in region.regex_templates,
    }
    reasons = []
    if protected:
        reasons.append("protected_context")
    if not gates["minimum_length"]:
        reasons.append("candidate_too_short")
    if not gates["length"]:
        reasons.append("length_ratio")
    if not gates["position"]:
        reasons.append("position_not_allowed")
    if not gates["evidence"]:
        reasons.append("insufficient_evidence")
    if not gates["margin"]:
        reasons.append("ambiguous_best_match")
    review, auto = config["thresholds"]["review"], config["thresholds"]["auto"]
    protective_failure = protected or not gates["minimum_length"] or not gates["length"] or not gates["position"]
    if best_score >= auto and not protective_failure and gates["evidence"] and gates["margin"]:
        decision = "applied"
    elif best_score >= review and not protective_failure:
        decision = "review"
    else:
        decision = "skipped"
    if best_score < review:
        reasons.append("below_review_threshold")
    return {
        "combined_score": best_score,
        "components": components,
        "decision": decision,
        "evidence_count": evidence,
        "gates": gates,
        "length_ratio": length_ratio,
        "margin": best_score - second_score,
        "matched_span": region.text,
        "position": region.position,
        "reason_codes": sorted(set(reasons)),
        "record_id": "",
        "span_end": region.end,
        "span_start": region.start,
        "template_id": template_id,
    }


def _resolve_overlaps(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(matches, key=lambda item: (
        -_rank_decision(item["decision"]), -item["combined_score"],
        -(item["span_end"] - item["span_start"]), item["template_id"], item["span_start"],
    ))
    selected = []
    for match in ranked:
        if not any(match["span_start"] < other["span_end"] and other["span_start"] < match["span_end"] for other in selected):
            selected.append(match)
    return sorted(selected, key=lambda item: (item["span_start"], item["span_end"]))


def _delete_span(text: str, start: int, end: int) -> str:
    if end < len(text) and text[end] == "\n":
        end += 1
    elif start > 0 and text[start - 1] == "\n":
        start -= 1
    return text[:start] + text[end:]


def fuzzy_clean_record(record: RecordV1, config: dict[str, Any]) -> tuple[RecordV1, list[CleaningEventV1], list[dict[str, Any]]]:
    validate_config(config)
    source = record.clean_text if record.clean_text is not None else record.raw_text
    matches = _resolve_overlaps([_best_for_region(region, config) for region in _line_regions(source, config)])
    for match in matches:
        match["record_id"] = record.record_id
    text = source
    events: list[CleaningEventV1] = []
    applied = sorted((item for item in matches if item["decision"] == "applied"), key=lambda item: item["span_start"], reverse=True)
    for match in applied:
        before = text
        text = _delete_span(text, match["span_start"], match["span_end"])
        events.append(_fuzzy_event(match, config, before, text))
    for match in matches:
        if match["decision"] != "applied":
            events.append(_fuzzy_event(match, config, text, text))
    return replace(record, clean_text=text), events, matches


def _fuzzy_event(match: dict[str, Any], config: dict[str, Any], before: str, after: str) -> CleaningEventV1:
    metrics = {
        **match["components"],
        "auto_threshold": config["thresholds"]["auto"],
        "best_match_margin": config["best_match_margin"],
        "evidence_count": match["evidence_count"],
        "gates": match["gates"],
        "length_ratio": match["length_ratio"],
        "margin": match["margin"],
        "position": match["position"],
        "reason_codes": match["reason_codes"],
        "review_threshold": config["thresholds"]["review"],
        "span_end": match["span_end"],
        "span_start": match["span_start"],
    }
    decision = match["decision"]
    return CleaningEventV1(
        record_id=match["record_id"], stage="fuzzy_template_cleaning",
        rule_id=f"fuzzy:{match['template_id']}", rule_version=config["rule_version"],
        match_method="fuzzy", action="remove",
        reason_code={"applied": "fuzzy_template_auto", "review": "fuzzy_template_review", "skipped": "fuzzy_template_skipped"}[decision],
        decision=decision, matched_span=match["matched_span"],
        removed_chars=max(0, len(before) - len(after)) if decision == "applied" else 0,
        score=match["combined_score"] / 10000,
        before_hash=sha256_text(before), after_hash=sha256_text(after), metrics=metrics,
        algorithm_version=config["algorithm_version"],
    )


def fuzzy_clean_records(records: list[RecordV1], config: dict[str, Any]) -> tuple[list[RecordV1], list[CleaningEventV1], list[dict[str, Any]]]:
    validate_config(config)
    cleaned, events, decisions = [], [], []
    for record in records:
        result, record_events, record_decisions = fuzzy_clean_record(record, config)
        cleaned.append(result)
        events.extend(record_events)
        decisions.extend(record_decisions)
    return cleaned, events, decisions


def _record_from_value(value: dict[str, Any], index: int) -> RecordV1:
    if value.get("schema_version") == "record-v1":
        return RecordV1.from_dict(value)
    required = {"case_id", "record_type", "event_date", "input_text", "case_family", "split"}
    if not required <= value.keys():
        raise ProcessingError("invalid_fuzzy_input", f"line {index}: missing Golden case fields")
    return RecordV1.from_dict({
        "record_id": value["case_id"], "dataset_id": "fuzzy_golden", "source_file": "fuzzy-cleaning-v1.jsonl",
        "source_offset": index - 1, "record_type": value["record_type"], "event_date": value["event_date"],
        "title": None, "raw_text": value["input_text"], "clean_text": value["input_text"],
        "metadata": {"case_family": value["case_family"], "golden_split": value["split"]},
        "parser_version": "fuzzy-golden-v1", "schema_version": "record-v1",
    })


def read_fuzzy_input(path: str | Path) -> tuple[list[RecordV1], list[dict[str, Any]]]:
    values, records = [], []
    for index, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            raise ProcessingError("invalid_fuzzy_input", f"line {index}: invalid JSON") from None
        if not isinstance(value, dict):
            raise ProcessingError("invalid_fuzzy_input", f"line {index}: expected object")
        values.append(value)
        records.append(_record_from_value(value, index))
    return records, values


def fuzzy_clean_file(input_path: str | Path, config: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
    records, _ = read_fuzzy_input(input_path)
    cleaned, events, decisions = fuzzy_clean_records(records, config)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    review_queue = [decision for decision in decisions if decision["decision"] == "review"]
    report = {
        "algorithm_version": config["algorithm_version"],
        "counts": dict(sorted(Counter(item["decision"] for item in decisions).items())),
        "decision_fingerprint": semantic_fingerprint(decisions),
        "record_count": len(records),
        "schema_version": "fuzzy-decision-report-v1",
        "thresholds": config["thresholds"],
    }
    write_jsonl(output / "cleaned-records.jsonl", (asdict(record) for record in cleaned))
    write_jsonl(output / "cleaning-events.jsonl", (asdict(event) for event in events))
    write_jsonl(output / "decisions.jsonl", decisions)
    write_jsonl(output / "review-queue.jsonl", review_queue)
    write_json(output / "decision-report.json", report)
    return report


def _metrics(cases: list[dict[str, Any]], actual: dict[str, str]) -> dict[str, Any]:
    labels = ("applied", "review", "skipped")
    confusion = {expected: {predicted: 0 for predicted in labels} for expected in labels}
    for case in cases:
        confusion[case["expected_decision"]][actual.get(case["case_id"], "skipped")] += 1
    auto_total = sum(confusion[label]["applied"] for label in labels)
    applied_total = sum(confusion["applied"].values())
    review_total = sum(confusion["review"].values())
    true_auto = confusion["applied"]["applied"]
    false_removal = confusion["review"]["applied"] + confusion["skipped"]["applied"]
    return {
        "auto_precision": 1.0 if auto_total == 0 else true_auto / auto_total,
        "auto_precision_basis_points": 10000 if auto_total == 0 else _rounded_basis(true_auto, auto_total),
        "auto_recall": 1.0 if applied_total == 0 else true_auto / applied_total,
        "auto_recall_basis_points": 10000 if applied_total == 0 else _rounded_basis(true_auto, applied_total),
        "confusion_matrix": confusion,
        "false_removal_count": false_removal,
        "meaningless_review_count": confusion["skipped"]["review"],
        "review_capture": 1.0 if review_total == 0 else confusion["review"]["review"] / review_total,
        "review_capture_basis_points": 10000 if review_total == 0 else _rounded_basis(confusion["review"]["review"], review_total),
    }


def _decisions_for(cases: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, str]:
    records = [_record_from_value(case, index + 1) for index, case in enumerate(cases)]
    _, _, decisions = fuzzy_clean_records(records, config)
    by_id: dict[str, list[str]] = {}
    for item in decisions:
        by_id.setdefault(item["record_id"], []).append(item["decision"])
    return {case["case_id"]: max(by_id.get(case["case_id"], ["skipped"]), key=_rank_decision) for case in cases}


def nfkc_audit(records: list[RecordV1], config: dict[str, Any]) -> dict[str, Any]:
    before_raw = [record.raw_text for record in records]
    mappings: Counter[tuple[str, str]] = Counter()
    changed = 0
    before_chars = after_chars = 0
    for record in records:
        normalized = unicodedata.normalize("NFKC", record.raw_text)
        changed += normalized != record.raw_text
        before_chars += len(record.raw_text)
        after_chars += len(normalized)
        for char in record.raw_text:
            mapped = unicodedata.normalize("NFKC", char)
            if mapped != char:
                mappings[(char, mapped)] += 1
    examples = []
    for example in config.get("nfkc_score_examples", []):
        examples.append({
            **example,
            "raw_ratio": ratio(example["candidate"], example["template"]),
            "matching_view_ratio": ratio(matching_view(example["candidate"], config), matching_view(example["template"], config)),
        })
    return {
        "after_character_count": after_chars,
        "before_character_count": before_chars,
        "changed_record_count": changed,
        "raw_text_unchanged": before_raw == [record.raw_text for record in records],
        "score_examples": examples,
        "top_mappings": [
            {"before": before, "after": after, "count": count}
            for (before, after), count in sorted(mappings.items(), key=lambda item: (-item[1], item[0]))[:20]
        ],
    }


def evaluate_thresholds(input_path: str | Path, config: dict[str, Any], nfkc_records: list[RecordV1] | None = None) -> dict[str, Any]:
    validate_config(config)
    _, values = read_fuzzy_input(input_path)
    cases = [value for value in values if "expected_decision" in value]
    calibration = [case for case in cases if case["split"] == "calibration"]
    holdout = [case for case in cases if case["split"] == "holdout"]
    if not calibration or not holdout or len(calibration) + len(holdout) != len(cases):
        raise ProcessingError("invalid_fuzzy_input", "Golden cases must use calibration or holdout split")
    sensitivity = []
    for review in config["sensitivity_grid"]["review_thresholds"]:
        for auto in config["sensitivity_grid"]["auto_thresholds"]:
            if review >= auto:
                continue
            trial = deepcopy(config)
            trial["thresholds"] = {"review": review, "auto": auto}
            metrics = _metrics(calibration, _decisions_for(calibration, trial))
            sensitivity.append({"auto_threshold": auto, "review_threshold": review, **metrics})
    eligible = [row for row in sensitivity if row["false_removal_count"] == 0]
    if not eligible:
        raise ProcessingError("fuzzy_calibration_failed", "no threshold pair has zero calibration false removals")
    best = max(eligible, key=lambda row: (
        row["auto_recall_basis_points"], row["review_capture_basis_points"], -row["meaningless_review_count"],
        row["auto_threshold"], row["review_threshold"],
    ))
    selected = {"review": best["review_threshold"], "auto": best["auto_threshold"]}
    final_config = deepcopy(config)
    final_config["thresholds"] = selected
    holdout_metrics = _metrics(holdout, _decisions_for(holdout, final_config))
    report = {
        "algorithm_version": config["algorithm_version"],
        "calibration_case_count": len(calibration),
        "calibration_metrics": {key: value for key, value in best.items() if key not in {"auto_threshold", "review_threshold"}},
        "golden_case_count": len(cases),
        "holdout_case_count": len(holdout),
        "holdout_metrics": holdout_metrics,
        "nfkc_audit": nfkc_audit(nfkc_records or [], config),
        "schema_version": "fuzzy-threshold-report-v1",
        "selected_thresholds": selected,
        "sensitivity": sensitivity,
    }
    return report
