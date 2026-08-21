from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from corpus_preprocessing_core.core import RecordV1
from corpus_preprocessing_core.processing import (
    ProcessingError,
    SamplePlanV1,
    allocate_quotas,
    clean_record,
    clean_records,
    clean_to_files,
    load_json,
    paired_sample,
    parse_directory,
    pipeline,
    risk_sample,
    sample_records,
    scan_directory,
    write_jsonl,
)


ROOT = Path(__file__).resolve().parents[2]
PARSING_CONFIG = ROOT / "configs/parsing-v1.json"
CLEANING_CONFIG = ROOT / "configs/cleaning-v1.json"
GENERATED = ROOT / "fixtures/generated"


def make_record(record_id: str, **changes) -> RecordV1:
    value = {
        "record_id": record_id,
        "dataset_id": "category_a",
        "source_file": "records.jsonl",
        "source_offset": 0,
        "record_type": "article",
        "event_date": "2026-08-01",
        "title": "标题",
        "raw_text": "普通正文内容",
        "clean_text": None,
        "metadata": {"source_format": "jsonl"},
        "parser_version": "parser-v1",
        "schema_version": "record-v1",
    }
    value.update(changes)
    return RecordV1.from_dict(value)


def make_plan(method: str, target: int, strata=None, minimum=0, seed=7, filters=None) -> SamplePlanV1:
    return SamplePlanV1(
        plan_id=f"test-{method}", seed=seed, target_size=target,
        strata_keys=strata or [], allocation_method=method,
        minimum_per_stratum=minimum, filters=filters or {},
        algorithm_version="hash-rank-v1", schema_version="sample-plan-v1",
    )


def raw_value(index: int) -> dict:
    return {
        "record_id": f"large-{index:05d}", "dataset_id": "category_a",
        "event_date": "2026-08-01", "record_type": ("article", "comment", "reply")[index % 3],
        "title": "标题" if index % 3 == 0 else None, "raw_text": f"正文 {index}",
        "category_signal": "测试信号", "source_batch": "large", "synthetic": True,
    }


