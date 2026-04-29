# AI-IDE 常见问题修复与 Skill 迁移手册

## 1. 目标与适用范围

本文档用于解决 QuantMind AI-IDE 在策略生成与执行阶段的高频错误，并给出可复制到开源版的最小改造方案。  
适用目录：

- `backend/services/engine/routers/ai_ide/`
- `backend/services/engine/ai_strategy/llm/`
- `electron/src/pages/AIIDEPage.tsx`

---

## 2. 本轮高频问题总览（按出现频率）

1. 生成了“示例教学代码”，不是平台可执行策略。
2. 生成代码引用不存在模块（如 `quantmind.api`、`qlib.contrib.signal`、`qlib.contrib.evaluate.backtest`）。
3. 数据读取路径错误（把 `/app/db/qlib_data` 当 CSV 目录，或使用 `path/to/your/data.csv`）。
4. Qlib 初始化遗漏（`NameError: qlib is not defined`）。
5. 前端接口报 404（`/api/v1/ai-ide/files/list`、`/execute/check-syntax`）。
6. 策略执行后只打印表格，没有收益指标（脚本缺少回测统计输出）。
7. 回测耗时长（股票池过大、每根 bar 重复计算、无默认 topK 限制）。

---

## 3. 根因分析

## 3.1 提示词缺少“硬约束”

原提示词偏“开放问答”，没有强制：

- 必须输出完整可运行脚本（含 `main()`）。
- 禁止输出占位路径、伪 API、教学注释模板。
- 必须打印收益指标（累计收益/年化/回撤/夏普/交易次数）。

结果是模型常输出通用教程，而非平台脚本。

## 3.2 缺少“策略类型分流”

模型策略与传统指标策略混在同一提示词内，模型容易混用范式：

- 本应 Qlib 配置式，却输出 pandas 本地 CSV 脚本。
- 本应传统 pandas 脚本，却引用 Qlib 不稳定接口。

## 3.3 缺少“错误记忆与就地纠偏”

没有把历史报错注入下一轮生成上下文，导致相同错误反复出现（如 `qlib` 未 import、错误模块路径、错误数据目录）。

## 3.4 部署同步机制漏传新文件

新增未追踪文件（`skill_engine.py`、`skill_templates/*`）若未进入同步列表，远端加载 router 时报 `ModuleNotFoundError`，最终表现为 API 404。

---

## 4. 已落地修复方案

## 4.1 Skill 工程化（强烈建议开源版保留）

新增：

- `backend/services/engine/routers/ai_ide/skill_engine.py`
- `backend/services/engine/routers/ai_ide/skill_templates/traditional_indicator_backtest.md`
- `backend/services/engine/routers/ai_ide/skill_templates/qlib_model_strategy_config.md`
- `backend/services/engine/routers/ai_ide/skill_templates/debug_guardrail.md`

作用：

- 按用户意图自动路由模板（模型策略 / 传统指标 / 调试防护）。
- 多模板叠加，既给主模板又给防错守卫。
- 注入历史报错，减少“同错重犯”。

## 4.2 AI-IDE Chat 路由注入

在 `chat.py` 的用户提示组装函数里注入 skill 内容（例如 `_format_user_prompt`）。

目标：

- 把“平台硬约束”前置给模型。
- 保证每轮生成都带运行环境信息。

## 4.3 前端错误提示兜底

在 `electron/src/pages/AIIDEPage.tsx` 统一处理 401/403/404/500 的 AI-IDE 接口错误提示，避免用户只看到 `Not Found`。

## 4.4 默认性能约束

在传统策略模板中增加默认规则：

- 默认股票池 `top100`。
- 优先一次性向量化计算，避免在 `on_bar` 内重复全窗口指标计算。
- 禁止无边界遍历全市场。

---

## 5. 开源版迁移步骤（可直接执行）

## 5.1 复制目录与关键文件

至少复制以下目录：

```bash
backend/services/engine/routers/ai_ide/skill_engine.py
backend/services/engine/routers/ai_ide/skill_templates/
```

建议同步以下调用点：

```bash
backend/services/engine/routers/ai_ide/chat.py
backend/services/engine/ai_strategy/llm/prompt_builder.py
electron/src/pages/AIIDEPage.tsx
```

## 5.2 启动前检查

```bash
python -m py_compile backend/services/engine/routers/ai_ide/skill_engine.py
python -m py_compile backend/services/engine/routers/ai_ide/chat.py
```

## 5.3 部署后检查（API）

