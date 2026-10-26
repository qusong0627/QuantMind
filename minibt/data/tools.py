from __future__ import annotations
import os
import glob
import shutil
from typing import TYPE_CHECKING
from pandas import DataFrame, read_csv
from ..other import time_to_datetime


__all__ = ["base", "DataString", "datas_dirs", "user_datas_dir",
           "list_csv_names", "package_csv_names", "get_csv_path",
           "ensure_user_copy", "import_package_data", "rewrite_datas",
           "reset_index"]
if TYPE_CHECKING:
    from .utils import LocalDatas
    from ..indicators import KLine

#: 包内**只读**基础数据目录（随包分发；开箱即用 + 测试用）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_DATAS = os.path.join(BASE_DIR, "test")
_kline = None


# ------------------------------------------------------------------ 目录解析
def user_datas_dir() -> str:
    """## 用户数据目录 ``~/.minibt/datas``

    与 ``setting.json`` 同目录（Windows 即 ``C:\\Users\\<你>\\.minibt\\datas``），
    可用环境变量 ``MINIBT_SETTINGS`` 覆盖（指向目录或 ``.../setting.json``）。
    用户自己的 CSV 放这里；同名文件会**覆盖**包内基础数据。
    """
    try:
        from ..utils import user_dir
        root = user_dir()
    except Exception:                      # 独立运行/循环导入兜底
        env = os.environ.get('MINIBT_SETTINGS')
        if env:
            p = os.path.expanduser(env)
            root = p if not os.path.splitext(p)[1] else os.path.dirname(
                os.path.abspath(p))
        else:
            root = os.path.join(os.path.expanduser('~'), '.minibt')
    return os.path.join(root, 'datas')


def datas_dirs() -> list[str]:
    """数据目录列表（**按优先级**）：用户目录 → 包内目录。"""
    return [user_datas_dir(), _PACKAGE_DATAS]


def list_csv_names() -> list[str]:
    """扫描两个目录里的全部 CSV 名（去重、排序）。"""
    names: set[str] = set()
    for d in datas_dirs():
        if os.path.isdir(d):
            for f in glob.glob(os.path.join(d, "*.csv")):
                names.add(os.path.splitext(os.path.basename(f))[0])
    return sorted(names)


def package_csv_names() -> list[str]:
    """只要**包内**基础数据的名字（``minibt/data/test``）。

    打包前复位 ``LocalDatas`` 索引用（见 :func:`reset_index`），避免把本机
    ``~/.minibt/datas`` 里的私人数据名写进 wheel。
    """
    names: set[str] = set()
    if os.path.isdir(_PACKAGE_DATAS):
        for f in glob.glob(os.path.join(_PACKAGE_DATAS, "*.csv")):
            names.add(os.path.splitext(os.path.basename(f))[0])
    return sorted(names)


def get_csv_path(name: str) -> str:
    """按优先级返回某个数据的 CSV 路径（用户目录优先，包内兜底）。

    两边都没有时返回**用户目录**下的路径（方便调用方直接写入）。
    """
    for d in datas_dirs():
        path = os.path.join(d, f"{name}.csv")
        if os.path.exists(path):
            return path
    return os.path.join(user_datas_dir(), f"{name}.csv")


def ensure_user_copy(name: str) -> str:
    """确保 ``~/.minibt/datas/{name}.csv`` 存在（缺失时从包内复制一份），返回该路径。"""
    user = os.path.join(user_datas_dir(), f"{name}.csv")
    if not os.path.exists(user):
        src = os.path.join(_PACKAGE_DATAS, f"{name}.csv")
        if os.path.exists(src):
            os.makedirs(user_datas_dir(), exist_ok=True)
            shutil.copyfile(src, user)
    return user


