from .tools import *


class LocalDatas(base):
    """本地 CSV 数据索引（自动生成，请勿手改）。

    * 名字来自两个目录：``~/.minibt/datas``（用户数据，可写）与
      ``minibt/data/test``（包内基础数据，随包分发、只读）；
    * 同名时用户目录优先（盖上包内那份，包内文件不动）；
    * **想让某个名字从这里消失，必须删掉对应的真实文件**：
      用户目录用 ``LocalDatas.deleter('名字')`` 或
      ``LocalDatas.keep('名字')``；只改本文件没用，
      下一次 ``rewrite()`` 会把它原样生成回来；
    * 增删 CSV 后 ``LocalDatas.rewrite()`` 会重新生成本文件
      （``get_kline(..., save='名字')`` 会自动调用）；
    * 打包前（``python -m build``）先跑一次
      ``LocalDatas.rewrite(base_only=True)``（= ``reset_index()``）：
      只列包内基础数据，免得把本机 ``~/.minibt/datas`` 里的
      私人数据名写进 wheel；它只改索引文件、不删你的数据。
    """

    l2609_60 = DataString("l2609_60")
    pp2609_60 = DataString("pp2609_60")
    test = DataString("test")


LocalDatas = LocalDatas()
