# 合成 fixtures

`generated/` 由 `fixture-generator-v2` 生成：

- `raw/records.jsonl`、`records.tsv`、`records.html` 各含 80 条主记录，且各自混合四类别、四日期和 article/comment/reply。
- `normalized/records.jsonl` 是 240 条合法 `RecordV1` 参考真值。
- `injections.json` 记录 19 组异常类型、record ID 和测试用途。
- `generation.json` 记录固定 seed、规模、格式×类型分布和生成器版本。

`parser_cases/` 是不计入主 240 条的独立小型解析案例，覆盖 JSON 数组、UTF-8 BOM、非法 UTF-8、未知扩展名、空文件、缺字段、坏 JSONL/TSV 后继续读取和未知 schema drift。

`golden/cleaning-v1.jsonl` 是 16 条人工可读确定性清洗 Golden Set。

所有内容均为程序合成，不含公司原始数据、私人数据或真实内部字段。解析器必须从原始字段和文件上下文构造 RecordV1，不得读取真值或根据 ID、正文信号猜测类别。
