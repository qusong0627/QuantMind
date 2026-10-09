# 方法论与口径（Brinson 业绩归因）

## 单期分解

行业 i 的主动权重 = w_p,i − w_b,i；基准总收益 R_b = Σ w_b,i·r_b,i；组合总收益 R_p = Σ w_p,i·r_p,i。

| 效应 | Fachler（默认） | BHB |
|---|---|---|
| 配置 Allocation | (w_p−w_b)·(r_b − R_b) | (w_p−w_b)·r_b |
| 选股 Selection | w_b·(r_p − r_b) | 同 |
| 交互 Interaction | (w_p−w_b)·(r_p − r_b) | 同 |

Fachler 把「配置到一个本身强于/弱于大盘的行业」与「选中好行业」分开；BHB 把行业自身收益全部记入配置。两者对行业分散度不同的组合会给出不同读法，切换方法时连同结论一起说明。

**恒等式**：三效应逐行业求和 ≡ R_p − R_b（当 Σw_p = Σw_b = 1），与 r_b 取值约定无关。残差 = active − Σ(三效应)，门禁 `abs_residual<1bp`（1e-4）。权重和偏离 1 时恒等式引入 R_b·(Σw_p−Σw_b) 的残差——这就是「残差≠0」的第一嫌疑。

## 多期链接（Carino 1999）

逐期算术效应不能直接相加对上几何主动收益，用逐期缩放因子：

- k_t = ln((1+r_p,t)/(1+r_b,t)) / (r_p,t − r_b,t)；r_p,t = r_b,t 时取极限 1/(1+r_b,t)。
- scale = A_g / Σ k_t·(r_p,t − r_b,t)，A_g = Π(1+r_p) − Π(1+r_b)；Σ k_t·active_t ≈ 0 时 scale=1（源行为）。
- c_t = scale·k_t；`effect_linked = Σ c_t·effect_t`；`residual_linked = A_g − Σ linked`（数值精度内 ≈0，门禁同上）。

链接后 headline 收益/效应为几何口径；行业明细与 HHI 为**最后一期**快照（源契约）。`linked.active_return_arithmetic_sum` 与几何值之差即复利间隙。

## 质量门禁与 status

| gate | 含义 |
|---|---|
| weights_sum_near_1 | w_p、w_b 各自和落在 1±0.02 |
| abs_residual<1bp | 主动收益被三效应解释（多期另有 abs_residual_linked<1bp） |
| active_return_explained | 与残差门禁同判据（源契约保留双键） |
| has_dispersion | 行业收益存在离散度（单行业输入时该门禁失败） |

`score` = 门禁通过率。报告 `status`：残差门禁失败 → `fail`；其余门禁失败或有 high/critical findings → `warning`；校验错误（缺列、权重和越界、空输入等）→ `insufficient-evidence`（不出数，`analysis_skipped`）。

## 解释规则

- `verdict` = 绝对贡献最大的效应（ALLOCATION / SELECTION / INTERACTION）；`top_contributors` = |total| 最大的三个行业（`行业:total`）。
- 组合缺某行业：r_p 取 r_b（该行业选股/交互必为 0，配置用 w_p−w_b 照常）。基准缺组合所持行业：r_b 取 R_b（中性），报 medium finding。
- HHI = Σw²（**行业级**权重，源契约）；越大越集中。等权大盘基准的行业 HHI 由行业数量结构决定，不是个股集中度。

## 数据口径（本地 QuantDB，2026-10-07 标定）

- 收益 = 窗口内首/末有效收盘之比 − 1。CN `daily_forward`=前复权（含分红再投）；HK/US `daily_forward`=不复权（价格收益，不含分红）；CN `daily_backward` 损坏，禁止用于收益。
- 行业映射：US=`quantus/2_base_sector/sector`（sector/industry 列）；HK=`quanthk/2_base_sector/akshare_profile.所属行业`（sector/ 目录 sector 列 2818/2818 全空）；CN=`instrument_detail.rs_hyname`（快照 HqDate=20260720）。
- 基准（缺省）= 行业映射 ∩ 窗口有行情 的标的等权；剔除数量在 findings（info）。组合/基准权重容差内归一化（原始和入 findings），越界拒绝。
- HK K 线 2024-09-02~2026-05-08 存在双来源重复行；读取侧按 `(symbol,time)` 保留 `published_at` 最新行并报 info。

## 校准记录（2026-10-07）

- **对拍**：与按源方法论独立重写的 numpy 向量化实现比对（demo 数据 + 200 组随机数据 × Fachler/BHB），worst |diff| = 6.9e-18，0 失配。
- **源样例复现**：`--demo`（源 run_demo CASE B 三期）输出 R_p=15.8146%、R_b=10.7640%、active=5.0505%、allocation_linked=2.6096%、selection_linked=2.4409%、interaction_linked=−0.0000%、residual_linked=0.0000%，与源实现打印一致。
- **US 冒烟**（2026Q2，30 只倾斜组合 vs 487 全集）：R_p=7.6619%、R_b=10.2560%、residual=−5.6e-17（status=pass）；基准收益用两边界交易日独立复算 = 10.255979%，与脚本一致。
- **多期冒烟**（2026Q2+Q3）：active_geometric=6.4121%（算术和 5.7252%）、carino_factors=[1.0218, 1.0894]、residual_linked=−6.9e-17。
- **HK/CN 冒烟**（2026-09）：HK 残差 −6.9e-18（31 行业）；CN 残差 3.5e-17（128 行业）；US `--level industry` 残差 0（108 行业）。

## 参考

- Brinson, Hood, Beebower (1986), "Determinants of Portfolio Performance"。
- Brinson, Fachler (1985), "Measuring Non-US Equity Portfolio Performance"。
- Carino (1999), "Combining Attribution Effects Over Time"。
