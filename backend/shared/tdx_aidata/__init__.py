"""TdxAiData 行情接入（P6 T-P6-01）——免 Windows 的通达信数据通道。

结构：
- ``config``   配置唯一事实源（目录/开关/Token/套接字路径）
- ``protocol`` IPC 协议与纯函数口径（worker/client 共用）
- ``worker``   子进程单实例（chdir 隔离 + SDK 唯一引入口 + 冷却窗口管理）
- ``client``   父进程异步客户端（按需拉起 worker / 超时 / 错误映射）

纪律：全仓只有 ``worker.py`` 一处 import tqServer（源守卫测试约束）。
"""
