"""Qlib adapter for the prefork-shared raw daily arrays."""
# OSS port: 与企业版逐字一致。

from qlib.data.data import LocalFeatureProvider

from .memory_feature_cache import CACHE


class MemoryFeatureProvider(LocalFeatureProvider):
    def feature(self, instrument, field, start_index, end_index, freq):
        result = CACHE.feature(instrument, field, start_index, end_index, freq)
        if result is not None:
            return result
        return super().feature(instrument, field, start_index, end_index, freq)
