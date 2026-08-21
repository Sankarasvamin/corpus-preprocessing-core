# 0003：跨语言可复现抽样

## 已确认的设计

- 抽样排名固定为 `SHA256(algorithm_version + US + decimal_seed + US + record_id)`，再以 record ID 破同 rank；不依赖 Python/Rust 默认 RNG。
- 分层字段支持顶层和 `metadata.<key>`；null 与不存在分别规范为 `__NULL__`、`__MISSING__`。
- proportional、equal、minimum_then_proportional 共享容量约束、最大余数分配和规范化 key 的并列规则；不静默扩大 target。
- 报告保存总体、配额、实际选择量及 plan/population/selected/output 指纹。

## 当前合理假设

- `record_id` 是抽样单位的稳定唯一键；重复 ID 应先作为数据质量错误处理，而不是以出现顺序破同 rank。
- 过滤条件当前使用精确值或值列表，足以覆盖阶段 2 的合成回归。

## 后续需要校准

- 相似度区间抽样、加权总体估计和置信区间不在本阶段。
- 真实审阅预算、层级字段和配额下限需结合实习数据规模确定。
