"""Builtin Qlib metadata only; user pool files never enter public sandboxes."""

from pathlib import Path

from backend.shared.stock_pool.builtins import cn_index_symbols
from backend.shared.stock_utils import StockCodeUtil


def public_instrument_files() -> dict[str, str]:
    return {"all_a": "all.txt", **{name: name + ".txt" for name in cn_index_symbols()}}


def builtin_universe_symbols(provider: str | Path) -> dict[str, list[str]]:
    result = {}
    for name, filename in public_instrument_files().items():
        path = Path(provider) / "instruments" / filename
        if path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Builtin instrument file exceeds the metadata limit")
        symbols = sorted(
            {
                StockCodeUtil.to_qlib(line.split()[0])
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
        )
        if not symbols or len(symbols) > 20_000:
            raise ValueError("Builtin instrument list is empty or oversized")
        result[name] = symbols
    return result
