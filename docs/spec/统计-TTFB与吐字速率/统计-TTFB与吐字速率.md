# 统计-TTFB与吐字速率

> **状态**：方案已定稿（2026-10-16；对齐参考实现 1.13.0 评审的 P2 项，用户「按建议来」纳入排期）
>
> **2026-10-17 归档注记**：本需求已并入《[可观测性数据底座](../可观测性数据底座/可观测性数据底座.md)》作为其 F5 子项（TTFB/tps/finish_reason），**不再单独实施**；字段、公式、验收以数据底座 spec 为准。
> **优先级**：交付（可与其他需求并批，改动自包含）
> **来源**：workbuddy2api-panel 1.13.0 对齐评审（`8b18ab8` issue #34 吐字速率扣 TTFB、`e05edb7` issue #127 200ms 下限退回端到端）；参考实现实测：同一响应「42.5 tok/s → 348.8 tok/s」
> **前置**：M8 Token 统计页（`duration_ms`/usage 落库链已具备）
> **关联**：未决项「真实请求反哺模型状态」——速率/首字等待是模型状态的一个证据面（本期只做观测与展示）

## 背景 / 目标

现有观测只有端到端 `duration_ms`（`oai_adapter.UsageCollector`），没有首 token 等待（TTFB）与吐字速率：

- 端到端时长把「上游排队/首字慢」与「生成慢」混在一起——换个上游 TTFB 从 0.3s 变 2s，同一模型的「速度」报告直接减半（参考实现 issue #34 的原始现象）；
- 无法横向比较「哪个模型/哪个域吐字快」，也无法回答「这次慢是排队还是生成」。

本期新增两个观测值并落库/上页：

1. **首 token 等待 `first_token_ms`**（TTFB）；
2. **吐字速率 `tokens_per_second`**（分母扣除 TTFB，带下限保护）。

## 设计方案

### 核心决策

| 决策 | 结论 |
|---|---|
| TTFB 口径 | 请求发出 → **首个非空产出帧**（`content` 或 `reasoning_content` 非空 delta；role 帧/零值 delta 不算——与我们空响应判定的「真实产出」口径同源）。非流式无分阶段概念：不产出 TTFB（留空） |
| 速率公式 | `tps = completion_tokens * 1000 / (duration_ms - ttfb_ms)`；`ttfb` 缺失/≤0 → 不扣（回落端到端）；`duration_ms - ttfb_ms < 200ms` → 回落端到端（假流式：上游攒批一次下发会除出上万 tok/s 幻数，issue #127）；`completion_tokens <= 0` → 不产出速率 |
| 分子口径 | `completion_tokens`（usage，含 reasoning_tokens——与账单/统计页既有口径一致） |
| 落库 | `logs` 表新增 `first_token_ms`、`tokens_per_second` 两列（`db.py` 启动自省补列，老库透明升级）；两个值在 `chat_done` 行写入 |
| 展示 | 日志页明细加两列（速率保留 1 位小数；缺失显示 `—`）；统计页指标卡加「平均吐字速率」（有值行均值）与「平均首字等待」 |
| 向后兼容 | 不改变既有 `duration_ms` 语义与任何 API 字段语义；新增字段可空 |

### 修改文件

```
relay/relay/oai_adapter.py        # UsageCollector：记录首个真实产出时刻 → first_token_ms
relay/relay/platforms/workbuddy/chat.py / custom_openai.py
                                  # 流式取首帧时刻（经 UsageCollector 统一；不各自实现）
relay/relay/db.py                 # COLUMN_DEFS 增两列（启动自省补列）
relay/relay/routes.py             # _log_db 写入 + /v1/stats 聚合（速率/首字均值）
relay/tests/test_m8_stats.py / test_m6_logs.py
                                  # 边界用例（keyless，MockTransport 定时帧）
frontend/src/views/Logs.vue       # 明细两列
frontend/src/views/Stats.vue      # 两张指标卡
```

## 已知边界

- 速率是「生成窗口」口径（扣 TTFB），与端到端时长（`duration_ms`）并存展示、不互相替代；
- 上游 usage 缺失时（部分错误路径）两值均缺失，显示 `—`；
- 假流式（攒批下发）一律回落端到端口径——宁可数字保守，不给幻数。

## 验收

- [ ] keyless：延迟首帧 + 定速后续帧 → `first_token_ms`/`tokens_per_second` 按公式断言（含 reasoning 首帧计入 TTFB）
- [ ] 边界：`ttfb<=0` 不扣 / `duration-ttfb<200ms` 回落 / `completion_tokens=0` 不产出 / 非流式无 TTFB
- [ ] 老库升级：无两列的既有 db 打开后自动补列（`test_db_schema_auto_add_column` 同款）
- [ ] 统计页均值卡与日志页明细列显示正常（format:check + build 绿）
