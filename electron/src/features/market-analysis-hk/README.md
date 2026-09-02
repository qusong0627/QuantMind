# 港股市场分析模块 (Market Analysis HK)

## 模块说明
港股市场多维分析：恒指四大指数脉搏、港股通南向资金穿透、CCASS 中央结算系统
席位透视（港股独有）、高股息/低估值主题、恒生行业轮动与 AH 对应股联动。
数据全部来自本地 QuantHK parquet（`QM_QUANTHK_DATA_DIR`，默认 `data/quanthk/`），
无外部实时行情依赖。

## 市场特色设计（区别于 A 股市场分析）
| 维度 | 港股口径 |
|------|---------|
| 涨跌停 | 港股无涨跌停 → 快涨/快跌阈值取 ±5% |
| 行业体系 | 恒生行业分类（akshare_profile 全市场 2700+ 只），非申万 |
| 资金穿透 | 南向持股（hsgt_south 日频）+ CCASS 席位（ccass_top50 / ccass_factors） |
| 主题榜单 | 高股息率 / 低 PE-TTM / 低 PB-MRQ（akshare 快照） |
| 两地联动 | AH 对应股（ah_membership）同日涨跌对照 |

## 目录结构（独立市场目录 + 共享层）
```
electron/src/features/market-analysis-hk/
├── pages/MarketAnalysisHkPage.tsx    # 主页面（5 Tab）
├── components/
│   ├── HkIndexCards.tsx              # 恒生 4 大指数卡
│   ├── HkBreadthCard.tsx             # 市场温度计（±5% 口径）
│   ├── HkSouthPanel.tsx              # 南向总览 + 增持/减持榜 + 板块配置
│   ├── HkCcassPanel.tsx              # CCASS 集中度榜 + 个股席位下钻 + 席位异动
│   ├── HkValuationPanel.tsx          # 高股息 / 低PE / 低PB 三榜
│   ├── HkRotationPanel.tsx           # 行业 1/5/20 日轮动
│   ├── HkAhPanel.tsx                 # AH 对应股联动
│   └── shared/ui.tsx                 # HK 内共享小组件
├── services/api.ts                   # /api/v1/market-analysis-hk 封装
└── types.ts                          # 响应类型（与后端对齐）
```
跨市场共享：热力图 treemap 复用 A 股
`features/market-analysis/components/ShenwanHeatmapChart.tsx`
（数据形状 name/value/pct_change/leader 一致）。

## 后端
- `backend/services/api/market_analysis_hk/` — 港股市场目录（quanthk_feed + router）
- `backend/services/api/market_analysis_shared/` — 跨市场共享层（缓存/交易日历/名称/展示）
- 测试：`backend/tests/test_quanthk_market_analysis.py`（真实数据集成断言）

## 数据口径要点
- 日线为不复权原始价（akshare stock_hk_daily），涨跌幅 = close_t/close_{t-1}-1，
  特殊公司行动（>±100%）已裁剪防失真
- 南向主力布局为 `hsgt_south/dt=YYYYMMDD/`（分区式，日频）；旧「每股一文件」
  布局为遗留兜底
- CCASS 为 T 日收盘披露，show 生成日与 kline 可能差 1 个交易日
- 估值/股息为用户为快照（published_at 标注），非逐日时间序列