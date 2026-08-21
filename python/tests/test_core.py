from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from corpus_preprocessing_core.core import LineValidationError, RecordV1, profile_jsonl, validate_jsonl


ROOT = Path(__file__).resolve().parents[2]


def record(**changes):
    value = {
        "record_id": "r-1",
        "dataset_id": "category_a",
        "source_file": "raw/example.jsonl",
        "source_offset": 0,
        "record_type": "article",
        "event_date": "2026-08-20",
        "title": "标题",
        "raw_text": "正文",
        "clean_text": None,
        "metadata": {},
        "parser_version": "test-v1",
        "schema_version": "record-v1",
    }
    value.update(changes)
    return value


def write_jsonl(path: Path, values) -> None:
    path.write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values), encoding="utf-8")


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class CoreTests(unittest.TestCase):
    def test_fixture_generation_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "first", Path(temp) / "second"
            command = [sys.executable, str(ROOT / "scripts/generate_fixtures.py"), "--seed", "20260820"]
            subprocess.run(command + ["--output-dir", str(first)], check=True, capture_output=True, text=True)
            subprocess.run(command + ["--output-dir", str(second)], check=True, capture_output=True, text=True)
            self.assertEqual(tree_hashes(first), tree_hashes(second))
            records = (first / "normalized/records.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(records), 240)
            cases = {item["case"] for item in json.loads((first / "injections.json").read_text(encoding="utf-8"))["injections"]}
            self.assertTrue({"similarity_chain", "canonical_quality_candidates", "schema_drift"} <= cases)

    def test_record_v1_accepts_valid_input(self):
        parsed = RecordV1.from_dict(record())
        self.assertEqual(parsed.record_type, "article")
        self.assertIsNone(parsed.clean_text)

    def test_record_v1_rejects_invalid_input(self):
        missing = record()
        del missing["record_id"]
        with self.assertRaisesRegex(Exception, "required field"):
            RecordV1.from_dict(missing)
        with self.assertRaisesRegex(Exception, "non-negative"):
            RecordV1.from_dict(record(source_offset=-1))
        with self.assertRaisesRegex(Exception, "article, comment, or reply"):
            RecordV1.from_dict(record(record_type="other"))

    def test_profile_statistics(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "records.jsonl"
            write_jsonl(
                path,
                [
                    record(),
                    record(record_id="r-1", dataset_id="category_b", record_type="comment", event_date=None, title=None, raw_text="  "),
                    record(record_id="r-3", dataset_id="category_b", record_type="reply", title=""),
                ],
            )
            profile = profile_jsonl(path).to_dict()
            self.assertEqual(profile["total_records"], 3)
            self.assertEqual(profile["by_dataset_id"], {"category_a": 1, "category_b": 2})
            self.assertEqual(profile["by_event_date"], {"2026-08-20": 2, "<null>": 1})
            self.assertEqual(profile["missing_title_count"], 2)
            self.assertEqual(profile["missing_or_empty_body_count"], 1)
            self.assertEqual(profile["unique_record_id_count"], 2)
            self.assertEqual(profile["duplicate_record_id_count"], 1)

    def test_cli_success_and_failure_exit_codes(self):
        env = os.environ | {"PYTHONPATH": str(ROOT / "python/src")}
        with tempfile.TemporaryDirectory() as temp:
            valid, invalid = Path(temp) / "valid.jsonl", Path(temp) / "invalid.jsonl"
            output = Path(temp) / "profile.json"
            write_jsonl(valid, [record()])
            invalid.write_text('{"record_id":\n', encoding="utf-8")
            base = [sys.executable, "-m", "corpus_preprocessing_core"]
            ok = subprocess.run(base + ["validate", "--input", str(valid)], env=env, capture_output=True, text=True)
            bad = subprocess.run(base + ["validate", "--input", str(invalid)], env=env, capture_output=True, text=True)
            profiled = subprocess.run(base + ["profile", "--input", str(valid), "--output", str(output)], env=env, capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0)
            self.assertEqual(profiled.returncode, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["total_records"], 1)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("line 1 [invalid_json]", bad.stderr)
            self.assertNotIn("Traceback", bad.stderr)

    def test_validate_reports_line_number(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "records.jsonl"
            write_jsonl(path, [record(), record(schema_version="wrong")])
            with self.assertRaises(LineValidationError) as caught:
                validate_jsonl(path)
            self.assertEqual(caught.exception.line_number, 2)
            self.assertEqual(caught.exception.error_type, "invalid_value")


if __name__ == "__main__":
    unittest.main()