class Phase2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsing_config = load_json(PARSING_CONFIG)
        cls.cleaning_config = load_json(CLEANING_CONFIG)
        cls.truth = [RecordV1.from_dict(json.loads(line)) for line in (GENERATED / "normalized/records.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_fixture_v2_is_self_contained_and_mixed(self):
        cross = {(fmt, kind): 0 for fmt in ("jsonl", "tsv", "html") for kind in ("article", "comment", "reply")}
        for record in self.truth:
            cross[(record.metadata["source_format"], record.record_type)] += 1
        self.assertEqual(len(self.truth), 240)
        self.assertTrue(all(26 <= count <= 27 for count in cross.values()))
        self.assertEqual(len(json.loads((GENERATED / "injections.json").read_text(encoding="utf-8"))["injections"]), 19)

    def test_main_parse_restores_truth_and_quarantine(self):
        parsed, quarantine = parse_directory(GENERATED / "raw", self.parsing_config)
        key = lambda record: json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)
        self.assertEqual(sorted(map(key, parsed)), sorted(map(key, self.truth)))
        self.assertEqual([item.error_code for item in quarantine], ["invalid_json", "missing_field", "unknown_schema_drift", "malformed_tsv"])

    def test_parser_cases_cover_formats_encoding_and_recovery(self):
        manifest = scan_directory(ROOT / "fixtures/parser_cases")
        statuses = {entry.relative_path: (entry.encoding_status, entry.file_status) for entry in manifest}
        self.assertEqual(statuses["bom.jsonl"][0], "utf8_bom")
        self.assertEqual(statuses["invalid-utf8.jsonl"][1], "encoding_error")
        self.assertEqual(statuses["empty.jsonl"][1], "empty")
        self.assertEqual(statuses["unknown.bin"][1], "unsupported")
        parsed, quarantine = parse_directory(ROOT / "fixtures/parser_cases", self.parsing_config)
        self.assertEqual({record.record_id for record in parsed}, {"case-array-1", "case-array-2", "case-bom-1", "case-after-bad-json", "case-after-bad-tsv"})
        self.assertEqual(
            {item.error_code for item in quarantine},
            {"invalid_encoding", "unsupported_format", "missing_field", "invalid_json", "malformed_tsv", "unknown_schema_drift"},
        )

    def test_streaming_jsonl_and_tsv_10000_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (root / "large.jsonl").open("w", encoding="utf-8") as handle:
                for index in range(5000):
                    handle.write(json.dumps(raw_value(index), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            headers = ("record_id", "dataset_id", "event_date_json", "record_type", "title_json", "raw_text_json", "category_signal_json", "source_batch", "synthetic")
            with (root / "large.tsv").open("w", encoding="utf-8") as handle:
                handle.write("\t".join(headers) + "\n")
                for index in range(5000, 10000):
                    raw = raw_value(index)
                    fields = (raw["record_id"], raw["dataset_id"], json.dumps(raw["event_date"]), raw["record_type"], json.dumps(raw["title"], ensure_ascii=False), json.dumps(raw["raw_text"], ensure_ascii=False), json.dumps(raw["category_signal"], ensure_ascii=False), raw["source_batch"], "true")
                    handle.write("\t".join(fields) + "\n")
            parsed, quarantine = parse_directory(root, self.parsing_config)
            self.assertEqual(len(parsed), 10000)
            self.assertEqual(quarantine, [])

    def test_hash_sampling_seed_and_input_order(self):
        first, report = sample_records(self.truth, make_plan("simple_random", 24, seed=11))
        repeated, repeated_report = sample_records(list(reversed(self.truth)), make_plan("simple_random", 24, seed=11))
        changed, _ = sample_records(self.truth, make_plan("simple_random", 24, seed=12))
        self.assertEqual([r.record_id for r in first], [r.record_id for r in repeated])
        self.assertEqual(report, repeated_report)
        self.assertNotEqual({r.record_id for r in first}, {r.record_id for r in changed})

    def test_allocation_methods_and_ties(self):
        capacities = {'["a"]': 1, '["b"]': 4, '["c"]': 5}
        self.assertEqual(allocate_quotas(capacities, make_plan("proportional", 5, ["dataset_id"])), {'["a"]': 1, '["b"]': 2, '["c"]': 2})
        self.assertEqual(allocate_quotas(capacities, make_plan("equal", 6, ["dataset_id"])), {'["a"]': 1, '["b"]': 3, '["c"]': 2})
        minimum = allocate_quotas(capacities, make_plan("minimum_then_proportional", 7, ["dataset_id"], minimum=2))
        self.assertEqual(sum(minimum.values()), 7)
        self.assertEqual(minimum['["a"]'], 1)
        tied = allocate_quotas({'["a"]': 3, '["b"]': 3}, make_plan("proportional", 3, ["dataset_id"]))
        self.assertEqual(tied, {'["a"]': 2, '["b"]': 1})

    def test_infeasible_minimum_and_target_errors(self):
        with self.assertRaisesRegex(ProcessingError, "minimum") as error:
            allocate_quotas({"a": 5, "b": 5}, make_plan("minimum_then_proportional", 3, ["dataset_id"], minimum=2))
        self.assertEqual(error.exception.code, "infeasible_minimum")
        with self.assertRaises(ProcessingError) as error:
            sample_records(self.truth, make_plan("simple_random", 241))
        self.assertEqual(error.exception.code, "target_exceeds_population")

    def test_null_missing_strata_and_filters(self):
        records = [
            make_record("a", event_date=None, metadata={}),
            make_record("b", metadata={"source_format": "jsonl"}),
            make_record("c", dataset_id="category_b", metadata={"source_format": "tsv"}),
        ]
        selected, report = sample_records(records, make_plan("equal", 2, ["event_date", "metadata.source_format"]))
        keys = {item["key"] for item in report["strata"]}
        self.assertTrue(any("__NULL__" in key for key in keys))
        self.assertTrue(any("__MISSING__" in key for key in keys))
        filtered, _ = sample_records(records, make_plan("simple_random", 1, filters={"dataset_id": "category_b"}))
        self.assertEqual(filtered[0].record_id, "c")

    def test_high_risk_and_paired_samples(self):
        risks = risk_sample(self.truth, 20, self.cleaning_config)
        risk_ids = {item["record_id"] for item in risks}
        self.assertTrue({"syn-0001", "syn-0002", "syn-0006", "syn-0020"} <= risk_ids)
        cleaned, events = clean_records(self.truth, self.cleaning_config)
        pairs = paired_sample(self.truth[:12], cleaned, events)
        self.assertEqual([pair["record_id"] for pair in pairs], [record.record_id for record in self.truth[:12]])
        self.assertTrue(any(pair["changed"] for pair in pairs))

    def test_golden_cleaning_cases(self):
        for index, line in enumerate((ROOT / "fixtures/golden/cleaning-v1.jsonl").read_text(encoding="utf-8").splitlines()):
            case = json.loads(line)
            record = make_record(f"golden-{index}", raw_text=case["raw_text"])
            cleaned, events = clean_record(record, self.cleaning_config)
            reviews = [event.reason_code for event in events if event.decision == "review"]
            self.assertEqual(cleaned.clean_text, case["expected_clean_text"], case["case_id"])
            self.assertEqual(reviews, case["expected_review_reasons"], case["case_id"])
            self.assertEqual(cleaned.raw_text, case["raw_text"])

    def test_injected_cleaning_regression_and_no_special_ids(self):
        expected = {
            "syn-0001": "", "syn-0002": "", "syn-0003": "零宽字符样本",
            "syn-0004": "Full-width 123 与 half-width 123", "syn-0005": "正文含标签&实体",
            "syn-0006": "正文内容",
        }
        cleaned, _ = clean_records(self.truth, self.cleaning_config)
        by_id = {record.record_id: record.clean_text for record in cleaned}
        self.assertEqual({key: by_id[key] for key in expected}, expected)

    def test_cleaning_priority_conflict_noop_and_idempotency(self):
        bad = json.loads(json.dumps(self.cleaning_config))
        bad["rules"][1]["priority"] = bad["rules"][0]["priority"]
        with self.assertRaises(ProcessingError) as error:
            clean_record(make_record("bad"), bad)
        self.assertEqual(error.exception.code, "duplicate_priority")
        unchanged, events = clean_record(make_record("noop", raw_text="普通正文内容"), self.cleaning_config)
        self.assertFalse(any(event.decision == "applied" for event in events))
        cleaned, first_events = clean_records(self.truth, self.cleaning_config)
        recleaned, second_events = clean_records(cleaned, self.cleaning_config)
        self.assertEqual([record.clean_text for record in cleaned], [record.clean_text for record in recleaned])
        self.assertEqual([asdict(event) for event in first_events], [asdict(event) for event in second_events])

    def test_four_date_regression(self):
        cleaned, _ = clean_records(self.truth, self.cleaning_config)
        dates = {record.event_date for record in cleaned if record.event_date is not None}
        self.assertEqual(dates, {"2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22"})
        self.assertEqual(len(cleaned), 240)

    def test_cache_hit_and_invalidation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path = root / "input.jsonl"
            output, events, cache = root / "cleaned.jsonl", root / "events.jsonl", root / "cache.json"
            write_jsonl(input_path, [asdict(make_record("cache", raw_text="零\u200b宽"))])
            first = clean_to_files(input_path, CLEANING_CONFIG, output, events, cache)
            second = clean_to_files(input_path, CLEANING_CONFIG, output, events, cache)
            self.assertFalse(first[2])
            self.assertTrue(second[2])
            self.assertEqual(first[:2], second[:2])
            output.write_text("corrupt\n", encoding="utf-8")
            third = clean_to_files(input_path, CLEANING_CONFIG, output, events, cache)
            self.assertFalse(third[2])
            old_key = json.loads(cache.read_text(encoding="utf-8"))["cache_key"]
            changed_config = root / "cleaning.json"
            config = json.loads(CLEANING_CONFIG.read_text(encoding="utf-8"))
            config["short_text_threshold"] = 7
            changed_config.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            clean_to_files(input_path, changed_config, output, events, cache)
            config_key = json.loads(cache.read_text(encoding="utf-8"))["cache_key"]
            self.assertNotEqual(old_key, config_key)
            write_jsonl(input_path, [asdict(make_record("cache", raw_text="输入变化\u200b"))])
            clean_to_files(input_path, changed_config, output, events, cache)
            self.assertNotEqual(config_key, json.loads(cache.read_text(encoding="utf-8"))["cache_key"])

    def test_pipeline_outputs_and_repeatability(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "first", Path(temp) / "second"
            one = pipeline(GENERATED / "raw", PARSING_CONFIG, CLEANING_CONFIG, ROOT / "configs/sample-multifield-v1.json", first)
            two = pipeline(GENERATED / "raw", PARSING_CONFIG, CLEANING_CONFIG, ROOT / "configs/sample-multifield-v1.json", second)
            self.assertEqual(one, two)
            self.assertEqual(len((first / "parsed-records.jsonl").read_text(encoding="utf-8").splitlines()), 240)
            self.assertEqual(len((first / "quarantine.jsonl").read_text(encoding="utf-8").splitlines()), 4)
            self.assertEqual(len((first / "sampled-before.jsonl").read_text(encoding="utf-8").splitlines()), 60)


if __name__ == "__main__":
    unittest.main()
