#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def differences(left: Any, right: Any, path: str = "$") -> list[str]:
    if type(left) is not type(right):
        return [f"{path}: type {type(left).__name__} != {type(right).__name__}"]
    if isinstance(left, dict):
        result = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}"
            if key not in left:
                result.append(f"{child}: missing in Python output")
            elif key not in right:
                result.append(f"{child}: missing in Rust output")
            else:
                result.extend(differences(left[key], right[key], child))
        return result
    if isinstance(left, list):
        if len(left) != len(right):
            return [f"{path}: length {len(left)} != {len(right)}"]
        return [item for index, values in enumerate(zip(left, right)) for item in differences(*values, f"{path}[{index}]")]
    return [] if left == right else [f"{path}: {left!r} != {right!r}"]


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    try:
        completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise SystemExit(f"parity blocked: command not found: {error.filename}") from None
    if completed.returncode:
        print(completed.stdout, end="", file=sys.stderr)
        print(completed.stderr, end="", file=sys.stderr)
        raise SystemExit(f"parity blocked: command exited {completed.returncode}: {' '.join(command)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="比较 Python/Rust profile 的完整 JSON 语义")
    parser.add_argument("--input", type=Path, default=ROOT / "fixtures/generated/normalized/records.jsonl")
    args = parser.parse_args()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "python/src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    with tempfile.TemporaryDirectory(prefix="corpus-profile-parity-") as temp:
        python_output = Path(temp) / "python.json"
        rust_output = Path(temp) / "rust.json"
        run(
            [sys.executable, "-m", "corpus_preprocessing_core", "profile", "--input", str(args.input), "--output", str(python_output)],
            env=env,
        )
        run(
            ["cargo", "run", "--quiet", "--manifest-path", str(ROOT / "rust/Cargo.toml"), "--", "profile", "--input", str(args.input), "--output", str(rust_output)]
        )
        python_profile = json.loads(python_output.read_text(encoding="utf-8"))
        rust_profile = json.loads(rust_output.read_text(encoding="utf-8"))
        diff = differences(python_profile, rust_profile)
        if diff:
            print("profile parity failed:", file=sys.stderr)
            for item in diff:
                print(f"- {item}", file=sys.stderr)
            return 1
        print("profile parity passed: Python and Rust JSON semantics are identical")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