```bash
curl -i https://<host>/api/v1/ai-ide/files/list
curl -i -X POST https://<host>/api/v1/ai-ide/execute/check-syntax
```

期望：

- 未登录返回 401/403（不是 404）。
- 登录态正常时返回 200 或业务错误 JSON（不是路由不存在）。

---

## 6. 策略生成硬规范（建议写入系统提示词）

1. 必须是“完整、可直接运行”的 Python 脚本。
2. 必须包含 `main()` 与 `if __name__ == "__main__": main()`。
3. 禁止使用占位路径，如 `path/to/your/data.csv`。
4. 禁止引用不存在模块（`quantmind.api`、`qlib.contrib.signal`、`qlib.contrib.evaluate.backtest`）。
5. 使用 Qlib 时必须显式 `import qlib` 并 `qlib.init(provider_uri="/app/db/qlib_data", region=REG_CN)`。
6. 传统指标脚本默认 `top100` 股票池，且打印收益指标。
7. 输出必须包含：累计收益、年化收益、最大回撤、夏普比率、交易次数。
8. 若读取失败，必须给出可读错误信息，不允许静默失败。

---

## 7. 典型错误 -> 修复动作对照表

## 7.1 `ModuleNotFoundError: No module named 'quantmind'`

原因：生成了非本平台运行时 API。  
修复：禁止 `quantmind.api`，改用 Qlib 或 pandas 方案。

## 7.2 `ModuleNotFoundError: No module named 'qlib.contrib.signal'`

原因：环境中无该模块。  
修复：用 pandas/ta 公式自行计算 MACD，或改为 Qlib 已验证接口。

## 7.3 `ImportError: cannot import name 'backtest' from qlib.contrib.evaluate`

原因：Qlib 版本 API 不兼容。  
修复：移除该 import，使用项目内统一回测封装或自定义轻量回测统计。

## 7.4 `NameError: name 'qlib' is not defined`

原因：缺少 `import qlib`。  
修复：在初始化函数前显式导入并调用 `qlib.init(...)`。

## 7.5 `FileNotFoundError: path/to/your/data.csv`

原因：生成了示例占位路径。  
修复：禁止占位路径；统一使用 Qlib 数据目录 `/app/db/qlib_data`。

## 7.6 AI-IDE 接口 404

原因：后端路由未加载（常见于新文件未同步）。  
修复：

1. 检查 `quantmind-engine` 日志是否 `No module named ...skill_engine`。
2. 同步新文件到服务器。
3. 重启 `quantmind-engine` 与 `quantmind-api`。

---

## 8. 回测慢的优化建议（默认策略）

1. 股票池上限默认 `100`（可配置）。
2. 指标向量化计算，不在逐 bar 内重复 `rolling/ewm` 全量计算。
3. 缩短回测区间用于迭代调试（先 3-6 个月，再全年）。
4. 先日频验证，再扩展更高频。
5. 统计输出与图表输出解耦，先保留核心指标。

---

## 9. 服务器同步与重启标准流程

> 仅当改动涉及 `backend/`、`website/`、Docker/环境配置时需要执行。

```bash
chmod +x deploy_live.sh
./deploy_live.sh
```

若怀疑漏同步新文件，手工补传并重启：

```bash
rsync -avz backend/services/engine/routers/ai_ide/ quantmind-server:/home/quantmind/backend/services/engine/routers/ai_ide/
ssh quantmind-server "docker restart quantmind-engine quantmind-api"
```

---

## 10. 回归用例（建议每次发布前执行）

1. 传统指标策略生成并执行：应输出 5 项收益指标。
2. 模型策略配置生成：应输出 `STRATEGY_CONFIG`，不出现占位路径。
3. 注入历史报错后再生成：不再复现同类错误。
4. 未登录访问 AI-IDE 接口：401/403。
5. 登录访问 AI-IDE 接口：200。

---

## 11. 建议的开源版目录结构

```text
backend/services/engine/routers/ai_ide/
  chat.py
  skill_engine.py
  skill_templates/
    traditional_indicator_backtest.md
    qlib_model_strategy_config.md
    debug_guardrail.md
```

---

## 12. 维护建议

1. 每新增一种常见报错，就在 `debug_guardrail.md` 增加“错误模式 -> 正确替代”。
2. 每次改 prompt 或 skill 后，保留 3 个固定 smoke case（模型策略、传统策略、报错修复重试）。
3. 部署脚本必须覆盖新建文件，否则优先使用 `rsync` 全量同步 `ai_ide` 目录。

