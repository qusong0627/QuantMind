# coding: utf-8
"""Callback-fed order watch table (issue #164).

QMT PUSHES order facts at us: order_callback carries the remark, the contract
id (m_strOrderSysID) and the status. The settlement path was polling
get_trade_detail_data on the adjust thread to learn exactly these -- measured
135 query rounds in 3.6s for one cancel of a nonexistent order (#151), and
62/64 lookup rounds for one submit (#122).

Each order event teaches two directions:

  remark -> order_sys_id   submit settlement: "which id did the order get?"
  order_sys_id -> status   cancel settlement: "did it reach 54?"

Written from QMT's C++ callback thread, read from the adjust thread: a plain
dict under a lock, no new threads, bounded FIFO + TTL (same shape as
_order_identity_local in redis_rpc). Callbacks only fire in live run mode --
a miss must always mean "ask QMT", so the polling path stays as the fallback.
"""

import collections
import threading
import time


class OrderWatchTable(object):
    MAX_ENTRIES = 5000
    TTL_SECONDS = 86400.0

    def __init__(self):
        self._lock = threading.Lock()
        self._by_remark = collections.OrderedDict()     # remark -> (ts, sysid)
        self._status_by_sysid = collections.OrderedDict()  # sysid -> (ts, status)

    def note(self, event):
        """Learn one normalized order event. Never raises (callback thread)."""
        try:
            remark = str(event.get("user_order_id") or event.get("remark") or "").strip()
            sysid = str(event.get("order_sys_id") or "").strip()
            status = str(event.get("status") or "").strip()
            if not sysid:
                return  # a pre-sysid event teaches nothing (#152's window)
            now = time.time()
            with self._lock:
                if remark:
                    self._by_remark[remark] = (now, sysid)
                    self._by_remark.move_to_end(remark)
                if status:
                    self._status_by_sysid[sysid] = (now, status)
                    self._status_by_sysid.move_to_end(sysid)
                while len(self._by_remark) > self.MAX_ENTRIES:
                    self._by_remark.popitem(last=False)
                while len(self._status_by_sysid) > self.MAX_ENTRIES:
                    self._status_by_sysid.popitem(last=False)
        except Exception:
            pass

    def sysid_for_remark(self, remark):
        """The order_sys_id QMT assigned to this remark, or None."""
        remark = str(remark or "").strip()
        if not remark:
            return None
        with self._lock:
            entry = self._by_remark.get(remark)
            if entry is None:
                return None
            ts, sysid = entry
            if time.time() - ts > self.TTL_SECONDS:
                self._by_remark.pop(remark, None)
                return None
            return sysid

    def stats(self):
        """Counts only -- how much the callbacks have taught this table.

        Remarks and order ids are order identifiers, so this reports sizes and
        nothing else. It exists because "is #164 actually live on this
        deployment" was otherwise unanswerable: the wiring happens in the
        strategy file, which reload_deployment cannot refresh, so a tree that
        has the code can still be running the old poll path until a restart.
        """
        with self._lock:
            return {
                "remarks": len(self._by_remark),
                "statuses": len(self._status_by_sysid),
                "max_entries": self.MAX_ENTRIES,
                "ttl_seconds": self.TTL_SECONDS,
            }

    def status_for_sysid(self, order_sys_id):
        """The latest status seen for this order_sys_id, or None."""
        sysid = str(order_sys_id or "").strip()
        if not sysid:
            return None
        with self._lock:
            entry = self._status_by_sysid.get(sysid)
            if entry is None:
                return None
            ts, status = entry
            if time.time() - ts > self.TTL_SECONDS:
                self._status_by_sysid.pop(sysid, None)
                return None
            return status
