---
name: model-signal-eval
description: 模型标准化评估 — 测试集截面排序（RankIC/中性化/FM/分位）+ TopN 长多 + FF5 归因；滚动 3 个月动态体检、准入等级与闸门。用户问「这个模型能不能上/评估一下/排序怎么样/为什么 IC 低/长多行不行/模型准入」时使用。
---

# 模型标准化评估（model-signal-eval）

> 只回答两件事：**模型在测试集上排得准不准**，以及**落到可执行 TopN 长多后还剩多少**。
> 不负责训练、不负责调参、不承诺收益。

## 运行环境契约

- 命令在 `quantmind` 容器内执行：`docker exec -w /app quantmind python3 ...`。
- 数据：QuantDB `/data/quantdb`；模型产物 `/app/models/users/{tenant}/{user}/{model_id}/pred.parquet`。
- 评估报告落 `data/factor_research/{ff_attribution,model_signal,ff_validation}/`；UI 缓存落 `data/factor_research/model_signal/{model_id}/ui_63d.json`。

## 先看哪里

1. **前端（最快）**：模型详情 → 「测试集实盘力」面板（滚动 63 个有标签交易日；只算 test 切分）。
2. **API**：
   - `GET /api/v1/models/{model_id}/signal-eval` → ready/stale/missing/running + admission
   - `POST /api/v1/models/{model_id}/signal-eval/refresh` → 后台重算
3. **CLI（完整研究）**：
   ```bash
   # 截面排序 + TopN 长多（测试集）
   python backend/scripts/eval/model_signal_report.py \
     --model-id <model_id> --split test --top-ns 20,50,100 --horizon 5 --min-adv 2e7

   # FF5 归因（Top20/50/100，双口径）
   python backend/scripts/eval/ff_attribution.py \
     --model-id <model_id> --split test --start 2025-02-24 \
     --top-n 20 --top-ns 20,50,100 --bottom-n 20 --horizon 5 --cost-bps 15 \
     --sort-mode academic --orth-inv
   # 敏感性对照：同一命令把 --sort-mode 换成 monthly

   # 因子自检（溢价/相关/spanning/5×5）
   python backend/scripts/eval/ff_validate.py --start 2025-02-24 --end 2026-11-20 --sort-mode academic --orth-inv

   # 外部 FF5 CSV 交叉核对（央财/jh-factors，需自备 CSV）
   python backend/scripts/eval/ff_external_check.py --external <csv> --start ... --end ...
   ```

## 标准评估口径（必须一致）

| 项 | 口径 |
|---|---|
| 样本 | `split=test`；只算推理过的行；截面报告窗口 = 最后 63 个**标签完整**交易日 |
| 标签 | 模型训练口径（`daily_forward` 现算；默认 T+1 收盘 → T+H 收盘；以模型 `execution_price` 为准） |
| 过滤 | 20 日 ADV ≥ 2000 万、PIT ST 剔除 |
| IC | 原始/中性化（log 市值 + 行业）**同一批有效行**；HAC lag = horizon−1 |
| 长多 | T+1 开盘买、持 H、开盘涨停剔除、跌停顺延、双边 15bp |
| 基准 | 打分池等权 + 中证500/中证1000；FF5 归因另算 |
| FF5 | `sort-mode=academic`（6 月固定）+ `monthly` 两种；`--orth-inv` 默认开启；两种口径同过才算 α 存续 |

## 判读规则（硬阈值）

- **排序能力**：RankIC 均值 ≥0.02 且 HAC t≥2；中性化后 ≥0.01 且 t≥2；否则只能当风险分层。
- **分位**：单调性 ≥0.7 且 Q10−Q1 >0；否则分位不可用。
- **FM**：月末截面斜率 t≥2 且 |t|>2 月份占比 ≥50% 才算月频稳健。
- **IC 期限结构**：5 日 IC ≥ 1 日的一半（信息保留）；IC 随持有期上升 = 信号偏慢。
- **长多**：FF5 α>0 且 t≥2（academic+monthly 都过）；SMB 载荷显著 = 主要是小盘 beta。
- **容量**：TopN 等权、按最不流动持仓约束；5% 参与率中位即容量上限。
- **准入等级**：green=五闸全过；yellow=排序+中性化过、长多或稳定性未过；red=排序不成立；unknown=未算/过期；独立周期 <4 自动降 yellow。

## 准入闸门（防止未评估模型直接上线）

- 用户态三个入口：`POST /models/{id}/activate`、`PATCH /models/default`、`PUT /models/strategy-bindings/{id}`。
- red/unknown → **409**（`code=model_signal_eval_gate`）；前端二次确认后带 `force=true` 重试；yellow 放行但回传未过项。
- 每日 16:50 Celery `engine.tasks.model_signal_eval_refresh` 自动体检默认/策略绑定模型，red 或长多连续 5 次跑输发站内预警（red 才即时 QQ）。

## 常见陷阱

- `daily_forward.amount` 单位是**万元**，ADV 门槛（元）比较前必须 ×1e4。
- pred 尾部推理补推日没有 forward 标签：IC 窗口短于组合窗口；面板会分别显示 `end` 与 `pred_end`。
- 模型训练标签可能是 T+1 收盘（`deal_price=close`），长多模拟用 T+1 开盘；两者不可混用。
- `score_direction=reversed` 的模型训练 IC 是翻转前原值；展示/比较时必须用翻转后的有效 IC。
- 3 个月样本不估 FF5 α；不要用短窗口 α 做准入结论。
- 不要跨 transform / 跨因子口径直接比 IC；同一口径 A/B。

## 相关技能

- 训练/推理/回测全流程：`model-train-infer-backtest-report`
- 推理缺口补齐：`model-inference-backfill`
- 回测偏差审计（PBO/DSR）：`backtesting-bias-avoidance`
- 因子 IC 衰减/漂移：`factor-ic-decay`、`factor-drift-monitor`
