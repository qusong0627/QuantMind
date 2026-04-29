用途：当用户提供报错日志时，优先做“最小修复”。

诊断流程：
1) 先识别报错类型（ImportError/ModuleNotFoundError/FileNotFoundError/KeyError 等）。
2) 明确指出根因行与修复方案，不给泛化建议。
3) 禁止新增无关依赖，禁止大规模重构。
4) 修复后保持原策略意图不变，并给可运行版本。

常见修复规则：
- FileNotFoundError: 移除占位路径，改为 qlib + D.features 或加路径存在性判断。
- ImportError: 删除不存在的导入，保留最小必要 import。
- 链式赋值告警: 改为 .loc 赋值。
- 收益计算异常: 使用 position.shift(1)、对零波动夏普返回 0。

专项错误护栏（高频）：
1) `NameError: name 'qlib' is not defined`
   - 根因：调用 `qlib.init(...)` 但未 `import qlib`
   - 修复：补充 `import qlib`，并删除未使用导入
2) `ImportError: cannot import name 'backtest' from qlib.contrib.evaluate`
   - 根因：版本不兼容或错误导入
   - 修复：删除该导入，若仅做简易回测，使用 pandas 本地回测逻辑
3) `/app/db/qlib_data/*.csv` 路径错误
   - 根因：把 Qlib 数据目录误当 CSV
   - 修复：统一改为 `qlib.init + D.features`

输出前强制自检（必须逐条满足）：
- 已包含 `import qlib`
- 已包含 `main()` 入口
- 不含 `path/to/your/data.csv` 与 `/app/db/qlib_data/*.csv`
- 不含 `from qlib.contrib.evaluate import backtest`
- 收益计算使用 `position = signal.shift(1)`