def import_package_data(*names: str, overwrite: bool = False) -> list[str]:
    """把包内基础数据**显式**导出到 ``~/.minibt/datas``（想要完整可改副本时用）。

    ``import_package_data()`` 导出全部；``import_package_data('test')`` 只导出指定项。
    ``overwrite=False``（默认）时不覆盖已存在的用户文件。
    """
    want = set(names) or None
    out: list[str] = []
    for src in glob.glob(os.path.join(_PACKAGE_DATAS, "*.csv")):
        stem = os.path.splitext(os.path.basename(src))[0]
        if want is not None and stem not in want:
            continue
        dst = os.path.join(user_datas_dir(), f"{stem}.csv")
        if overwrite or not os.path.exists(dst):
            os.makedirs(user_datas_dir(), exist_ok=True)
            shutil.copyfile(src, dst)
        out.append(dst)
    return out


# ------------------------------------------------------------------ 数据对象
class DataString(str):

    @property
    def dataframe(self) -> DataFrame | None:
        # 用户目录优先，包内兜底
        path = get_csv_path(str(self))
        if not os.path.exists(path):
            path = str(self)               # 也支持直接传绝对路径
        if not os.path.exists(path):
            return
        df = read_csv(path, index_col=0).reset_index()
        try:
            df.datetime = df.datetime.apply(time_to_datetime)
        except Exception:
            print(f"本地数据{self}的时间数据无法处理")
        return df

    def __kline(self):
        global _kline
        if _kline is None:
            from ..indicators import KLine
            _kline = KLine
        return _kline

    @property
    def kline(self) -> KLine:
        if self.dataframe is None:
            return
        return self.__kline()(self.dataframe, height=450)


