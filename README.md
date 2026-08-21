# corpus-preprocessing-core

`0.3.0` 是“实习算法复现计划”的可复现语料预处理核心。它用完全匿名的合成新闻、评论和回复，复现多格式接入、可追溯解析、确定性抽样、确定性清洗、已知模板轻微变体清洗和审计链。现实目标不是只得到一列“干净文本”，而是让每条结果都能回答：来自哪个文件和位置、为什么被改、两种语言是否给出相同结果、相同输入能否重放。

仓库只使用程序生成的合成数据，不含公司原始数据、私人数据、真实内部字段、密钥或线上调用。

## 为什么 contract-first

后续仍有精确/模糊去重、strict STAR clustering、canonical 选择、质量评分、消融评估和交付审计。先固定 `RecordV1`、`CleaningEventV1`、`SamplePlanV1`、文件 manifest 和 quarantine 契约，才能让解析、抽样和清洗共享字段含义、可空性、版本、哈希和溯源语义。

`RecordV1` 的 12 个顶层字段全部必填；`event_date`、`title`、`clean_text` 允许为 `null`。来源特有信息进入 `metadata`，`raw_text` 永不覆盖，清洗结果只写 `clean_text`。 `record_type` 只接受 `article`、`comment`、`reply`，`schema_version` 固定为 `record-v1`。

## Python 与 Rust 的职责

| 能力 | Python | Rust |
|---|---|---|
| `validate`、`profile` | 参考语义 | 同语义、面向性能路径 |
| `scan`、`parse` | 参考实现 | 独立实现并做完整 parity |
| `SAM-01`～`SAM-03` | 参考实现 | 同一哈希排序和配额语义 |
| 高风险、前后配对 | `SAM-04`、`SAM-05` | pipeline 生成前后配对 |
| 确定性清洗 | 配置驱动参考实现 | 同规则、事件和指纹语义 |
| 模糊模板清洗 `FCL-01`～`FCL-07` | 参考实现 | 相同候选、整数基点、决策与事件 |
| 阈值敏感性 `FCL-08` | calibration 选择、holdout 验证与 NFKC 审计 | 使用最终阈值复现决策 |
| fixture 生成 | 唯一 `fixture-generator-v2` | 消费同一原始输入 |

Python 只使用标准库。Rust 沿用 `serde`、`serde_json`、`sha2`、`unicode-normalization`，仅为 Python/Rust 兼容锚定正则增加 `regex`；不引入 fuzzy matching 库、CLI 框架或随机数库。

## 目录

```text
configs/                         解析、确定性/模糊清洗和三份抽样计划
contracts/                       五个 v1 JSON Schema
fixtures/generated/raw/          混合类型的 JSONL、TSV、HTML 主数据
fixtures/generated/normalized/   240 条 RecordV1 参考真值
fixtures/generated/injections.json
                                 19 组异常注入真值
fixtures/parser_cases/            编码、坏行、JSON 数组等小型解析案例
fixtures/golden/                  16 条确定性清洗 + 72 条模糊清洗 Golden cases
python/                           Python 参考实现与 unittest
rust/                             Rust 实现与 Cargo 测试
scripts/                          生成器、三级 parity、一键验证
docs/decisions/                   设计决策记录
```

## 自包含合成数据

固定 seed 生成 240 条记录：四类别各 60，article/comment/reply 各 80，四个日期窗口并保留 1 条日期空值。主原始数据中的每条记录只出现一次；三种格式都包含多个类别、多个日期和三种记录类型：

| 格式 | article | comment | reply | 总计 |
|---|---:|---:|---:|---:|
| HTML | 26 | 27 | 27 | 80 |
| JSONL | 27 | 26 | 27 | 80 |
| TSV | 27 | 27 | 26 | 80 |

原始字段直接携带 `record_id`、`dataset_id`、`event_date`、`record_type`、`title`、`raw_text`、`category_signal`、`source_batch` 和 `synthetic`。解析器不根据 ID、正文关键词或真值文件猜测类别；`metadata.source_format` 来自文件格式，其余 metadata 来自原始字段。

19 组注入覆盖空/空白文本、零宽字符、NFKC、HTML、精确模板噪声、坏行、缺字段、schema drift、完全/近似重复、传递链、canonical 候选、日期缺口和类别特异信号。`fixtures/parser_cases/` 另行覆盖 JSON 数组、BOM、非法 UTF-8、未知扩展名、空文件、缺字段、malformed JSONL/TSV 及未知漂移，不计入 240 条主数据。

