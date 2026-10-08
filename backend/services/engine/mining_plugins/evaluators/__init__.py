"""内置评估器打包：导入即注册（顺序即前端展示顺序）。

顺序 IC 族 → RRE → PFS 族 → 换手/成本 → 毛收益族，与金样 registry 段一致——
改顺序前端指标表列序会跟着变。
"""

from . import ic_family  # noqa: F401
from . import reliability  # noqa: F401
from . import pfs  # noqa: F401
from . import turnover_cost  # noqa: F401
from . import legacy_return  # noqa: F401
