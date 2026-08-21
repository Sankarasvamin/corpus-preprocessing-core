#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any


COUNT = 240
CATEGORIES = ("category_a", "category_b", "category_c", "category_d")
DATES = ("2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22")
RECORD_TYPES = ("article", "comment", "reply")
FORMATS = ("jsonl", "tsv", "html")
TSV_FIELDS = (
    "record_id",
    "dataset_id",
    "event_date_json",
    "record_type",
    "title_json",
    "raw_text_json",
    "category_signal_json",
    "source_batch",
    "synthetic",
)
SIGNALS = {
    "category_a": "脉冲星观测信号",
    "category_b": "海岸湿地观测信号",
    "category_c": "城市交通观测信号",
    "category_d": "古籍修复观测信号",
}


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source_format_for(index: int) -> str:
    # Every three consecutive record types rotate across all three formats.
    return FORMATS[((index // len(RECORD_TYPES)) + (index % len(RECORD_TYPES))) % len(FORMATS)]


def make_records(seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    adjectives = ("清晰", "连续", "局部", "稳定", "匿名", "可复核")
    records: list[dict[str, Any]] = []
    source_offsets = {name: 0 for name in FORMATS}

    for index in range(COUNT):
        category = CATEGORIES[index % len(CATEGORIES)]
        record_type = RECORD_TYPES[index % len(RECORD_TYPES)]
        source_format = source_format_for(index)
        event_date = DATES[(index // 12) % len(DATES)]
        source_batch = f"batch-{event_date}"
        records.append(
            {
                "record_id": f"syn-{index + 1:04d}",
                "dataset_id": category,
                "source_file": f"records.{source_format}",
                "source_offset": source_offsets[source_format],
                "record_type": record_type,
                "event_date": event_date,
                "title": f"合成标题 {index + 1}" if record_type == "article" else None,
                "raw_text": (
                    f"{SIGNALS[category]}：第{index + 1}条{record_type}样本，"
                    f"包含{rng.choice(adjectives)}的匿名事实 {rng.randrange(1000, 9999)}。"
                ),
                "clean_text": None,
                "metadata": {
                    "category_signal": SIGNALS[category],
                    "source_batch": source_batch,
                    "source_format": source_format,
                    "synthetic": True,
                },
                "parser_version": "parser-v1",
                "schema_version": "record-v1",
            }
        )
        source_offsets[source_format] += 1

    records[0]["raw_text"] = ""
    records[1]["raw_text"] = "  \n\t  "
    records[2]["raw_text"] = "零\u200b宽\ufeff字符样本"
    records[3]["raw_text"] = "Ｆｕｌｌ－ｗｉｄｔｈ　１２３ 与 half-width 123"
    records[4]["raw_text"] = "<p>正文<strong>含标签</strong>&amp;实体</p>"
    records[5]["raw_text"] = "站点页头｜正文内容｜免责声明：仅供合成测试｜相关推荐"
    records[6]["raw_text"] = records[7]["raw_text"] = "完全重复文本：同一内容保持逐字一致。"
    records[8]["raw_text"] = "近似重复文本包含甲乙丙丁和一个核心事实。"
    records[9]["raw_text"] = "近似重复文本包含甲乙丙丁、一个核心事实，以及轻微补充。"
    records[10]["raw_text"] = "链式样本 甲乙丙丁 戊己庚辛"
    records[11]["raw_text"] = "链式样本 甲乙丙丁 壬癸子丑"
    records[12]["raw_text"] = "链式样本 壬癸子丑 寅卯辰巳"
    records[13]["title"] = "完整候选标题"
    records[13]["raw_text"] = "完整候选正文。事实一完整，事实二完整，结尾完整。"
    records[14]["title"] = None
    records[14]["raw_text"] = "完整候选正文。事实一完整，事实二完整，结尾完整。"
    records[15]["title"] = "截断候选"
    records[15]["raw_text"] = "平台页头 推荐阅读 免责声明 完整候选正文。事实一完整……"
    records[16]["title"] = None
    records[17]["raw_text"] = "疑似截断正文，后续内容"
    records[18]["raw_text"] = "页头 页头 推荐 推荐 免责声明 正文很短 推荐阅读 页尾"
    records[19]["event_date"] = None
    records[19]["metadata"]["source_batch"] = "batch-date-missing"
    return records


def raw_record(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record["metadata"]
    return {
        "record_id": record["record_id"],
        "dataset_id": record["dataset_id"],
        "event_date": record["event_date"],
        "record_type": record["record_type"],
        "title": record["title"],
        "raw_text": record["raw_text"],
        "category_signal": metadata["category_signal"],
        "source_batch": metadata["source_batch"],
        "synthetic": metadata["synthetic"],
    }


def injection_manifest(seed: int, records: list[dict[str, Any]]) -> dict[str, Any]:
    category_ids = {
        category: next(record["record_id"] for record in records[30:] if record["dataset_id"] == category)
        for category in CATEGORIES
    }

    def item(case: str, ids: list[str], expected_use: str, raw_locations: list[str] | None = None) -> dict[str, Any]:
        value: dict[str, Any] = {"case": case, "record_ids": ids, "expected_use": expected_use}
        if raw_locations:
            value["raw_locations"] = raw_locations
        return value

    return {
        "schema_version": "fixture-injections-v1",
        "seed": seed,
        "injections": [
            item("empty_text", ["syn-0001"], "确定性清洗中的空正文识别"),
            item("whitespace_only_text", ["syn-0002"], "空白折叠后的空正文识别"),
            item("zero_width_characters", ["syn-0003"], "零宽字符移除回归"),
            item("unicode_normalization", ["syn-0004"], "NFKC 与全半角规范化回归"),
            item("html_tags", ["syn-0005"], "HTML 标签和实体清理回归"),
            item("template_noise", ["syn-0006"], "页头页尾、免责声明和推荐语识别"),
            item("exact_duplicate", ["syn-0007", "syn-0008"], "后续精确去重真值"),
            item("near_duplicate", ["syn-0009", "syn-0010"], "后续模糊去重真值"),
            item("similarity_chain", ["syn-0011", "syn-0012", "syn-0013"], "验证 strict STAR 阻断传递式误合并"),
            item("canonical_quality_candidates", ["syn-0014", "syn-0015", "syn-0016"], "比较完整度、标题和模板噪声的 canonical 候选"),
            item("missing_title", ["syn-0015", "syn-0017"], "标题完整度统计和 canonical 评分"),
            item("truncated_body", ["syn-0016", "syn-0018"], "正文截断特征回归"),
            item("different_template_ratios", ["syn-0014", "syn-0016", "syn-0019"], "模板占比特征与 canonical 选择"),
            item("missing_event_date", ["syn-0020"], "日期缺口与时间覆盖检查"),
            item("malformed_jsonl", ["raw-json-bad-001"], "坏行隔离和稳定错误类型", ["raw/records.jsonl:81"]),
            item("missing_required_field", ["raw-json-missing-001"], "Schema 必填字段检查", ["raw/records.jsonl:82"]),
            item("schema_drift", ["raw-json-drift-001"], "历史字段漂移识别", ["raw/records.jsonl:83"]),
            item("malformed_tsv", ["raw-tsv-bad-001"], "TSV 列数错误隔离", ["raw/records.tsv:82"]),
            item("category_specific_signals", list(category_ids.values()), "Leave-One-Category-Out 的匿名类别信号"),
        ],
    }


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    selected = [record for record in records if record["metadata"]["source_format"] == "jsonl"]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in selected:
            handle.write(dump_json(raw_record(record)) + "\n")
        handle.write('{"record_id":"raw-json-bad-001","raw_text":"unterminated"\n')
        handle.write(dump_json({"record_id": "raw-json-missing-001", "raw_text": "缺字段"}) + "\n")
        handle.write(dump_json({"record_id": "raw-json-drift-001", "text_v2": "字段改名", "published_at": 1785600000}) + "\n")


def tsv_row(value: dict[str, Any]) -> str:
    fields = (
        value["record_id"],
        value["dataset_id"],
        dump_json(value["event_date"]),
        value["record_type"],
        dump_json(value["title"]),
        dump_json(value["raw_text"]),
        dump_json(value["category_signal"]),
        value["source_batch"],
        "true" if value["synthetic"] else "false",
    )
    return "\t".join(fields)


def write_tsv(path: Path, records: list[dict[str, Any]]) -> None:
    selected = [record for record in records if record["metadata"]["source_format"] == "tsv"]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\t".join(TSV_FIELDS) + "\n")
        for record in selected:
            handle.write(tsv_row(raw_record(record)) + "\n")
        handle.write("raw-tsv-bad-001\tcategory_b\tonly-three-columns\n")


def attr(value: Any) -> str:
    encoded = dump_json(value).replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return html.escape(encoded, quote=True)


def write_html(path: Path, records: list[dict[str, Any]]) -> None:
    selected = [record for record in records if record["metadata"]["source_format"] == "html"]
    lines = ["<!doctype html>", '<html lang="zh-CN"><body>']
    for record in selected:
        raw = raw_record(record)
        lines.append(
            '<article data-record-id="{record_id}" data-dataset-id="{dataset_id}" '
            'data-event-date-json="{event_date}" data-record-type="{record_type}" '
            'data-title-json="{title}" data-raw-text-json="{raw_text}" '
            'data-category-signal-json="{category_signal}" data-source-batch="{source_batch}" '
            'data-synthetic="{synthetic}"></article>'.format(
                record_id=html.escape(raw["record_id"], quote=True),
                dataset_id=html.escape(raw["dataset_id"], quote=True),
                event_date=attr(raw["event_date"]),
                record_type=html.escape(raw["record_type"], quote=True),
                title=attr(raw["title"]),
                raw_text=attr(raw["raw_text"]),
                category_signal=attr(raw["category_signal"]),
                source_batch=html.escape(raw["source_batch"], quote=True),
                synthetic="true" if raw["synthetic"] else "false",
            )
        )
    lines.append("</body></html>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def case_record(record_id: str, record_type: str = "article") -> dict[str, Any]:
    return {
        "record_id": record_id,
        "dataset_id": "category_a",
        "event_date": "2026-08-01",
        "record_type": record_type,
        "title": "解析案例" if record_type == "article" else None,
        "raw_text": "自包含解析案例正文",
        "category_signal": SIGNALS["category_a"],
        "source_batch": "parser-cases",
        "synthetic": True,
    }


def write_parser_cases(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "array.json").write_text(
        json.dumps([case_record("case-array-1"), case_record("case-array-2", "comment")], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "bom.jsonl").write_text("\ufeff" + dump_json(case_record("case-bom-1")) + "\n", encoding="utf-8")
    (output_dir / "invalid-utf8.jsonl").write_bytes(b'{"record_id":"case-invalid-utf8","raw_text":"\xff"}\n')
    (output_dir / "unknown.bin").write_bytes(b"synthetic unknown format\n")
    (output_dir / "empty.jsonl").write_bytes(b"")
    (output_dir / "missing.jsonl").write_text(dump_json({"record_id": "case-missing-1", "raw_text": "缺字段"}) + "\n", encoding="utf-8")
    (output_dir / "malformed.jsonl").write_text(
        '{"record_id":"case-bad-json"\n' + dump_json(case_record("case-after-bad-json", "reply")) + "\n",
        encoding="utf-8",
    )
    (output_dir / "malformed.tsv").write_text(
        "\t".join(TSV_FIELDS) + "\n"
        + "case-bad-tsv\tcategory_a\tonly-three-columns\n"
        + tsv_row(case_record("case-after-bad-tsv", "comment"))
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "drift.jsonl").write_text(
        dump_json({"record_id": "case-drift-1", "text_v2": "未知漂移", "published_at": 1785600000}) + "\n",
        encoding="utf-8",
    )


def generate(seed: int, output_dir: Path, parser_cases_dir: Path | None = None) -> None:
    raw_dir = output_dir / "raw"
    normalized_dir = output_dir / "normalized"
    raw_dir.mkdir(parents=True, exist_ok=True)
    normalized_dir.mkdir(parents=True, exist_ok=True)
    records = make_records(seed)
    write_jsonl(raw_dir / "records.jsonl", records)
    write_tsv(raw_dir / "records.tsv", records)
    write_html(raw_dir / "records.html", records)
    (normalized_dir / "records.jsonl").write_text(
        "".join(dump_json(record) + "\n" for record in records),
        encoding="utf-8",
    )
    (output_dir / "injections.json").write_text(
        json.dumps(injection_manifest(seed, records), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    format_type_counts = Counter(
        (record["metadata"]["source_format"], record["record_type"]) for record in records
    )
    (output_dir / "generation.json").write_text(
        json.dumps(
            {
                "categories": list(CATEGORIES),
                "dates": list(DATES),
                "format_record_type_counts": {
                    f"{source_format}:{record_type}": format_type_counts[(source_format, record_type)]
                    for source_format in FORMATS
                    for record_type in RECORD_TYPES
                },
                "generator_version": "fixture-generator-v2",
                "record_count": COUNT,
                "seed": seed,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if parser_cases_dir is not None:
        write_parser_cases(parser_cases_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成确定性匿名合成语料")
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--parser-cases-dir", type=Path)
    args = parser.parse_args()
    generate(args.seed, args.output_dir, args.parser_cases_dir)
    print(f"generated {COUNT} records -> {args.output_dir}")


if __name__ == "__main__":
    main()
