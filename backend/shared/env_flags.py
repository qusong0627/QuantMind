"""布尔环境变量的**唯一读取实现**。

同名环境变量有两种读法 = 一个变量两个答案。本仓已踩过：`ENABLE_REAL_TRADING`
在 `shared/live_trading_gate`（端点咽喉）与 `trade_shared/trade_config`（引擎据此
选券商）各写了一份实现，分叉矩阵是**实测**的（`docker exec` 内，逐值跑子进程）：

    raw         闸门     settings
    "true"      True     True
    " true "    True     ValidationError
    "\\ttrue"    True     ValidationError
    "true\\r"    True     ValidationError
    ""          False    ValidationError
    "1"         False    True
    "yes"       False    True
    "on"        False    True

三种后果都真实：闸门放行而 settings **崩**（模块级 `settings = Settings()` 在
import 时就炸）、闸门判关而 settings 判开（`_get_broker` 以为在实盘）。
**同一份变量不该有两个读者**——本模块存在，就是让「第二种读法」无处可写。

口径与词表都**按闸门侧定**，不按 pydantic 侧：

* 词表**只有 `true`**（`strip()` + 小写之后）。闸门是合规咽喉，它的词表是
  已经被接受的那一份；放宽词表等于让更多写法能打开实盘，方向错。pydantic 的
  bool 解析器认 `1`/`yes`/`on`，那是**它**的口径，不是实盘开关该有的口径。
* `strip()` 不是洁癖：本项目发 Windows 便携包（`.bat` 必须 ASCII+CRLF），
  CRLF 的 `.env` 会带出 `"true\\r"`，没有 strip 就是一个「设了没生效」的静默故障。

**不读 `.env` 文件**，只读进程环境：`.env` 由 docker-compose 注入进程环境后两者
等价，而 `os.getenv` 是本模块唯一依赖（不为了一个布尔把 pydantic 拖进 settings
模块以外的调用方）。副作用是「`.env` 里有、但没 export」时闸门判关、settings 判开
——这个方向的差是**安全**的（中间件 403 在前），故只记录不修。
"""

from __future__ import annotations

import os

#: 归一（`strip().lower()`）后可接受的**唯一**真值 token。
TRUE_TOKEN = "true"


def normalize_env_flag(raw: str | None, *, default: bool = False) -> bool:
    """把**原始字符串**按全项目唯一口径判成布尔；`None`（未设置）取 `default`。

    给 pydantic 的 `mode="before"` 校验器用（那里拿到的是字符串，还没被解析）。
    """
    if raw is None:
        return default
    return raw.strip().lower() == TRUE_TOKEN


def env_flag(name: str, *, default: bool = False) -> bool:
    """读取布尔环境变量。**调用时读**，不在 import 时冻结。

    不冻结是有意的：测试要能 monkeypatch，运维改 env 重启即生效，没有中间态。
    """
    return normalize_env_flag(os.getenv(name), default=default)
