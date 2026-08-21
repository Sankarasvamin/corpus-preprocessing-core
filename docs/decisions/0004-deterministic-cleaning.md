# 0004：保守的确定性清洗与事件审计

## 已确认的设计

- `raw_text` 永不修改；每条记录保留，结果写入 `clean_text`。
- 配置中的 rule priority 必须唯一；script/style、HTML、零宽、NFKC、空白、精确模板、风险 flag 依次执行。
- 每次真实变化产生 CleaningEvent；no-op 不伪造 applied，空/纯符号/短文本产生 review。
- before/after 哈希、规则版本、match method、decision 和 metrics 共同构成可重放审计链。
- 16 条 Golden Set、`syn-0001`～`syn-0006` 和四日期样本用于跨语言、负例和幂等回归。

## 当前合理假设

- 精确 prefix/suffix/line/block 和保守锚定规则足以作为阶段 2 安全边界；相似但未精确命中的模板应保留。
- 缓存只在输入、canonical 配置、算法版本和两份输出哈希均匹配时命中。

## 后续需要校准

- 真实模板库、异常短文本阈值和人工复核理由需要用实习样本校准。
- 模糊模板识别、阈值敏感性和 LLM 银标属于后续阶段，本决策不预设实现。
