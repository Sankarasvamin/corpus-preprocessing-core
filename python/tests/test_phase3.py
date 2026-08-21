from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from corpus_preprocessing_core.core import RecordV1, validate_jsonl
from corpus_preprocessing_core.fuzzy import (
    _resolve_overlaps,
    char_jaccard,
    evaluate_thresholds,
    fuzzy_clean_record,
    levenshtein,
    matching_view,
    nfkc_audit,
    partial_ratio,
    ratio,
    read_fuzzy_input,
    score_pair,
    token_set_ratio,
    token_sort_ratio,
    validate_config,
)
from corpus_preprocessing_core.processing import ProcessingError, load_json, pipeline


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "configs/fuzzy-cleaning-v1.json"
GOLDEN_PATH = ROOT / "fixtures/golden/fuzzy-cleaning-v1.jsonl"
NORMALIZED_PATH = ROOT / "fixtures/generated/normalized/records.jsonl"


def make_record(text: str, record_id: str = "test") -> RecordV1:
    return RecordV1.from_dict({
        "record_id": record_id, "dataset_id": "category_a", "source_file": "test.jsonl",
        "source_offset": 0, "record_type": "article", "event_date": "2026-08-01",
        "title": None, "raw_text": text, "clean_text": text, "metadata": {},
        "parser_version": "test-v1", "schema_version": "record-v1",
    })