## 解析与 quarantine

映射语义如下：JSONL 每行一个对象；TSV 首行为固定列名，JSON 类型字段在单元格内编码；HTML 每个 `<article>` 的 `data-*` 属性携带完整字段；JSON 数组是小文件兼容路径。JSONL 和 TSV 逐条流式读取，HTML 按行处理单个 `<article>`，JSON 数组因语法需要整体载入，适用于受控小文件。

`source_file` 是相对输入根目录的 POSIX 路径。`source_offset` 是源文件中从 0 开始的数据记录序号：TSV 表头不计数，JSONL/HTML/JSON 数组从第一条数据开始计数。它不是字节偏移，也不因坏记录重新编号。

扫描识别 TSV、JSON、JSONL、HTML、UTF-8、UTF-8 BOM 和非法编码，按相对路径稳定排序并流式计算 SHA-256。解析结果按 `(source_file, source_offset)` 排序。坏行、非法编码、未知扩展名和未知 schema drift 写入 `QuarantineRecordV1`；错误包含稳定 code、可理解信息和原始片段哈希，一条坏记录不会阻断后续合法记录。输出不含本机绝对路径。

主 fixtures 实际解析为 240 条合法记录，quarantine 为 `invalid_json`、`malformed_tsv`、`missing_field`、`unknown_schema_drift` 各 1 条，并与 normalized truth 的全部 RecordV1 字段一致。

## 可复现抽样

跨语言不使用默认 RNG。每个记录的稳定排名为：

```text
SHA256(algorithm_version + US + decimal_seed + US + record_id)
```

按哈希升序、再按 `record_id` 升序选择，因此输入顺序不影响样本。`strata_keys` 支持顶层字段和 `metadata.<key>`；null 规范为 `__NULL__`，字段不存在规范为 `__MISSING__`。

支持 `simple_random`、`proportional`、`equal`、`minimum_then_proportional`。分层分配先遵守容量和最低量，再按最大余数法补齐；余数并列按规范化 stratum key 排序，小层最多全量纳入，总样本严格等于 target。target 超总体返回 `target_exceeds_population`，最低量不可行返回 `infeasible_minimum`，不会静默扩大 target。

sample report 记录每层总体量、配额和实际选择量，以及 seed、plan/population/selected ID/output fingerprints。Python 的高风险抽样依据空正文、缺标题/日期、解析 warning、短文本和已知精确模板标记，并输出 `risk_reasons`；它不读取 `injections.json`。配对样本按相同 ID 输出 before/after、规则 ID、是否变化和变化字符数。

## 确定性清洗与审计链

`configs/cleaning-v1.json` 规定唯一 priority，顺序为：

1. 移除 script/style block。
2. 保守解析 HTML 结构和实体。
3. 移除零宽字符。
4. Unicode NFKC。
5. 规范换行、行内空格和空行。
6. 清理配置中的 exact prefix/suffix/line/block。
7. 对空正文、纯符号、异常短文本标记 `review`。

同优先级配置返回 `duplicate_priority`。每个真实变化生成 `applied` CleaningEvent；no-op 不伪造 applied 事件。空、纯符号或短文本保留 RecordV1，仅产生 review 事件。HTML 规则整体移除 script/style，将 br/段落/块边界转为换行，解码命名和数字实体，并保留普通比较表达式中的 `<`。

事件记录 rule/version、match method、decision、matched span、移除字符数、before/after SHA-256、metrics 和 algorithm version。16 条 Golden Set 覆盖普通文本、空白、Unicode、HTML、script/style、段落、精确模板、负例、重复/近似重复和 canonical 候选；`syn-0001`～`syn-0006` 直接进入跨日期回归，但生产代码不特判 ID。

## 模糊模板清洗：边界与评分

`configs/fuzzy-cleaning-v1.json` 只描述已知模板，算法只比较文本前缀、后缀、独立行或配置锚定正则命中的短区域；候选最长 64 个 Unicode 字符。它不比较文章与文章，不用整篇正文召回候选，也不会在正文中部搜索相似短语。因此这里的“模糊”是模板轻微变体识别，不是文章级模糊去重。

模板和候选先进入同一 matching view：NFKC、ASCII 小写、空白折叠及冒号/分隔符邻接空格统一。`raw_text` 始终是原始证据，`clean_text` 是处理结果，matching view 只用于评分。NFKC 审计显示主数据 240 条中 231 条发生变化，字符数由 8,766 变为 8,770，最常见映射是 `，→,` 228 次、`：→:` 225 次；所有 `raw_text` 均未变化。规范化提高了全角模板召回，也会降低评分视图的标点保真度。

