# ADR-0008 契约自愈 + 启动期统一 ensure（老库缺列防线）

- 状态：已采纳（事故驱动）
- 背景：老库升级后新列缺失，契约自愈只挂在**写入路径** → 读任务刷 UndefinedColumn
  （老库缺列事故，commit 374911d5）；自愈迁移里带 DROP 的被守卫跳过属预期。
- 决定：trade 启动期统一跑六契约 ensure（signal/ledger/order/price/sentinel/anomaly）；
  自愈只做增量（ADD COLUMN/索引），破坏性变更走 `data/upgrade_*.sql` 显式执行；
  便携包同步勿漏 upgrade_*.sql。
- 后果：新契约列必须进启动期 ensure 清单 + 对应回归测试。
