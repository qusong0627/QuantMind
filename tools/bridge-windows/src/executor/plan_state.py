"""TradePlan 的持久化幂等状态（按交易日，原子落盘）。

旧实现只在内存 ``OrderedDict`` 里去重：桥重启后同一 plan_id 可以再执行一次。
A 股委托当日有效，plan 也是按日生成的；这里按日持久化 ``started/done``：

* ``started`` 后崩溃 -> 下次返回 ``unknown``，要求人工核对柜台，不自动重放；
* ``done/partial/rejected`` -> 返回 ``duplicate``；
* 新的一天 -> 清空，允许新 plan。

写盘失败必须 fail-closed：状态都落不下去，就不能保证重启后不重复下单。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime

from .order_tracker import atomic_write

log = logging.getLogger(__name__)

DEFAULT_FILENAME = "executed_plans.json"

DECISION_NEW = "new"
DECISION_DUPLICATE = "duplicate"
DECISION_UNKNOWN = "unknown"


class PlanStateStore:
    def __init__(self, state_dir: str, filename: str = DEFAULT_FILENAME):
        self.state_dir = state_dir or "."
        self.path = os.path.join(self.state_dir, filename)
        self._lock = threading.Lock()
        self._load_error = False
        self._doc = self._load()

    @staticmethod
    def today() -> str:
        return datetime.now().strftime("%Y%m%d")

    def _blank(self) -> dict:
        return {"date": self.today(), "plans": {}}

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return self._blank()
        except (OSError, ValueError) as exc:
            # 状态文件读不出来时不能当成"今天没有 plan"——那会在重启后重复下单。
            self._load_error = True
            log.error("plan state unreadable: %r", exc)
            return self._blank()
        if not isinstance(doc, dict) or doc.get("date") != self.today():
            return self._blank()
        if not isinstance(doc.get("plans"), dict):
            doc["plans"] = {}
        return doc

    def _rollover_locked(self) -> None:
        if self._doc.get("date") != self.today():
            self._doc = self._blank()

    def _save_locked(self) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        atomic_write(self.path, self._doc)

    def classify(self, plan_id: str) -> str:
        with self._lock:
            if self._load_error:
                return DECISION_UNKNOWN
            self._rollover_locked()
            entry = (self._doc.get("plans") or {}).get(str(plan_id))
            if not isinstance(entry, dict):
                return DECISION_NEW
            status = str(entry.get("status") or "")
            if status in ("started", "unknown"):
                return DECISION_UNKNOWN
            return DECISION_DUPLICATE

    def mark(self, plan_id: str, status: str, **fields) -> None:
        """写入状态；失败抛 OSError（调用方必须据此拒绝执行）。"""
        with self._lock:
            if self._load_error:
                raise OSError("plan state was unreadable; refusing to overwrite")
            self._rollover_locked()
            entry = {
                "status": str(status),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                **{k: v for k, v in fields.items() if v is not None},
            }
            self._doc.setdefault("plans", {})[str(plan_id)] = entry
            self._save_locked()

    def get(self, plan_id: str) -> dict | None:
        with self._lock:
            self._rollover_locked()
            entry = (self._doc.get("plans") or {}).get(str(plan_id))
            return dict(entry) if isinstance(entry, dict) else None