五类相似度全部输出 `0～10000` 整数基点，跨语言不使用浮点容差：

- Ratio：`10000 × (1 - Levenshtein / max_length)`，半数向上取整；Levenshtein 使用两行 DP。
- Partial Ratio：短串在受 64 字符上限保护的长串窗口中取最高 Ratio。
- Token Sort：ASCII 字母数字连续串转小写，CJK 字符逐字成 token，排序连接后计算 Ratio。
- Token Set：比较 token 交集、交集加左右差集的三种组合，取最高 Ratio。
- Character Jaccard：默认字符 3-gram 的交并比；短串以整串作为一个 gram。

默认组合权重依次为 25、20、15、15、25，总和必须为 100。自动判定还必须通过允许位置、候选/模板长度比例、至少两项独立证据、最佳与次佳模板分差和保护语境 gate；这些 gate 和全部组件基点都进入 `CleaningEventV1.metrics`。

三段决策为：`combined >= 0.95` 且全部 gate 通过才 `applied` 并删除精确 span；`0.60 <= combined < 0.95`，或达到自动阈值但证据/分差不足时进入 `review`；低分、正文中部、引用/讨论/否定语境、过短、长度异常、位置非法或歧义保护案例为 `skipped`。review/skipped 不修改文本，before/after hash 相同；多个 applied span 从右向左删除，重叠时按决策等级、分数、span 长度、template ID 选择。每个边界区域只保留最佳模板，避免日志膨胀。

72 条纯合成 fuzzy Golden cases 固定分为 calibration 36 条和 holdout 36 条；每个 split 的 applied/review/skipped 标签各 12 条，article/comment/reply 各 24 条、四日期各 18 条。calibration 在 19 个合法阈值组合中先要求自动误删为 0，再依次优化自动召回、review 捕获、无意义 review，并以更高 auto/review 阈值破平。最终选择 `review=0.60`、`auto=0.95`。holdout confusion matrix 为：applied `12/0/0`、review `0/11/1`、skipped `0/0/12`（列顺序 applied/review/skipped）；auto precision 和 recall 都是 1.0，review capture 为 11/12（0.9167），自动误删为 0。这只是合成 Golden Set 上的阈值验证，不代表真实生产数据的泛化性能。

`review-queue.jsonl` 保存需人工复核的候选、组件分数、gate 与原因。Python/Rust 对组件整数基点、最佳模板、span、decision、清洗文本、CleaningEvent、review queue 和确定性 pipeline manifest 做完整 parity。

## 指纹、幂等性和缓存

文件使用 streaming SHA-256；JSON 配置先 canonical serialization 再哈希。抽样记录 plan、population、selected ID 和 output fingerprint；pipeline 的 run manifest 记录输入、配置、计划及各输出指纹，不写时间和绝对临时路径。

清洗缓存命中必须同时满足 algorithm version、输入指纹、配置指纹、cache key、cleaned output hash 和 events output hash。文件缺失、输出被改或配置/输入变化都会重算。相同输入重复运行业务输出相同，对清洗后 RecordV1 再清洗内容和事件保持一致；缓存命中与未命中结果相同。

## 从零运行

要求 Python 3.11+、Rust 2021 edition 工具链和 Cargo。

```bash
cd corpus-preprocessing-core

python3 scripts/generate_fixtures.py \
  --seed 20260820 \
  --output-dir fixtures/generated \
  --parser-cases-dir fixtures/parser_cases

PYTHONPATH=python/src python3 -m corpus_preprocessing_core pipeline \
  --input fixtures/generated/raw \
  --parsing-config configs/parsing-v1.json \
  --cleaning-config configs/cleaning-v1.json \
  --sample-plan configs/sample-multifield-v1.json \
  --fuzzy-config configs/fuzzy-cleaning-v1.json \
  --output-dir /tmp/corpus-pipeline-python

cargo run --manifest-path rust/Cargo.toml -- pipeline \
  --input fixtures/generated/raw \
  --parsing-config configs/parsing-v1.json \
  --cleaning-config configs/cleaning-v1.json \
  --sample-plan configs/sample-multifield-v1.json \
  --fuzzy-config configs/fuzzy-cleaning-v1.json \
  --output-dir /tmp/corpus-pipeline-rust
```

