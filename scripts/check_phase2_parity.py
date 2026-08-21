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
PIPELINE_ARTIFACTS = (
    "file-manifest.jsonl",
    "parsed-records.jsonl",
    "quarantine.jsonl",
    "sampled-before.jsonl",
    "cleaned-records.jsonl",
    "cleaning-events.jsonl",
    "sampled-pairs.jsonl",
    "sample-report.json",
    "run-manifest.json",
)


def run(command: list[str], env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    if completed.returncode:
        print(completed.stdout, end="", file=sys.stderr)
        print(completed.stderr, end="", file=sys.stderr)
        raise SystemExit(f"stage2 parity blocked: command exited {completed.returncode}: {' '.join(command)}")


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
    with tempfile.TemporaryDirectory(prefix="corpus-stage2-parity-") as temp:
        temp = Path(temp)
        outputs = {"python": temp / "python", "rust": temp / "rust"}
        for language, command in (("python", python_cli), ("rust", rust_cli)):
            run(command + [
                "pipeline", "--input", str(ROOT / "fixtures/generated/raw"),
                "--parsing-config", str(ROOT / "configs/parsing-v1.json"),
                "--cleaning-config", str(ROOT / "configs/cleaning-v1.json"),
                "--sample-plan", str(ROOT / "configs/sample-multifield-v1.json"),
                "--output-dir", str(outputs[language]),
            ], env if language == "python" else None)
            for plan_name in ("sample-simple-v1.json", "sample-stratified-v1.json"):
                stem = Path(plan_name).stem
                run(command + [
                    "sample", "--input", str(outputs[language] / "parsed-records.jsonl"),
                    "--plan", str(ROOT / "configs" / plan_name),
                    "--output", str(outputs[language] / f"{stem}.jsonl"),
                    "--report", str(outputs[language] / f"{stem}-report.json"),
                ], env if language == "python" else None)

        diff = []
        for name in PIPELINE_ARTIFACTS:
            diff.extend(compare(name, outputs["python"] / name, outputs["rust"] / name))
        for stem in ("sample-simple-v1", "sample-stratified-v1"):
            diff.extend(compare(f"{stem}.jsonl", outputs["python"] / f"{stem}.jsonl", outputs["rust"] / f"{stem}.jsonl"))
            diff.extend(compare(f"{stem}-report.json", outputs["python"] / f"{stem}-report.json", outputs["rust"] / f"{stem}-report.json"))
        if diff:
            print("stage2 parity failed:", file=sys.stderr)
            for item in diff[:200]:
                print(f"- {item}", file=sys.stderr)
            if len(diff) > 200:
                print(f"- ... {len(diff) - 200} more differences", file=sys.stderr)
            return 1
        print("stage2 parity passed: scan, parse, quarantine, sampling, cleaning, events, fingerprints, and run manifest match")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
