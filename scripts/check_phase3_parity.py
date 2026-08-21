#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from check_parity import differences


ROOT = Path(__file__).resolve().parents[1]
FUZZY_ARTIFACTS = (
    "cleaned-records.jsonl",
    "cleaning-events.jsonl",
    "decisions.jsonl",
    "review-queue.jsonl",
    "decision-report.json",
)
PIPELINE_ARTIFACTS = (
    "file-manifest.jsonl", "parsed-records.jsonl", "quarantine.jsonl", "sampled-before.jsonl",
    "cleaned-records.jsonl", "cleaning-events.jsonl", "sampled-pairs.jsonl", "sample-report.json",
    "fuzzy-cleaned-records.jsonl", "fuzzy-cleaning-events.jsonl", "fuzzy-decisions.jsonl",
    "fuzzy-review-queue.jsonl", "fuzzy-decision-report.json", "run-manifest.json",
)


def run(command: list[str], env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    if completed.returncode:
        print(completed.stdout, end="", file=sys.stderr)
        print(completed.stderr, end="", file=sys.stderr)
        raise SystemExit(f"stage3 parity blocked: command exited {completed.returncode}: {' '.join(command)}")


def read_artifact(path: Path) -> Any:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return json.loads(path.read_text(encoding="utf-8"))


def compare(label: str, left: Path, right: Path) -> list[str]:
    return [f"{label}{item[1:]}" for item in differences(read_artifact(left), read_artifact(right))]


def main() -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "python/src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    python_cli = [sys.executable, "-m", "corpus_preprocessing_core"]
    rust_cli = ["cargo", "run", "--quiet", "--manifest-path", str(ROOT / "rust/Cargo.toml"), "--"]
    with tempfile.TemporaryDirectory(prefix="corpus-stage3-parity-") as temporary:
        temporary = Path(temporary)
        outputs = {"python": temporary / "python", "rust": temporary / "rust"}
        for language, command in (("python", python_cli), ("rust", rust_cli)):
            run(command + [
                "fuzzy-clean", "--input", str(ROOT / "fixtures/golden/fuzzy-cleaning-v1.jsonl"),
                "--config", str(ROOT / "configs/fuzzy-cleaning-v1.json"),
                "--output-dir", str(outputs[language] / "fuzzy"),
            ], env if language == "python" else None)
            run(command + [
                "pipeline", "--input", str(ROOT / "fixtures/generated/raw"),
                "--parsing-config", str(ROOT / "configs/parsing-v1.json"),
                "--cleaning-config", str(ROOT / "configs/cleaning-v1.json"),
                "--sample-plan", str(ROOT / "configs/sample-multifield-v1.json"),
                "--fuzzy-config", str(ROOT / "configs/fuzzy-cleaning-v1.json"),
                "--output-dir", str(outputs[language] / "pipeline"),
            ], env if language == "python" else None)
        evaluation = temporary / "threshold-report.json"
        run(python_cli + [
            "evaluate-fuzzy", "--input", str(ROOT / "fixtures/golden/fuzzy-cleaning-v1.jsonl"),
            "--config", str(ROOT / "configs/fuzzy-cleaning-v1.json"),
            "--nfkc-input", str(ROOT / "fixtures/generated/normalized/records.jsonl"),
            "--output", str(evaluation),
        ], env)

        diff = []
        for name in FUZZY_ARTIFACTS:
            diff.extend(compare(f"fuzzy/{name}", outputs["python"] / "fuzzy" / name, outputs["rust"] / "fuzzy" / name))
        for name in PIPELINE_ARTIFACTS:
            diff.extend(compare(f"pipeline/{name}", outputs["python"] / "pipeline" / name, outputs["rust"] / "pipeline" / name))
        if diff:
            print("stage3 parity failed:", file=sys.stderr)
            for item in diff[:200]:
                print(f"- {item}", file=sys.stderr)
            if len(diff) > 200:
                print(f"- ... {len(diff) - 200} more differences", file=sys.stderr)
            return 1

        report = read_artifact(evaluation)
        config = json.loads((ROOT / "configs/fuzzy-cleaning-v1.json").read_text(encoding="utf-8"))
        holdout = report["holdout_metrics"]
        required = (
            report["selected_thresholds"] == config["thresholds"],
            report["calibration_metrics"]["false_removal_count"] == 0,
            holdout["auto_precision_basis_points"] == 10000,
            holdout["false_removal_count"] == 0,
            holdout["review_capture_basis_points"] >= 8000,
        )
        if not all(required):
            print("stage3 threshold targets failed: " + json.dumps(report, ensure_ascii=False), file=sys.stderr)
            return 1
        audit = report["nfkc_audit"]
        print(
            "stage3 parity passed: component basis points, decisions, spans, cleaned text, events, "
            "review queue, fuzzy pipeline, fingerprints, and run manifest match"
        )
        print(
            f"thresholds review={report['selected_thresholds']['review']} auto={report['selected_thresholds']['auto']}; "
            f"holdout auto_precision={holdout['auto_precision_basis_points']}/10000 "
            f"review_capture={holdout['review_capture_basis_points']}/10000 false_removals={holdout['false_removal_count']}"
        )
        print(
            f"NFKC audit: changed={audit['changed_record_count']}/240 chars={audit['before_character_count']}->{audit['after_character_count']} "
            f"raw_text_unchanged={str(audit['raw_text_unchanged']).lower()}"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
