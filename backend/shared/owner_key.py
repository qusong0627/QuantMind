"""Lossless identity components for shared Redis keys."""

from urllib.parse import quote


def owner_key_component(value: str) -> str:
    if not value:
        raise ValueError("Owner identity cannot be empty")
    return quote(str(value), safe="")
