#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

for command in python3 rustc cargo; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "environment check failed: required command not found: $command" >&2
    exit 1
  fi
done

python3 --version
rustc --version
cargo --version

VERIFY_TMP="$(mktemp -d "${TMPDIR:-/tmp}/corpus-preprocessing-core.XXXXXX")"
trap 'rm -rf "$VERIFY_TMP"' EXIT

python3 scripts/generate_fixtures.py --seed 20260820 --output-dir fixtures/generated \
  --parser-cases-dir fixtures/parser_cases
python3 scripts/generate_fixtures.py --seed 20260820 --output-dir "$VERIFY_TMP/first" \
  --parser-cases-dir "$VERIFY_TMP/parser-first"
python3 scripts/generate_fixtures.py --seed 20260820 --output-dir "$VERIFY_TMP/second" \
  --parser-cases-dir "$VERIFY_TMP/parser-second"
diff -r "$VERIFY_TMP/first" "$VERIFY_TMP/second"
diff -r "$VERIFY_TMP/parser-first" "$VERIFY_TMP/parser-second"
echo "fixture determinism passed"

for schema in contracts/*.schema.json; do
  python3 -m json.tool "$schema" >/dev/null
done
echo "contract JSON parsing passed"

PYTHONPATH=python/src python3 -m unittest discover -s python/tests -v
cargo test --manifest-path rust/Cargo.toml

PYTHONPATH=python/src python3 -m corpus_preprocessing_core validate \
  --input fixtures/generated/normalized/records.jsonl
cargo run --quiet --manifest-path rust/Cargo.toml -- validate \
  --input fixtures/generated/normalized/records.jsonl
python3 scripts/check_parity.py
python3 scripts/check_phase2_parity.py
python3 scripts/check_phase3_parity.py

echo "all verification checks passed"