class Phase3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_json(CONFIG_PATH)
        cls.records, cls.cases = read_fuzzy_input(GOLDEN_PATH)

    def test_levenshtein_unicode_and_edits(self):
        self.assertEqual(levenshtein("", ""), 0)
        self.assertEqual(levenshtein("", "中文"), 2)
        self.assertEqual(levenshtein("中文", "中闻"), 1)
        self.assertEqual(levenshtein("公告", "公X告"), 1)
        self.assertEqual(levenshtein("公X告", "公告"), 1)

    def test_component_scores_and_rounding(self):
        self.assertEqual(ratio("", ""), 10000)
        self.assertEqual(ratio("abc", "axc"), 6667)
        self.assertEqual(partial_ratio("公告", "平台公告内容"), 10000)
        self.assertEqual(token_sort_ratio("News SOURCE", "source news"), 10000)
        self.assertEqual(token_set_ratio("source news", "news source extra"), 10000)
        self.assertEqual(token_set_ratio("", ""), 10000)
        self.assertEqual(char_jaccard("abc", "abc"), 10000)
        self.assertEqual(char_jaccard("", ""), 10000)
        self.assertEqual(char_jaccard("abc", "abd"), 0)

    def test_matching_view_nfkc_and_score(self):
        self.assertEqual(matching_view("ＳＯＵＲＣＥ： ＮＥＷＳ", self.config), "source:news")
        raw = ratio("平台公告：内容仅供参考", "平台公告:内容仅供参考")
        normalized = score_pair("平台公告：内容仅供参考", "平台公告:内容仅供参考", self.config)["ratio"]
        self.assertLess(raw, normalized)
        self.assertEqual(normalized, 10000)

    def test_invalid_config_errors_are_stable(self):
        cases = [
            ("invalid_fuzzy_weights", lambda value: value["score_weights"].update(ratio=24)),
            ("invalid_fuzzy_thresholds", lambda value: value.update(thresholds={"review": 9500, "auto": 9500})),
            ("invalid_fuzzy_regex", lambda value: value["templates"][0].update(compatible_regex=r"^(公告)\\1$")),
        ]
        for code, mutate in cases:
            config = deepcopy(self.config)
            mutate(config)
            with self.assertRaises(ProcessingError) as error:
                validate_config(config)
            self.assertEqual(error.exception.code, code)

    def test_golden_inventory_and_expected_outputs(self):
        self.assertEqual(len(self.cases), 72)
        self.assertEqual({split: sum(case["split"] == split for case in self.cases) for split in ("calibration", "holdout")}, {"calibration": 36, "holdout": 36})
        for split in ("calibration", "holdout"):
            self.assertEqual(
                {label: sum(case["split"] == split and case["expected_decision"] == label for case in self.cases) for label in ("applied", "review", "skipped")},
                {"applied": 12, "review": 12, "skipped": 12},
            )
        self.assertGreaterEqual(len({case["case_family"] for case in self.cases}), 12)
        for record, case in zip(self.records, self.cases):
            cleaned, events, decisions = fuzzy_clean_record(record, self.config)
            actual = max((item["decision"] for item in decisions), default="skipped", key={"skipped": 1, "review": 2, "applied": 3}.get)
            if case["expected_decision"] == "applied":
                self.assertEqual(actual, "applied", case["case_id"])
            else:
                self.assertNotEqual(actual, "applied", case["case_id"])
            self.assertEqual(cleaned.clean_text, case["expected_cleaned_text"], case["case_id"])
            self.assertEqual(cleaned.raw_text, case["input_text"], case["case_id"])
            if actual != "applied":
                self.assertTrue(all(event.before_hash == event.after_hash and event.removed_chars == 0 for event in events))

    def test_decisions_events_and_idempotent_text(self):
        applied, applied_events, _ = fuzzy_clean_record(make_record("平台公告:内容仅供参考\n真实正文"), self.config)
        review, review_events, _ = fuzzy_clean_record(make_record("平台公吿:内容仅供参考\n真实正文", "review"), self.config)
        skipped, skipped_events, _ = fuzzy_clean_record(make_record("本文讨论平台公告:内容仅供参考\n真实正文", "skip"), self.config)
        self.assertEqual(applied.clean_text, "真实正文")
        self.assertTrue(any(event.decision == "applied" and event.removed_chars > 0 for event in applied_events))
        self.assertEqual(review.clean_text, review.raw_text)
        self.assertTrue(any(event.decision == "review" for event in review_events))
        self.assertEqual(skipped.clean_text, skipped.raw_text)
        self.assertTrue(any(event.decision == "skipped" for event in skipped_events))
        repeated, _, _ = fuzzy_clean_record(applied, self.config)
        self.assertEqual(repeated.clean_text, applied.clean_text)
        self.assertEqual(repeated.raw_text, applied.raw_text)

    def test_boundary_position_length_margin_and_overlap(self):
        prefix, _, prefix_decisions = fuzzy_clean_record(make_record("平台公告:内容仅供参考\n正文"), self.config)
        suffix, _, suffix_decisions = fuzzy_clean_record(make_record("正文\n免责声明:本文仅供学习交流"), self.config)
        line, _, line_decisions = fuzzy_clean_record(make_record("正文一\n相关推荐\n正文二"), self.config)
        middle, _, middle_decisions = fuzzy_clean_record(make_record("正文提到平台公告:内容仅供参考然后继续"), self.config)
        short, _, short_decisions = fuzzy_clean_record(make_record("公\n正文"), self.config)
        long_text = "平台公告:" + "很长" * 100 + "\n正文"
        _, _, long_decisions = fuzzy_clean_record(make_record(long_text), self.config)
        self.assertEqual(prefix.clean_text, "正文")
        self.assertEqual(suffix.clean_text, "正文")
        self.assertEqual(line.clean_text, "正文一\n正文二")
        self.assertTrue(any(item["position"] == "prefix" for item in prefix_decisions))
        self.assertTrue(any(item["position"] == "suffix" for item in suffix_decisions))
        self.assertTrue(any(item["position"] == "line" for item in line_decisions))
        self.assertEqual(middle_decisions, [])
        self.assertTrue(all(item["decision"] == "skipped" for item in short_decisions))
        self.assertTrue(all(len(item["matched_span"]) <= self.config["candidate_limits"]["max_length"] for item in long_decisions))
        base = {
            "components": {}, "evidence_count": 0, "gates": {}, "length_ratio": 0, "margin": 0,
            "matched_span": "xxxxx", "position": "line", "reason_codes": [], "record_id": "x",
        }
        overlap = _resolve_overlaps([
            {**base, "combined_score": 9500, "decision": "review", "span_start": 0, "span_end": 5, "template_id": "b"},
            {**base, "combined_score": 9000, "decision": "applied", "span_start": 2, "span_end": 7, "template_id": "a"},
        ])
        self.assertEqual([item["template_id"] for item in overlap], ["a"])

    def test_threshold_evaluation_and_holdout_targets(self):
        report = evaluate_thresholds(GOLDEN_PATH, self.config)
        self.assertEqual(report["selected_thresholds"], {"review": 6000, "auto": 9500})
        self.assertEqual(len(report["sensitivity"]), 19)
        self.assertEqual(report["calibration_metrics"]["false_removal_count"], 0)
        self.assertEqual(report["holdout_metrics"]["auto_precision_basis_points"], 10000)
        self.assertEqual(report["holdout_metrics"]["false_removal_count"], 0)
        self.assertGreaterEqual(report["holdout_metrics"]["review_capture_basis_points"], 8000)

    def test_nfkc_audit_preserves_raw_evidence(self):
        audit = nfkc_audit(validate_jsonl(NORMALIZED_PATH), self.config)
        self.assertEqual(audit["changed_record_count"], 231)
        self.assertTrue(audit["raw_text_unchanged"])
        self.assertEqual(audit["before_character_count"], 8766)
        self.assertEqual(audit["after_character_count"], 8770)
        self.assertEqual(audit["top_mappings"][0], {"before": "，", "after": ",", "count": 228})

    def test_pipeline_fuzzy_off_and_on(self):
        args = (
            ROOT / "fixtures/generated/raw", ROOT / "configs/parsing-v1.json",
            ROOT / "configs/cleaning-v1.json", ROOT / "configs/sample-multifield-v1.json",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            off = pipeline(*args, root / "off")
            explicit_off = pipeline(*args, root / "explicit-off", fuzzy_config_path=None)
            on = pipeline(*args, root / "on", fuzzy_config_path=CONFIG_PATH)
            repeated = pipeline(*args, root / "repeated", fuzzy_config_path=CONFIG_PATH)
            self.assertEqual(off, explicit_off)
            self.assertEqual(on, repeated)
            self.assertNotIn("fuzzy_config_fingerprint", off)
            self.assertEqual(on["algorithm_version"], "pipeline-v1+fuzzy-template-clean-v1")
            for name in ("fuzzy-cleaned-records.jsonl", "fuzzy-cleaning-events.jsonl", "fuzzy-decisions.jsonl", "fuzzy-review-queue.jsonl", "fuzzy-decision-report.json"):
                self.assertTrue((root / "on" / name).is_file())
                self.assertEqual((root / "on" / name).read_bytes(), (root / "repeated" / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