class base:

    def update(self) -> LocalDatas | base:
        """重新生成 ``minibt/data/utils.py``（``LocalDatas`` 索引）。"""
        self.rewrite()
        return self

    def deleter(self, *args) -> LocalDatas | base:
        """删除 ``~/.minibt/datas`` 里的目标文件（只动用户目录）。"""
        for name in args:
            path = os.path.join(user_datas_dir(), f"{name}.csv")
            if os.path.exists(path):
                os.remove(path)
        self.rewrite()
        return self

    def keep(self, *args) -> LocalDatas | base:
        """只保留 ``args``，其余**用户目录**里的文件删除（包内基础数据不受影响）。"""
        if not args:
            return self
        keep = set(args)
        for name in list_csv_names():
            if name in keep:
                continue
            path = os.path.join(user_datas_dir(), f"{name}.csv")
            if os.path.exists(path):
                os.remove(path)
        self.rewrite()
        return self

    def rename(self, old_name: str = "", new_name: str = "") -> LocalDatas | base:
        """重命名（先把包内同名的**懒拷贝**到用户目录，再改名）。"""
        if all([old_name, new_name]) and all(
                [isinstance(old_name, str), isinstance(new_name, str)]):
            old_path = ensure_user_copy(old_name)
            if os.path.exists(old_path):
                os.rename(old_path, os.path.join(
                    user_datas_dir(), f"{new_name}.csv"))
                self.rewrite()
        return self

    @staticmethod
    def rewrite(check: bool = False, base_only: bool = False):
        """重新生成 ``minibt/data/utils.py``（``LocalDatas`` 的索引）。

        把 ``~/.minibt/datas``（用户数据）与 ``minibt/data/test``（包内基础
        数据）两个目录里的 CSV 名字写成 ``LocalDatas`` 的类属性，这样用户能
        一眼看到有哪些本地数据可调用（``LocalDatas.pp2609_60`` …）。

        ``check=True`` 时先比较现有类属性，集合一致就**不重写**（避免每次
        ``import minibt`` 都写文件）。``get_kline(..., save='名字')`` 保存完
        数据后会自动调用本方法。只读安装下写不进去也不报错，退回动态扫描。

        ``base_only=True``：只列**包内基础数据**，忽略用户目录 —— 打包前
        复位索引用（见 :func:`reset_index`）。它**只影响索引文件**，不会删
        任何文件；要让某个名字从 ``LocalDatas`` 里消失，得用 ``deleter`` /
        ``keep`` 删掉 ``~/.minibt/datas`` 里那个真实文件。
        """
        source = package_csv_names() if base_only else list_csv_names()
        names = [n for n in source if n.isidentifier()]
        current: set[str] | None = None
        try:
            from .utils import LocalDatas as _current
            current = {k for k in _current.__class__.__dict__
                       if not k.startswith('_')}
        except Exception:
            current = None
        if check and not base_only and current is not None \
                and current == set(names):
            return None
        lines = [
            'from .tools import *',
            '',
            '',
            'class LocalDatas(base):',
            '    """本地 CSV 数据索引（自动生成，请勿手改）。',
            '',
            '    * 名字来自两个目录：``~/.minibt/datas``（用户数据，可写）与',
            '      ``minibt/data/test``（包内基础数据，随包分发、只读）；',
            '    * 同名时用户目录优先（盖上包内那份，包内文件不动）；',
            '    * **想让某个名字从这里消失，必须删掉对应的真实文件**：',
            "      用户目录用 ``LocalDatas.deleter('名字')`` 或",
            "      ``LocalDatas.keep('名字')``；只改本文件没用，",
            '      下一次 ``rewrite()`` 会把它原样生成回来；',
            '    * 增删 CSV 后 ``LocalDatas.rewrite()`` 会重新生成本文件',
            "      （``get_kline(..., save='名字')`` 会自动调用）；",
            '    * 打包前（``python -m build``）先跑一次',
            '      ``LocalDatas.rewrite(base_only=True)``（= ``reset_index()``）：',
            '      只列包内基础数据，免得把本机 ``~/.minibt/datas`` 里的',
            '      私人数据名写进 wheel；它只改索引文件、不删你的数据。',
            '    """',
            '',
        ]
        for name in names:
            lines.append(f'    {name} = DataString("{name}")')
        lines += ['', '', 'LocalDatas = LocalDatas()', '']
        try:
            with open(os.path.join(BASE_DIR, 'utils.py'), 'w',
                      encoding='utf-8') as f:
                f.write('\n'.join(lines))
        except OSError as error:          # 只读安装：退回动态扫描
            print(f'[minibt] 写不了 LocalDatas 索引（{error}），'
                  '继续用目录扫描')
        return None

    def __getitem__(self, key: str) -> DataString:
        assert key and isinstance(key, str), "key为非空字符串"
        return DataString(key)

    def __getattr__(self, name: str) -> DataString:
        if name.startswith("_"):
            raise AttributeError(name)
        # 任意名字都返回 DataString —— 支持 ``LocalDatas.任意csv名``（含 `get_kline` 用字符串）
        return DataString(name)

    def __dir__(self) -> list[str]:
        return list(dict.fromkeys([*super().__dir__(), *list_csv_names()]))

    @staticmethod
    def get_path(name: str) -> str:
        return get_csv_path(name)

    def get_dataframe(self, name: str) -> DataFrame:
        df = read_csv(self.get_path(name))
        try:
            from ..other import time_to_datetime as _t2d
            df.datetime = df.datetime.apply(_t2d)
        except Exception:
            ...
        return df

    def get(self, name) -> DataString:
        return getattr(self, name)

    def __call__(self, name: str) -> DataString:
        return DataString(name)


def rewrite_datas(check: bool = False, base_only: bool = False):
    """重新生成 ``minibt/data/utils.py``（``LocalDatas`` 索引）。

    等价于 ``base.rewrite(check, base_only)``；
    ``other.save_and_generate_utils`` 保存完 CSV 后会调用它，让新数据立刻
    出现在 ``LocalDatas`` 里。``base_only=True`` 只列包内基础数据。
    """
    return base.rewrite(check, base_only)


def reset_index():
    """**打包前复位索引**：``LocalDatas`` 只列包内基础数据。

    等价于 ``rewrite_datas(base_only=True)``。

    ⚠ 它**只重新生成** ``minibt/data/utils.py``，**不会动任何 CSV 文件**：
    本机 ``~/.minibt/datas`` 里的用户数据依然在磁盘上（下次 ``rewrite()``
    又会把它们列回来）。要让某个名字真正从 ``LocalDatas`` 消失，请删掉真实
    文件 —— ``LocalDatas.deleter('名字')`` 或 ``LocalDatas.keep('名字')``。
    """
    return base.rewrite(base_only=True)
