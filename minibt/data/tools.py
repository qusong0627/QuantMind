from __future__ import annotations
import os
import glob
from typing import TYPE_CHECKING
from pandas import DataFrame, read_csv
from ..other import time_to_datetime


__all__ = ["base", "DataString"]
if TYPE_CHECKING:
    from .utils import LocalDatas
    from ..indicators import KLine
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_kline = None


class DataString(str):
    
    @property
    def dataframe(self) -> DataFrame | None:
        path = os.path.join(BASE_DIR, "test", f"{self}.csv")
        if not os.path.exists(path):
            path = str(self)
        if not os.path.exists(path):
            return
        df = read_csv(path, index_col=0).reset_index()
        try:
            df.datetime = df.datetime.apply(time_to_datetime)
        except:
            print(f"本地数据{self}的时间数据无法处理")
        return df

    def __kline(self):
        global _kline
        if _kline is None:
            from ..indicators import KLine
            _kline = KLine
        return _kline

    @property
    def kline(self) -> KLine | None:
        if self.dataframe is None:
            return
        return self.__kline()(self.dataframe, height=400)


class base:

    def update(self) -> LocalDatas | base:
        """更新本地文件"""
        self.rewrite()
        return self

    def deleter(self, *args) -> LocalDatas | base:
        """删除目标文件"""
        if args:
            t = False
            for name in args:
                path = os.path.join(BASE_DIR, "test", f"{name}.csv")
                if os.path.exists(path):
                    t = True
                    os.remove(path)
            if t:
                self.rewrite()
        return self

    def keep(self, *args) -> LocalDatas | base:
        """保留目标文件，其余的删除"""
        if args:
            attr = {k: v for k, v in vars(
                self.__class__).items() if not k.startswith("_")}
            delete_names = [k for k, v in attr.items() if k not in args]
            t = False
            for name in delete_names:
                path = os.path.join(BASE_DIR, "test", f"{name}.csv")
                if os.path.exists(path):
                    t = True
                    os.remove(path)
            if t:
                self.rewrite()
        return self

    def rename(self, old_name: str = "", new_name: str = "") -> LocalDatas | base:
        if all([old_name, new_name]) and all([isinstance(old_name, str), isinstance(new_name, str)]):
            old_path = os.path.join(BASE_DIR, "test", f"{old_name}.csv")
            if os.path.exists(old_path):
                os.rename(old_path, os.path.join(
                    BASE_DIR, "test", f"{new_name}.csv"))
                self.rewrite()
        return self

    @staticmethod
    def rewrite(check=False):
        # 获取当前目录下所有CSV文件名

        csv_files = glob.glob(os.path.join(BASE_DIR, "test", "*.csv"))
        py_file_path = os.path.join(BASE_DIR, "utils.py")

        names = [os.path.splitext(os.path.basename(file))[0]
                 for file in csv_files]
        if check:
            try:
                from .utils import LocalDatas
                # 1. 获取LocalDatas的类（排除实例干扰）
                local_datas_cls = LocalDatas.__class__
                # 2. 仅获取当前类自定义的非__开头属性（排除继承属性）
                current_attrs = [
                    k for k in local_datas_cls.__dict__.keys() if not k.startswith("__")]
                # 3. 准确判断是否需要重写
                need_rewrite = set(current_attrs) != set(names)
            except (ImportError, AttributeError):
                # 若LocalDatas未定义/导入失败，直接需要重写
                need_rewrite = True
        else:
            need_rewrite = False
        if need_rewrite:
            class_content = ['from .tools import *', "", "",
                             'class LocalDatas(base):', '    """本地CSV数据"""']
            for name in names:
                class_content.append(f'    {name} = DataString("{name}")')
            class_content.extend(["", ""])
            class_content.append('LocalDatas=LocalDatas()')
            with open(py_file_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(class_content))

    def __getitem__(self, key: str) -> str | LocalDatas:
        assert key and isinstance(key, str), "key为非空字符串"
        return key

    def __getattr__(self, name: str) -> str | LocalDatas | KLine:
        try:
            return super().__getattr__(name)
        except:
            setattr(self, name, name)
            return name

    @staticmethod
    def get_path(name: str) -> str:
        return os.path.join(BASE_DIR, "test", f"{name}.csv")

    def get_dataframe(self, name: str) -> DataFrame:
        df = read_csv(self.get_path(name))
        try:
            from ..other import time_to_datetime
            df.datetime = df.datetime.apply(time_to_datetime)
        except:
            ...
        return df

    def get(self, name) -> str:
        return getattr(self, name)

    def __call__(self, name: str) -> DataString | None:
        return DataString(name)
