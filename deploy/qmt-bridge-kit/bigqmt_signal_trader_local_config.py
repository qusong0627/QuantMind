# coding: utf-8
"""QMT 端私有配置（只改这一个文件，不要提交 git）。

放在 QMT 的 python 目录下，与 BIGQMT_REDIS_DRYRUN.py 同级。
"""

# QMT 资金账号 —— 必须与 QuantMind 页面里的 account_id 完全一致
BIGQMT_ACCOUNT_ID = "在这里填资金账号"

# 账号类型 —— 服务端只认这里的值（页面上填的 account_type 传不过去）。
# 普通账户 STOCK；两融/信用账户必须改 CREDIT，否则服务端按 STOCK 查询，
# 信用账户会返回「资产全 0」而不是报错。
BIGQMT_ACCOUNT_TYPE = "STOCK"

BIGQMT_REDIS_CONFIG = {
    # 桥 Redis（建议独立实例，只监听局域网 + 密码）
    "host": "在这里填 Redis 地址",
    "port": 6380,
    "db": 0,
    "password": "在这里填 Redis 密码",

    # ★ 下单开关：先保持 False 验通只读链路，确认风控后再改 True
    "rpc_allow_order_methods": False,

    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    # redis 传输保持 True；换 zmq/pipe 必须改 False（上游实测 zmq 差 37 倍）
    "rpc_background_threads": True,
    "schedule_adjust": True,
    "schedule_adjust_interval": "100nMilliSecond",
}
