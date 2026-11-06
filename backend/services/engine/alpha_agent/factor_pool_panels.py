"""Owner-scoped, quota-managed factor value panels for private pool research."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import stat
from pathlib import Path

import numpy as np
import pandas as pd

from backend.shared.owner_path import owner_segment, user_scoped_dir

logger = logging.getLogger(__name__)

_DEFAULT_ROOT = Path("/data/factor_research/panels")
SAMPLE_DAYS = 60
MAX_PANEL_ROWS = 1_200_000
_PANEL_COLUMNS = {"trade_date", "symbol", "rank_pct", "zscore"}


def panel_root() -> Path:
    configured = os.getenv("QM_FACTOR_POOL_PANEL_DIR")
    return Path(configured) if configured else _DEFAULT_ROOT


def _relative_path(market: str, factor_id: str, version: str | None = None) -> Path:
    if market != "a_share":
        raise ValueError("Only A-share factor panels are supported")
    if not isinstance(factor_id, str) or not factor_id.strip() or len(factor_id) > 256:
        raise ValueError("Invalid factor ID")
    digest = hashlib.sha256(factor_id.encode("utf-8")).hexdigest()
    if version is not None and (
        len(version) != 64 or any(char not in "0123456789abcdef" for char in version)
    ):
        raise ValueError("Invalid factor panel version")
    parts = [owner_segment(market), owner_segment(digest)]
    if version is not None:
        parts.extend((owner_segment(version), "panel.parquet"))
    return Path(*parts)


def panel_resource_key(
    tenant_id: str, user_id: str, market: str, factor_id: str, version: str
) -> str:
    relative = _relative_path(market, factor_id, version)
    return (
        f"factor_pool_panels/{owner_segment(tenant_id)}/"
        f"{owner_segment(user_id)}/{relative.as_posix()}"
    )


def _panel_path(
    tenant_id: str, user_id: str, market: str, factor_id: str, version: str
) -> Path:
    return user_scoped_dir(panel_root(), tenant_id, user_id).joinpath(
        *_relative_path(market, factor_id, version).parts
    )


def _safe_regular_file(path: Path) -> bool:
    root = panel_root().absolute()
    target = path.absolute()
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Factor panel path escaped its storage root")
    try:
        root_mode = os.lstat(root).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise ValueError("Factor panel root is not a safe directory")
    for directory in target.parents:
        if not directory.is_relative_to(root) or directory == root:
            continue
        try:
            mode = os.lstat(directory).st_mode
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError("Factor panel path contains a non-directory")
    try:
        target_stat = os.lstat(target)
    except FileNotFoundError:
        return False
    if (
        stat.S_ISLNK(target_stat.st_mode)
        or not stat.S_ISREG(target_stat.st_mode)
        or target_stat.st_nlink != 1
    ):
        raise ValueError("Factor panel is not a regular file")
    return True


def _canonical_panel(
    values: pd.Series, forward_returns: pd.Series | None = None
) -> pd.DataFrame:
    if not isinstance(values, pd.Series) or values.empty:
        raise ValueError("Factor values are empty")
    if not isinstance(values.index, pd.MultiIndex) or values.index.nlevels != 2:
        raise ValueError("Factor values require a date/instrument MultiIndex")

    from backend.shared.stock_utils import StockCodeUtil

    days = pd.to_datetime(values.index.get_level_values(0), errors="coerce")
    symbols = [
        StockCodeUtil.to_suffix(str(value))
        for value in values.index.get_level_values(1)
    ]
    frame = pd.DataFrame(
        {
            "trade_date": days.strftime("%Y-%m-%d"),
            "symbol": symbols,
            "value": pd.to_numeric(
                pd.Series(np.asarray(values)), errors="coerce"
            ).to_numpy(),
        }
    )
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["trade_date", "value"]
    )
    frame = frame.drop_duplicates(subset=["trade_date", "symbol"], keep="last")
    if frame.empty:
        raise ValueError("Factor values contain no finite observations")

    if forward_returns is not None:
        if (
            not isinstance(forward_returns, pd.Series)
            or not isinstance(forward_returns.index, pd.MultiIndex)
            or forward_returns.index.nlevels != 2
        ):
            raise ValueError("Forward returns require a date/instrument MultiIndex")
        returns = forward_returns.reindex(values.index)
        return_frame = pd.DataFrame(
            {
                "trade_date": days.strftime("%Y-%m-%d"),
                "symbol": symbols,
                "forward_return": pd.to_numeric(
                    pd.Series(np.asarray(returns)), errors="coerce"
                ).to_numpy(),
            }
        ).drop_duplicates(subset=["trade_date", "symbol"], keep="last")
        frame = frame.merge(
            return_frame, on=["trade_date", "symbol"], how="left", validate="one_to_one"
        )
        frame["forward_return"] = frame["forward_return"].replace(
            [np.inf, -np.inf], np.nan
        )

    all_days = sorted(frame["trade_date"].unique())
    if len(all_days) > SAMPLE_DAYS:
        positions = np.unique(
            np.linspace(0, len(all_days) - 1, SAMPLE_DAYS).round().astype(int)
        )
        selected_days = {all_days[int(position)] for position in positions}
        frame = frame[frame["trade_date"].isin(selected_days)]
    if len(frame) > MAX_PANEL_ROWS:
        raise ValueError("Sampled factor panel exceeds the row limit")

    grouped = frame.groupby("trade_date")["value"]
    rank_pct = grouped.rank(pct=True)
    zscore = grouped.transform(
        lambda series: (series - series.mean()) / (series.std(ddof=0) + 1e-12)
    )
    result = pd.DataFrame(
        {
            "trade_date": frame["trade_date"].to_numpy(),
            "symbol": frame["symbol"].to_numpy(),
            "rank_pct": rank_pct.to_numpy(dtype=np.float32),
            "zscore": zscore.to_numpy(dtype=np.float32),
        }
    )
    if "forward_return" in frame:
        result["forward_return"] = frame["forward_return"].to_numpy(dtype=np.float32)
    return result


def write_panel(
    tenant_id: str,
    user_id: str,
    market: str,
    factor_id: str,
    values: pd.Series,
    forward_returns: pd.Series | None = None,
) -> dict[str, str]:
    frame = _canonical_panel(values, forward_returns)
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False, engine="pyarrow")
    payload = buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    relative = _relative_path(market, factor_id, digest)

    from backend.shared.storage_quota_ledger import write_quota_managed_local_file

    write_quota_managed_local_file(
        root=panel_root(),
        tenant_id=tenant_id,
        user_id=user_id,
        relative_path=relative,
        content=payload,
        resource_namespace="factor_pool_panels",
    )
    return {"panel_ref": f"sha256:{digest}", "panel_version": digest}


def read_panel(
    tenant_id: str,
    user_id: str,
    market: str,
    factor_id: str,
    *,
    expected_version: str | None = None,
) -> pd.DataFrame | None:
    if expected_version is None:
        return None
    path = user_scoped_dir(panel_root(), tenant_id, user_id).joinpath(
        *_relative_path(market, factor_id, expected_version).parts
    )
    try:
        if not _safe_regular_file(path):
            return None
        if expected_version:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected_version:
                logger.warning("Factor panel version mismatch for private factor")
                return None
        frame = pd.read_parquet(path, engine="pyarrow")
        if not _PANEL_COLUMNS.issubset(frame.columns):
            logger.warning("Factor panel is missing required columns")
            return None
        if len(frame) > MAX_PANEL_ROWS:
            logger.warning("Factor panel exceeded the supported row limit")
            return None
        columns = ["trade_date", "symbol", "rank_pct", "zscore"]
        if "forward_return" in frame:
            columns.append("forward_return")
        return frame[columns]
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - a bad panel cannot fail factor research
        logger.warning(
            "Unable to read owner-scoped factor panel: %s", type(exc).__name__
        )
        return None


def delete_panel_version(
    tenant_id: str,
    user_id: str,
    market: str,
    factor_id: str,
    version: str,
) -> None:
    path = _panel_path(tenant_id, user_id, market, factor_id, version)
    if not _safe_regular_file(path):
        return
    path.unlink()
    from backend.shared.storage_quota_ledger import delete_local_file_usage

    delete_local_file_usage(
        tenant_id,
        user_id,
        panel_resource_key(tenant_id, user_id, market, factor_id, version),
    )


__all__ = [
    "MAX_PANEL_ROWS",
    "SAMPLE_DAYS",
    "delete_panel_version",
    "panel_resource_key",
    "panel_root",
    "read_panel",
    "write_panel",
]