两种 pipeline 都输出阶段 2 的 `file-manifest.jsonl`、`parsed-records.jsonl`、`quarantine.jsonl`、抽样、确定性清洗、事件、配对、报告和 `run-manifest.json`。传入 `--fuzzy-config` 后额外输出 `fuzzy-cleaned-records.jsonl`、`fuzzy-cleaning-events.jsonl`、`fuzzy-decisions.jsonl`、`fuzzy-review-queue.jsonl` 和 `fuzzy-decision-report.json`；不传时输出和行为与 `0.2.0` 相同。Python 另输出 `risk-sample.jsonl`。

独立运行 Golden Set 清洗和阈值评估：

```bash
PYTHONPATH=python/src python3 -m corpus_preprocessing_core fuzzy-clean \
  --input fixtures/golden/fuzzy-cleaning-v1.jsonl \
  --config configs/fuzzy-cleaning-v1.json \
  --output-dir /tmp/cpc-fuzzy-python

cargo run --manifest-path rust/Cargo.toml -- fuzzy-clean \
  --input fixtures/golden/fuzzy-cleaning-v1.jsonl \
  --config configs/fuzzy-cleaning-v1.json \
  --output-dir /tmp/cpc-fuzzy-rust

PYTHONPATH=python/src python3 -m corpus_preprocessing_core evaluate-fuzzy \
  --input fixtures/golden/fuzzy-cleaning-v1.jsonl \
  --config configs/fuzzy-cleaning-v1.json \
  --nfkc-input fixtures/generated/normalized/records.jsonl \
  --output /tmp/fuzzy-threshold-report.json
```

原有命令继续可用：

```bash
PYTHONPATH=python/src python3 -m corpus_preprocessing_core validate \
  --input fixtures/generated/normalized/records.jsonl
cargo run --manifest-path rust/Cargo.toml -- profile \
  --input fixtures/generated/normalized/records.jsonl \
  --output /tmp/profile-rust.json
```

## 全部验证

```bash
bash scripts/verify.sh
```

脚本检查 Python/Rust/Cargo 环境，双次生成主 fixtures 和 parser cases 并递归比较，解析五个 schema，运行 Python/Rust 全部测试、两种 validate、阶段 1 profile parity、阶段 2 完整 parity 和阶段 3 fuzzy parity。阶段 3 parity 真实运行两种 fuzzy-clean 和开启 fuzzy 的 pipeline，并逐字段比较五类整数基点、span、decision、清洗结果、CleaningEvent、review queue、指纹及 run manifest；同时执行 calibration/holdout 和 NFKC 审计断言。

### 本机验收记录（2026-08-21）

- Python 3.13.7、rustc 1.97.1、cargo 1.97.1。
- Python unittest 31/31（阶段 3 新增 10）；Rust 集成测试 25/25（阶段 3 新增 8）。
- 主数据 240 条完整还原；parser cases、10,000 条临时 JSONL/TSV、16 条确定性 Golden、72 条 fuzzy Golden 和四日期回归通过。
- Python/Rust 原 profile、阶段 2 scan/parse/sample/clean，以及阶段 3 score/decision/events/review/pipeline parity 均通过。
- `scripts/verify.sh` 连续两次退出 0；`rust/Cargo.lock` 已更新并保留。

## 当前边界与后续路线

`0.3.0` 已实现 ING-01～08、SAM-01～05、CLN-01～10 和 FCL-01～08。它没有实现 ING-09 的历史 schema 迁移、CLN-11 可选 LLM 银标、文章级精确/模糊去重、候选召回、重复簇、STAR clustering、canonical 选择、质量综合评分、三分类、类别消融、多数据集估计或交付审计。重复、近似重复和 canonical fixtures 仍只保证不被模板清洗误删。

下一仓库应为 `text-dedup-canonicalization`：先实现精确/模糊去重候选、strict STAR clustering 和 canonical 选择，再单独评估文章级相似度；不要把这些逻辑塞回模板清洗。

## 总索引与发布

该仓库属于 `respawn` 实习算法复现计划。远程总索引地址待总索引发布后回填。

设计依据见：[`0001-contract-first.md`](docs/decisions/0001-contract-first.md)、[`0002-self-contained-fixtures.md`](docs/decisions/0002-self-contained-fixtures.md)、[`0003-reproducible-sampling.md`](docs/decisions/0003-reproducible-sampling.md)、[`0004-deterministic-cleaning.md`](docs/decisions/0004-deterministic-cleaning.md)。
