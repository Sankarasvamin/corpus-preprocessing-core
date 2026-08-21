from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .core import LineValidationError, profile_jsonl, validate_jsonl
from .fuzzy import evaluate_thresholds, fuzzy_clean_file
from .processing import (
    ProcessingError,
    SamplePlanV1,
    clean_to_files,
    load_json,
    parse_directory,
    pipeline,
    risk_sample,
    sample_records,
    scan_directory,
    write_json,
    write_jsonl,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corpus_preprocessing_core")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="验证 RecordV1 JSONL")
    validate.add_argument("--input", required=True)
    profile = commands.add_parser("profile", help="输出确定性数据画像")
    profile.add_argument("--input", required=True)
    profile.add_argument("--output", required=True)

    scan = commands.add_parser("scan", help="递归扫描文件并输出 manifest")
    scan.add_argument("--input", required=True)
    scan.add_argument("--output", required=True)

    parse = commands.add_parser("parse", help="解析 TSV、JSONL、HTML、JSON 数组")
    parse.add_argument("--input", required=True)
    parse.add_argument("--config", required=True)
    parse.add_argument("--records-output", required=True)
    parse.add_argument("--quarantine-output", required=True)

    sample = commands.add_parser("sample", help="执行确定性哈希排序抽样")
    sample.add_argument("--input", required=True)
    sample.add_argument("--plan", required=True)
    sample.add_argument("--output", required=True)
    sample.add_argument("--report", required=True)
    sample.add_argument("--risk-output")
    sample.add_argument("--risk-target", type=int, default=20)
    sample.add_argument("--cleaning-config", default="configs/cleaning-v1.json")

    clean = commands.add_parser("clean", help="执行确定性清洗")
    clean.add_argument("--input", required=True)
    clean.add_argument("--config", required=True)
    clean.add_argument("--output", required=True)
    clean.add_argument("--events", required=True)
    clean.add_argument("--run-manifest", required=True)

    fuzzy = commands.add_parser("fuzzy-clean", help="清理已知模板的边界轻微变体")
    fuzzy.add_argument("--input", required=True)
    fuzzy.add_argument("--config", required=True)
    fuzzy.add_argument("--output-dir", required=True)

    evaluate = commands.add_parser("evaluate-fuzzy", help="评估 calibration/holdout 阈值")
    evaluate.add_argument("--input", required=True)
    evaluate.add_argument("--config", required=True)
    evaluate.add_argument("--output", required=True)
    evaluate.add_argument("--nfkc-input")

    run = commands.add_parser("pipeline", help="运行扫描、解析、抽样、清洗审计链")
    run.add_argument("--input", required=True)
    run.add_argument("--parsing-config", required=True)
    run.add_argument("--cleaning-config", required=True)
    run.add_argument("--sample-plan", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--fuzzy-config")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            records = validate_jsonl(args.input)
            print(f"validated {len(records)} records")
        elif args.command == "profile":
            profile = profile_jsonl(args.input)
            write_json(args.output, profile.to_dict())
            print(f"profiled {profile.total_records} records -> {args.output}")
        elif args.command == "scan":
            manifest = scan_directory(args.input, args.output)
            write_jsonl(args.output, (asdict(entry) for entry in manifest))
            print(f"scanned {len(manifest)} files -> {args.output}")
        elif args.command == "parse":
            records, quarantine = parse_directory(args.input, load_json(args.config))
            write_jsonl(args.records_output, (asdict(record) for record in records))
            write_jsonl(args.quarantine_output, (asdict(item) for item in quarantine))
            print(f"parsed {len(records)} records; quarantined {len(quarantine)}")
        elif args.command == "sample":
            records = validate_jsonl(args.input)
            plan = SamplePlanV1.from_dict(load_json(args.plan))
            selected, report = sample_records(records, plan)
            write_jsonl(args.output, (asdict(record) for record in selected))
            write_json(args.report, report)
            if args.risk_output:
                write_jsonl(args.risk_output, risk_sample(records, args.risk_target, load_json(args.cleaning_config)))
            print(f"sampled {len(selected)} of {report['filtered_population_size']} records")
        elif args.command == "clean":
            cleaned, events, cache_hit = clean_to_files(args.input, args.config, args.output, args.events, args.run_manifest)
            print(f"cleaned {len(cleaned)} records; events {len(events)}; cache_hit={str(cache_hit).lower()}")
        elif args.command == "fuzzy-clean":
            result = fuzzy_clean_file(args.input, load_json(args.config), args.output_dir)
            print(f"fuzzy-cleaned {result['record_count']} records; decisions={result['counts']}")
        elif args.command == "evaluate-fuzzy":
            nfkc_records = validate_jsonl(args.nfkc_input) if args.nfkc_input else None
            result = evaluate_thresholds(args.input, load_json(args.config), nfkc_records)
            write_json(args.output, result)
            print(f"selected fuzzy thresholds: {result['selected_thresholds']}")
        else:
            result = pipeline(args.input, args.parsing_config, args.cleaning_config, args.sample_plan, args.output_dir, args.fuzzy_config)
            print(f"pipeline completed; output_fingerprint={result['outputs']['cleaned-records.jsonl']}")
        return 0
    except LineValidationError as error:
        print(error, file=sys.stderr)
        return 1
    except ProcessingError as error:
        print(f"[{error.code}] {error}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as error:
        print(f"[io_error] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
