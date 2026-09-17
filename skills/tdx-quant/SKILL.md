---
name: tdx-quant
description: （原名：通达信TQ）TdxQuant是由通达信软件提供的证券行情分析和量化投研平台，专注于为证券投资者提供行情信息获取、数据分析、策略研究、投资决策和智能交易的全流程解决方案
---

# Tdx Quant Skill

> 本文档基于 tqcenter.py **Version 1.0.6（2026-03-27）**
> 官方文档：https://help.tdx.com.cn/quant/docs/markdown/mindoc-1cfsjkbf8f3is/

TdxQuant 是由深圳市财富趋势科技股份有限公司研发的专业量化投研平台，本 Skill 通过 `tqcenter.py` 模块的 `tq` 类提供与通达信客户端交互的完整接口。

---

## 重要：Python 文件路径配置

### 文件目录结构

```
通达信安装目录\
├── Tdxw.exe                 # 主程序
├── PYPlugins\               # 插件目录
│   ├── TPyth.dll            # 通达信Python通信DLL
│   ├── TPythClient.dll      # 通达信Python通信DLL
│   ├── user\                # 用户策略目录
│   │   └── tqcenter.py      # TdxQuant核心模块
│   ├── data\                # 下载数据目录
│   └── file\                # 发送文件目录
```

**Python 策略文件可以放在任意位置。**

导入 `tqcenter` 前，必须将通达信安装目录的 `PYPlugins\user` 路径添加到 `sys.path`。**使用 `sys.path.insert(0, ...)` 确保加载正确的 `tqcenter.py`。**

### 获取通达信安装目录

通达信Tdxw的安装目录存储在 Windows 注册表中：

- **注册表路径**：`HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端64`
- **键名**：`InstallLocation`

可通过以下 Python 代码自动读取安装目录：

```python
import winreg

def get_tdx_install_path():
    """从注册表获取通达信安装目录"""
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端64"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            install_path, _ = winreg.QueryValueEx(key, "InstallLocation")
            return install_path
    except FileNotFoundError:
        print(f"未找到注册表项: HKLM\\{key_path}")
        return None

tdx_root = get_tdx_install_path()
# tdx_root 示例: "D:\\new_tdx" 或 "E:\\App\\new_tdx_test64"
```

- 本技能必须检测到进程中有运行的TdxW.exe进程。
- 进程路径所在的程序目录即为通达信安装目录。
- python 文件示例中，请将 `tdx_root` 替换为通达信安装目录。
- 如果检测不到tdxw.exe进程，请提示用户先运行通达信客户端。
- 通达信安装目录下面指定位置如果不存在tqcenter.py 文件，提示用户当前的客户端不支持TQ策略，需升级。
- 如果有多个TdxW.exe进程，请依次寻找，直到找到第一个含有tqcenter.py 文件的进程。
- 找到之后，请记住这个位置，作为 `tdx_root`，不用每次都查找，除非这个位置失效。

### 初始化示例（动态获取安装目录）

```python
import sys, winreg, os

# 从注册表获取安装目录
key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端64"
with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
    tdx_root, _ = winreg.QueryValueEx(key, "InstallLocation")

# 添加 PYPlugins/user 到 sys.path
sys.path.insert(0, os.path.join(tdx_root, 'PYPlugins', 'user'))

from tqcenter import tq

tq.initialize(__file__)
```

### 手动指定路径（已知安装目录时）

```python
import sys
sys.path.insert(0, 'E:/App/new_tdx_test64/PYPlugins/user')

from tqcenter import tq

tq.initialize(__file__)
```

**关键说明：**
- `tqcenter.py` 会在其上上级目录（`PYPlugins/`）自动定位 `TPythClient.dll`
- 使用 `sys.path.insert(0, ...)` 而非 `sys.path.append()`，优先加载通达信安装目录的 `tqcenter.py`
- `__file__` 为当前 Python 文件路径，用作策略唯一标识符

---

## 股票代码格式

- **上交所**：`600000.SH`
- **深交所**：`000001.SZ`
- **北交所**：`430047.BJ`
- **港股**：`00700.HK`
- **美股**：`AAPL.US`
- **新三板**：`430047.NQ`
- **股票期权**：`10004073.SZO` / `10004073.SHO`
- **国内期货**：代码.CFF / .SHF / .DCE / .CZC / .INE / .GFE
- **中证指数**：`000300.CSI`
- **开放式基金净值**：代码.OF

---

## 时间格式

- 仅日期：`YYYYMMDD`（如 `20231231`）
- 含时间：`YYYYMMDDHHMMSS`（如 `20231231150000`）

---

## 常量字典

### 市场代码后缀（股票代码后缀）

| 后缀 | 市场编号 | 说明 |
|------|----------|------|
| SZ | 0 | 深交所 |
| SH | 1 | 上交所 |
| BJ | 2 | 北交所 |
| US | 74 | 美股 |
| HK | 31 | 港股 |
| NQ | 44 | 新三板 |
| SZO | 9 | 深交所期权 |
| SHO | 8 | 上交所期权 |
| CSI | 62 | 中证指数 |
| CNI | 102 | 国证指数 |
| HG | 38 | 国内宏观指标 |
| CFF | 47 | 中金期货 |
| SHF | 30 | 上期所/上海能源 |
| DCE | 29 | 大商所 |
| CZC | 28 | 郑商所 |
| GFE | 66 | 广期所 |
| HI | 27 | 港股指数 |
| OF | 33 | 开放式基金净值 |

### K线周期（period）

| 值 | 说明 |
|----|------|
| `'1m'` | 1分钟 |
| `'5m'` | 5分钟 |
| `'15m'` | 15分钟 |
| `'30m'` | 30分钟 |
| `'1h'` | 60分钟 |
| `'1d'` | 日线 |
| `'1w'` | 周线 |
| `'1mon'` | 月线 |
| `'1q'` | 季线 |
| `'1y'` | 年线 |
| `'tick'` | 分笔 |

### 复权类型（dividend_type）

| 值 | 说明 |
|----|------|
| `'none'` | 不复权 |
| `'front'` | 前复权 |
| `'back'` | 后复权 |

### 委托类型（order_type）

| 常量 | 值 | 说明 |
|------|-----|------|
| `tqconst.STOCK_BUY` | 0 | 买入 |
| `tqconst.STOCK_SELL` | 1 | 卖出 |
| `tqconst.CREDIT_BUY` | 0 | 担保品买入（信用账户） |
| `tqconst.CREDIT_SELL` | 1 | 担保品卖出（信用账户） |
| `tqconst.CREDIT_FIN_BUY` | 69 | 融资买入 |
| `tqconst.CREDIT_SLO_SELL` | 70 | 融券卖出 |

### 报价类型（price_type）

| 常量 | 值 | 说明 |
|------|-----|------|
| `tqconst.PRICE_MY` | 0 | 自填价格 |
| `tqconst.PRICE_SJ` | 1 | 市价 |
| `tqconst.PRICE_ZTJ` | 2 | 涨停价/笼子上限 |
| `tqconst.PRICE_DTJ` | 3 | 跌停价/笼子下限 |

---

## 一、初始化与关闭

### `tq.initialize(path, dll_path='')`
初始化与通达信客户端的连接。

```python
tq.initialize(path: str, dll_path: str = '')
```

- `path`：当前 Python 文件路径（传入 `__file__`），作为策略唯一标识
- `dll_path`：可选，自定义 `TPythClient.dll` 路径

**注意：**
- 函数名 `"initialize"` 不可修改；每个策略必须有该函数
- 错误码 `ErrorId='12'` 表示已有同名策略运行（会打印警告但不报错）
- 错误码 `ErrorId='6'` 或 `'7'` 表示连接断开，会自动触发重新初始化

### `tq.close()`
手动关闭与通达信客户端的连接并释放资源。程序退出时也会自动调用。

---

## 二、行情数据接口

### 2.1 获取K线行情 `get_market_data`

```python
tq.get_market_data(
    field_list: List[str] = [],
    stock_list: List[str] = [],
    period: str = '',
    start_time: str = '',
    end_time: str = '',
    count: int = -1,
    dividend_type: Optional[str] = None,
    fill_data: bool = True
) -> Dict
```

根据股票列表获取历史 K 线数据。

**参数说明：**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| field_list | N | List[str] | 需返回的字段列表；空列表返回所有字段 |
| stock_list | Y | List[str] | 股票代码列表 |
| period | Y | str | K线周期 |
| start_time | N | str | 开始时间 |
| end_time | N | str | 结束时间；未传则默认当前时间 |
| count | N | int | `count>0`：取截止 end_time 最近 n 条；`count<=0`：使用 start_time/end_time 区间 |
| dividend_type | N | str | 复权类型：`'none'`（不复权）、`'front'`（前复权）、`'back'`（后复权） |
| fill_data | N | bool | 是否向前填充缺失数据，默认 `True` |

**返回字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| Date | str | 日期 |
| Time | str | 时间 |
| Open | str | 开盘价（元） |
| High | str | 最高价（元） |
| Low | str | 最低价（元） |
| Close | str | 收盘价（元） |
| Volume | str | 成交量（手） |
| Amount | str | 成交额（万元） |
| ForwardFactor | str | 前复权因子（仅 dividend_type=none 时有效） |

**返回值：** `Dict`，键为字段名，值为 `pd.DataFrame`（**行=时间，列=股票代码**）

**重要注意：**
- `count>0` 时 `start_time` 失效，`end_time` 若未传默认为当前时间
- 一次最多返回 24000 条数据，获取完整分钟线需分批
- 后复权数据与取的数据个数有关，只在返回的数据中进行后复权
- `dividend_type=None`（Python None，非字符串）时默认不复权

**数据样本：**
```
{'Close':             688318.SH
 2025-12-24     131.58,
 'Volume':             688318.SH
 2025-12-24  2257325.0}
```

---

### 2.2 获取实时行情快照 `get_market_snapshot`

```python
tq.get_market_snapshot(
    stock_code: str,
    field_list: List = []
) -> Dict
```

获取指定股票的最新行情快照数据。

**参数说明：**
- `stock_code`：单个股票代码，如 `'300505.SZ'`
- `field_list`：可选，指定返回字段（大小写不敏感）；空列表返回所有字段

**完整返回字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| ItemNum | str | 快照笔数 |
| LastClose | str | 前收盘价 |
| Open | str | 开盘价 |
| Max | str | 最高价 |
| Min | str | 最低价 |
| Now | str | 现价 |
| Volume | str | 总手 |
| NowVol | str | 现手 |
| Amount | str | 总成交金额 |
| Inside | str | 内盘（板块指数时为跌停家数） |
| Outside | str | 外盘（板块指数时为涨停家数） |
| TickDiff | str | 笔涨跌 |
| InOutFlag | str | 内外盘标志：0=Buy, 1=Sell, 2=Unknown |
| Jjjz | str | 基金净值 |
| Buyp | List[str] | 五档买价列表 |
| Buyv | List[str] | 五档买盘量列表 |
| Sellp | List[str] | 五档卖价列表 |
| Sellv | List[str] | 五档卖盘量列表 |
| UpHome | str | 上涨家数（对指数有效） |
| DownHome | str | 下跌家数（对指数有效） |
| Before5MinNow | str | 5分钟前价格 |
| Average | str | 均价 |
| XsFlag | str | 小数位数 |
| Zangsu | str | 涨速 |
| ZAFPre3 | str | 3日涨幅 |

**数据样本：**
```python
{'ItemNum': '3342', 'LastClose': '34.21', 'Open': '33.78', 'Max': '36.49',
 'Min': '32.50', 'Now': '35.06', 'Volume': '122881', 'NowVol': '1449',
 'Amount': '43068.48', 'Inside': '60373', 'Outside': '62509',
 'Buyp': ['35.05', '35.04', '35.02', '35.01', '35.00'],
 'Buyv': ['154', '9', '49', '136', '154'],
 'Sellp': ['35.06', '35.07', '35.08', '35.09', '35.10'],
 'Sellv': ['4', '31', '139', '4', '4'],
 'Average': '35.05', 'Zangsu': '-0.25', 'ZAFPre3': '-1.83', 'ErrorId': '0'}
```

---

### 2.3 获取股票基础信息 `get_stock_info`

```python
tq.get_stock_info(
    stock_code: str,
    field_list: List = []
) -> Dict
```

**注意：`field_list` 不能为空，必须明确指定字段列表。**

**完整返回字段（84个）：**

#### 基础信息

| 字段 | 说明 |
|------|------|
| Name | 证券名称 |
| Unit | 交易单位 |
| VolBase | 量比的基量 |
| MinPrice | 最小价格变动 |
| XsFlag | 价格小数位数 |
| Fz | 开收市时间（4段，8元素列表） |
| DelayMin | 延时分钟数 |
| QHVolBaseRate | 期货期权的每手乘数 |
| HKVolBaseRate | 港股/日股/新加坡股每手股数 |
| BelongHS300 | 是否属于沪深300 |
| BelongHasKQZ | 是否含可转债 |
| BelongRZRQ | 是否是融资融券标的 |
| BelongHSGT | 是否属于沪深股通 |
| IsHKGP | 是否是港股 |
| IsQH | 是否是期货 |
| IsQQ | 是否是期权 |
| IsSTGP | 是否是ST股票 |
| IsQuitGP | 是否是退市整理板股票 |
| TodayDRFlag | 当天是否有除权除息(沪深京) |
| HSStockKind | 沪深京品种类型：0=指数,1=A股主板,2=北证A股,3=创业板,4=科创板,5=B股,6=债券,7=基金,8=权重,9=其它,10=非沪深京品种 |

#### 股本数据

| 字段 | 说明 |
|------|------|
| ActiveCapital | 流通股本(万股) |
| J_zgb | 总股本(万股) |
| J_bg | B股(万股) |
| J_hg | H股(万股) |

#### 财务数据（万元）

| 字段 | 说明 |
|------|------|
| J_zzc | 总资产 |
| J_ldzc | 流动资产 |
| J_gdzc | 固定资产 |
| J_wxzc | 无形资产 |
| J_ldfz | 流动负债 |
| J_cqfz | 少数股东权益 |
| J_zbgjj | 资本公积金 |
| J_jzc | 股东权益/净资产 |
| J_yysy | 营业收入 |
| J_yycb | 营业成本 |
| J_yszk | 应收账款 |
| J_yyly | 营业利润 |
| J_tzsy | 投资收益 |
| J_jyxjl | 经营现金净流量 |
| J_zxjl | 总现金净流量 |
| J_ch | 存货 |
| J_lyze | 利润总额 |
| J_shly | 税后利润 |
| J_jly | 净利润 |
| J_wfply | 未分配利益 |

#### 每股指标

| 字段 | 说明 |
|------|------|
| J_jyl | 净资产收益率 |
| J_mgwfp | 每股未分配 |
| J_mgsy | 每股收益（折算为全年） |
| J_mgsy2 | 季报每股收益 |
| J_mggjj | 每股公积金 |
| J_mgjzc | 每股净资产 |
| J_mgjzc2 | 季报每股净资产 |
| J_gdqyb | 股东权益比 |
| J_gdrs | 股东人数 |
| J_HalfYearFlag | 报告期月份(3,6,9,12) |
| J_start | 上市日期 |

#### 行业与地域

| 字段 | 说明 |
|------|------|
| tdx_dycode | 通达信地域代码 |
| tdx_dyname | 通达信地域 |
| rs_hycode_sim | 通达信行业代码 |
| rs_hyname | 通达信行业 |
| blockzscode | 所属的行业板块指数代码 |
| underly_setcode | 标的市场代码（如ETF跟踪的指数市场） |
| underly_code | 标的代码（如ETF跟踪的指数代码） |

**数据样本：**

```python
{
    'Name': '财富趋势', 'Unit': '100', 'VolBase': '102.22', 'MinPrice': '0.01',
    'XsFlag': '2', 'Fz': ['570', '690', '780', '900', '900', '900', '900', '900'],
    'DelayMin': '0', 'QHVolBaseRate': '0', 'HKVolBaseRate': '0',
    'BelongHS300': '0', 'BelongHasKQZ': '0', 'BelongRZRQ': '1',
    'BelongHSGT': '1', 'IsHKGP': '0', 'IsQH': '0', 'IsQQ': '0',
    'IsSTGP': '0', 'IsQuitGP': '0', 'TodayDRFlag': '0', 'HSStockKind': '4',
    'ActiveCapital': '25611.94', 'J_zgb': '25611.94', 'J_bg': '0.00', 'J_hg': '0.00',
    'J_zzc': '389036.97', 'J_ldzc': '235598.84', 'J_gdzc': '972.62',
    'J_wxzc': '1184.64', 'J_ldfz': '17412.97', 'J_cqfz': '73.15',
    'J_zbgjj': '157998.02', 'J_jzc': '370454.03', 'J_yysy': '19827.85',
    'J_yycb': '4258.70', 'J_yszk': '2726.99', 'J_yyly': '20836.07',
    'J_tzsy': '5091.96', 'J_jyxjl': '5432.08', 'J_zxjl': '9779.30',
    'J_ch': '61.84', 'J_lyze': '20829.85', 'J_shly': '18421.45',
    'J_jly': '18421.34', 'J_wfply': '175521.63', 'J_jyl': '4.97',
    'J_mgwfp': '6.85', 'J_mgsy': '0.96', 'J_mgsy2': '0.00',
    'J_mggjj': '6.17', 'J_mgjzc': '14.46', 'J_mgjzc2': '14.46',
    'J_gdqyb': '0.95', 'J_gdrs': '24154.00', 'J_HalfYearFlag': '9',
    'J_start': '20200427', 'tdx_dycode': '18', 'tdx_dyname': '深圳板块',
    'rs_hycode_sim': 'X4202', 'rs_hyname': '软件服务',
    'blockzscode': '881355', 'underly_setcode': '0', 'underly_code': '',
    'ErrorId': '0'
}
```

---

### 2.4 获取股票所属板块 `get_relation`

```python
tq.get_relation(stock_code: str = '') -> List[Dict]
```

获取指定股票所属的全部板块信息。

**参数说明：**
- `stock_code`：股票代码，如 `'688318.SH'`

**返回字段：**

| 字段 | 说明 |
|------|------|
| BlockCode | 板块代码（无板块代码的板块返回 `"0"`） |
| BlockName | 板块名称 |
| BlockType | 板块类型（行业、地区、概念、风格、指数等） |
| GPNume | 成份股数量 |

**数据样本：**
```python
[{'BlockCode': '881355.SH', 'BlockName': '软件服务', 'BlockType': '行业', 'GPNume': '234'},
 {'BlockCode': '880218.SH', 'BlockName': '深圳板块', 'BlockType': '地区', 'GPNume': '427'},
 {'BlockCode': '880592.SH', 'BlockName': '互联金融', 'BlockType': '概念', 'GPNume': '211'},
 {'BlockCode': '880948.SH', 'BlockName': '人工智能', 'BlockType': '概念', 'GPNume': '1049'},
 {'BlockName': '沪股通标的', 'BlockType': '风格', 'GPNume': '1763'},
 {'BlockName': '中证500', 'BlockType': '指数', 'GPNume': '500'}]
```

```python
from tqcenter import tq
tq.initialize(__file__)

gp_block_res = tq.get_relation(stock_code='688318.SH')
print(gp_block_res)
```

---

### 2.6 获取分红配送数据 `get_divid_factors`

```python
tq.get_divid_factors(
    stock_code: str,
    start_time: str,
    end_time: str
) -> pd.DataFrame
```

**返回 DataFrame 列：**
- `Type`：类型；`Bonus`：每10股分红；`AllotPrice`：配股价；`ShareBonus`：送股比例；`Allotment`：配股比例

---

### 2.7 获取更多股票信息 `get_more_info`

```python
tq.get_more_info(
    stock_code: str = '',
    field_list: List = []
) -> Dict
```

获取股票更多扩展信息，包含涨幅系列、主力净额、L2数据、封单、PE/PB/市值等上百个字段。`field_list` 为空返回所有字段。

**完整返回字段（100+个）：**

#### 基本信息

| 字段 | 说明 |
|------|------|
| MainBusiness | 主营构成 |
| SafeValue | 安全分 |
| ShineValue | 亮点数 |
| ShapeValue | 短期形态+中期形态+长期形态编号 |
| TPFlag | 停牌标识 |
| ZTPrice | 涨停价 |
| DTPrice | 跌停价 |
| HqDate | 行情日期 |

#### 行情指标

| 字段 | 说明 |
|------|------|
| fHSL | 换手率 |
| fLianB | 量比 |
| Wtb | 委比 |
| Zsz | 总市值(亿) |
| Ltsz | 流通市值(亿) |
| vzangsu | 量涨速 |
| Fzhsl | 分钟换手率 |
| FzAmo | 2分钟金额(万元) |
| VOpenZAF | 抢筹涨幅 |

#### 涨幅系列

| 字段 | 说明 |
|------|------|
| ZAF | 涨幅 |
| ZAFYesterday | 昨日涨幅 |
| ZAFPre2D | 前天涨幅 |
| ZAFPre5 | 5日涨幅 |
| ZAFPre10 | 10日涨幅 |
| ZAFPre20 | 20日涨幅 |
| ZAFPre30 | 30日涨幅 |
| ZAFPre60 | 60日涨幅 |
| ZAFYear | 年初至今涨幅 |
| ZAFPreMyMonth | 涨幅(本月来) |
| ZAFPreOneYear | 涨幅(一年来) |

#### 主力资金

| 字段 | 说明 |
|------|------|
| Zjl | 主买净额(万元) |
| Zjl_HB | 主力净流入(万元) |

#### L2数据

| 字段 | 说明 |
|------|------|
| TotalBVol | 总买量 |
| TotalSVol | 总卖量 |
| BCancel | 总撤买量 |
| SCancel | 总撤卖量 |
| L2TicNum | L2逐笔成交数 |
| L2OrderNum | L2逐笔委托数 |

#### 封单与竞价

| 字段 | 说明 |
|------|------|
| FCAmo | 封单额(万元) |
| FCb | 封成比 |
| OpenAmo | 开盘金额(万元)(A股和板块指数有效) |
| OpenZTBuy | 竞价涨停买入金额(万元) |
| OpenFDE | 开盘封单额(万元) |
| OpenAmoPre1 | 昨开盘金额(万元) |
| OpenVolPre1 | 昨开盘量 |
| CJJEPre1 | 昨成交额(万元) |
| CJJEPre3 | 3日成交额(万元) |
| FDEPre1 | 昨封单额(万元) |
| FDEPre2 | 前封单额(万元) |

#### 涨停相关

| 字段 | 说明 |
|------|------|
| ZTGPNum | 板块指数的涨停家数 |
| LastStartZT | 几天 |
| LastZTHzNum | 几板 |
| EverZTCount | 连板天 |
| ConZAFDateNum | 连涨天数 |
| YearZTDay | 年涨停天数 |

#### 估值与价格

| 字段 | 说明 |
|------|------|
| MA5Value | 5日均价 |
| HisHigh | 52周最高 |
| HisLow | 52周最低 |
| IPO_Price | 发行价 |
| More_YJL | ETF,LOF溢价率 |
| BetaValue | 贝塔系数 |
| DynaPE | 动态市盈率 |
| MorePE | 市盈率(港股:动,其他扩展:静) |
| StaticPE_TTM | 市盈率(TTM) |
| DYRatio | 股息率 |
| PB_MRQ | 市净率(MRQ) |

#### 股票属性

| 字段 | 说明 |
|------|------|
| IsT0Fund | 是否是T+0基金 |
| IsZCZGP | 是否是注册制A股 |
| IsKzz | 是否可转债 |
| Kzz_HSCode | 可转债对应的正股代码 |
| QHMainYYMM | 主力合约关联的月份(期货),主力和次主力 |

#### 财务扩展

| 字段 | 说明 |
|------|------|
| FreeLtgb | 自由流通股本(万) |
| Yield | 应计利息(债券),占款天数(回购) |
| KfEarnMoney | 扣非净利润(万元) |
| RDInputFee | 研发费用(万元) |
| CashZJ | 货币资金(万元) |
| PreReceiveZJ | 合同负债(万元) |
| OtherQYJzc | 其它权益工具(万元) |
| StaffNum | 员工人数 |

#### 近期事件日期

| 字段 | 说明 |
|------|------|
| RecentGGJYDate | 最近北上大额交易日 |
| RecentHGDate | 最近回购预案日 |
| RecentIncentDate | 最近股权激励预案日 |
| NoticeDate_Recent | 最近业绩预告日 |
| RecentReleaseDate | 最近解禁日 |
| RecentDZDate | 最近定增日 |
| ReportDate | 最近财报公告日期 |
| ZTDate_Recent | 近2年最近涨停板日期 |
| DTDate_Recent | 近2年最近跌停板日期 |
| TopDate_Recent | 近2年最近龙虎榜日期 |
| StopJYDate_Recent | 最近停牌日期 |

涨停跌停的判断：用get_more_info取FCAmo来判断涨停跌停，大于0是涨停，小于0是跌停。

**数据样本：**

```python
{
    'MainBusiness': '软件服务收入', 'SafeValue': '98', 'ShineValue': '3',
    'ShapeValue': '101308', 'TPFlag': '0', 'ZTPrice': '151.62', 'DTPrice': '101.08',
    'HqDate': '20260227', 'fHSL': '0.86', 'fLianB': '0.89', 'Wtb': '-0.66',
    'Zsz': '326.91', 'Ltsz': '326.91', 'vzangsu': '2.17', 'Fzhsl': '0.12',
    'FzAmo': '514.92', 'VOpenZAF': '0.00', 'ZAF': '1.02', 'ZAFYesterday': '-1.21',
    'ZAFPre2D': '1.99', 'ZAFPre5': '-1.56', 'ZAFPre10': '-3.44', 'ZAFPre20': '-10.76',
    'ZAFPre30': '-10.13', 'ZAFPre60': '-1.59', 'ZAFYear': '-3.54',
    'ZAFPreMyMonth': '-5.23', 'ZAFPreOneYear': '10.27', 'Zjl': '0.00', 'Zjl_HB': '0.00',
    'TotalBVol': '1295.00', 'TotalSVol': '3555.00', 'BCancel': '42606.00',
    'SCancel': '40266.00', 'L2TicNum': '6880', 'L2OrderNum': '29448',
    'FCAmo': '0.00', 'FCb': '0.00', 'OpenAmo': '1069400.00', 'OpenZTBuy': '0.00',
    'OpenFDE': '0.00',
    'ZTGPNum': '0', 'EverZTCount': '0', 'ConZAFDateNum': '1', 'YearZTDay': '0',
    'MA5Value': '126.56', 'HisHigh': '180.86', 'HisLow': '83.41', 'IPO_Price': '107.41',
    'More_YJL': '0.00', 'BetaValue': '2.31', 'DynaPE': '133.10', 'MorePE': '107.56',
    'StaticPE_TTM': '94.99', 'DYRatio': '0.28', 'PB_MRQ': '8.82',
    'IsT0Fund': '0', 'IsZCZGP': '1', 'IsKzz': '0', 'QHMainYYMM': '0',
    'FreeLtgb': '7935.14', 'Yield': '106.94', 'KfEarnMoney': '9778.22',
    'RDInputFee': '5894.58', 'CashZJ': '60954.52', 'StaffNum': '446',
    'RecentReleaseDate': '20230427', 'ReportDate': '20251031',
    'ZTDate_Recent': '20241008', 'TopDate_Recent': '20250625',
    'ErrorId': '0'
}
```

---

### 2.8 获取股本信息 `get_gb_info`

```python
tq.get_gb_info(
    stock_code: str = '',
    date_list: List[str] = [],
    count: int = 1
) -> Dict
```

**参数说明：**
- `date_list`：日期列表，格式 `YYYYMMDD`；须从小到大排序；`len(date_list)` 必须 ≥ `count`
- `count`：要获取的日期数量，必须 ≥ 1

**返回字段：**

| 字段 | 说明 |
|------|------|
| Date | 日期 |
| Zgb | 总股本 |
| Ltgb | 流通股本 |

**数据样本：**

```python
[
    {'Date': 20250101, 'Zgb': 182942480.0, 'Ltgb': 182942480.0},
    {'Date': 20250601, 'Zgb': 182942480.0, 'Ltgb': 182942480.0}
]
```

---

### 2.9 获取可转债信息 `get_kzz_info`

```python
tq.get_kzz_info(
    stock_code: str = '',
    field_list: List[str] = []
) -> Dict
```

**完整返回字段：**

| 字段 | 说明 |
|------|------|
| KZZCode | 可转债代码 |
| HSCode | 正股代码 |
| ZGPrice | 转股价格 |
| CurRate | 当期利率 |
| RestScope | 剩余规模（万） |
| PutBack | 回售触发价 |
| ForceRedeem | 强赎触发价 |
| ZGDate | 转股日 |
| EndPrice | 到期价 |
| EndDate | 到期日期 |
| ZGRate | 转股比率% |
| RealValue | 纯债价值 |
| ExpireYield | 到期收益率% |
| KZZScore | 可转债评级 |
| HSScore | 主体评级 |
| AGPrice | 正股当前价格 |
| KZZPrice | 可转债当前价格 |
| KZZYj | 溢价率 |
| ZGValue | 转股价值 |

```python
kzz_info = tq.get_kzz_info(stock_code='123039.SZ')
```

---

### 2.10 获取新股申购信息 `get_ipo_info`

```python
tq.get_ipo_info(
    ipo_type: int = 0,
    ipo_date: int = 0
) -> list
```

- `ipo_type`：`0`=新股申购，`1`=新发债，`2`=两者都返回
- `ipo_date`：`0`=只返回今天，`1`=今天及以后

**返回字段：**

| 字段 | 说明 |
|------|------|
| code | 申购代码 |
| name | 证券名称 |
| SGCode | 申购代码 |
| SGDate | 申购日期 |
| SGPrice | 申购价格 |
| PE_Issue | 市盈率 |
| MaxSG | 最大申购额度 |
| setcode | 市场代码：0=沪市，1=深市 |

**数据样本：**

```python
[
    {'MaxSG': '0.00', 'PE_Issue': '0.00', 'SGCode': '371036', 'SGDate': '20251226',
     'SGPrice': '100.00', 'code': '301036', 'name': '双乐转债', 'setcode': '0'},
    {'MaxSG': '0.00', 'PE_Issue': '0.00', 'SGCode': '718676', 'SGDate': '20251225',
     'SGPrice': '100.00', 'code': '688676', 'name': '金05转债', 'setcode': '1'}
]
```

---

### 2.11 获取跟踪指数的ETF信息 `get_trackzs_etf_info`

```python
tq.get_trackzs_etf_info(zs_code: str = '') -> List[Dict]
```

获取跟踪某一指数的全部ETF基金信息。

- `zs_code`：指数代码，如 `'000300.CSI'`（沪深300）、`'950162.CSI'`（科创芯片设计）

**返回字段：**

| 字段 | 说明 |
|------|------|
| Code | ETF代码 |
| Name | ETF名称 |
| NowPrice | 现价 |
| PreClose | 昨收 |
| IOPV | 净值 |
| Zgb | 净额（万份） |
| Sz | 规模（亿元） |

**数据样本：**
```python
[{'Code': '589210.SH', 'Name': '科创芯片设计ETF', 'NowPrice': '1.208',
  'PreClose': '1.192', 'IOPV': '1.2071', 'Zgb': '7646.90', 'Sz': '0.92'}, ...]
```

---

## 三、专业数据接口

### 3.1 获取专业财务数据 `get_financial_data`

```python
tq.get_financial_data(
    stock_list: List[str] = [],
    field_list: List[str] = [],
    start_time: str = '',
    end_time: str = '',
    report_type: str = 'report_time'
) -> Dict
```

获取财务报表数据（字段 FN1~FN584），需要客户端下载专业财务数据包。

- `report_type`：`'announce_time'`（按公告时间）或 `'tag_time'`（按标签时间），默认按报告期
- **返回值：** `Dict`，键为股票代码，值为 `pd.DataFrame`

**常用字段（完整字段请参考官方文档）：**

| 字段 | 说明 |
|------|------|
| FN1 | 基本每股收益 |
| FN2 | 扣除非经常性损益每股收益 |
| FN3 | 每股未分配利润 |
| FN4 | 每股净资产 |
| FN5 | 每股资本公积金 |
| FN6 | 净资产收益率 |
| FN7 | 每股经营现金流量 |
| FN8 | 货币资金 |
| FN21 | 流动资产合计 |
| FN40 | 资产总计 |
| FN54 | 流动负债合计 |
| FN63 | 负债合计 |
| FN64 | 实收资本（或股本） |
| FN68 | 未分配利润 |
| FN72 | 所有者权益合计 |
| FN73 | 负债和所有者权益合计 |
| FN107 | 经营活动产生的现金流量净额 |
| FN119 | 投资活动产生的现金流量净额 |
| FN128 | 筹资活动产生的现金流量净额 |
| FN131 | 现金及现金等价物净增加额 |
| FN134 | 净利润 |

**返回字段：** `announce_time`（公告日期）、`tag_time`（报告期）+ 上述财务字段

---

### 3.2 按年度/季度获取财务数据 `get_financial_data_by_date`

```python
tq.get_financial_data_by_date(
    stock_list: List[str] = [],
    field_list: List[str] = [],
    year: int = 0,
    mmdd: int = 0
) -> Dict
```

- `year`：年份（如 `2023`）；`0` 返回最新
- `mmdd`：报告期月日（如 `331`=一季报，`630`=中报，`930`=三季报，`1231`=年报）；`0` 返回最新

---

### 3.3 获取股票交易数据 `get_gpjy_value`

```python
tq.get_gpjy_value(
    stock_list: List[str] = [],
    field_list: List[str] = [],
    start_time: str = '',
    end_time: str = ''
) -> Dict
```

获取股票市场交易数据（字段 GP1~GP46），包含：股东人数、龙虎榜、融资融券、大宗交易、陆股通、涨停数据等。

**完整返回字段（GP1~GP46）：**

| 字段 | 说明 |
|------|------|
| GP01 | 股东人数 股东户数(户) |
| GP02 | 龙虎榜 买入总计(万元) 卖出总计(万元) |
| GP03 | 融资融券1 融资余额(万元) 融券余量(股) |
| GP04 | 大宗交易 成交均价(元) 成交额(万元) |
| GP05 | 增减持1 成交均价(元) 变动股数(股) |
| GP06 | 陆股通持股量 持股数量(股) |
| GP07 | 陆股通市场成交净额 陆股通市场净买入(万元) |
| GP08 | 龙虎榜机构(卖方)数据 卖方机构个数 机构卖出金额(万元) |
| GP09 | 龙虎榜机构(买方)数据 买方机构个数 机构买入金额(万元) |
| GP10 | 近3月机构调研情况 近3月机构调研次数 近3月调研机构数量 |
| GP11 | 融资融券2 融资买入额(万元) 融资偿还额(万元) |
| GP12 | 融资融券3 融券卖出量(股) 融券偿还量(股) |
| GP13 | 融资融券4 融资净买入(万元) 融券净卖出(股) |
| GP14 | 涨停数据 涨停金额(万元) 开板次数 |
| GP15 | 涨跌停 涨跌停状态 封单金额(万元) |
| GP16 | 总市值 总市值(万元) |
| GP17 | 龙虎榜营业部数据 买入金额(万元) 卖出金额(万元) |
| GP18 | 龙虎榜沪深股通数据 买入金额(万元) 卖出金额(万元) |
| GP19 | 每周股票质押数量 无限售股份质押数(万) 有限售股份质押数(万) |
| GP20 | 每周股票质押比例 质押比例(%) |
| GP21 | 股息率 股息率(%) |
| GP22 | 涨跌停 封成比 封流比 |
| GP23 | 拟增减持 拟增持数量(万股) 拟减持数量(万股) |
| GP24 | 涨停 首次涨停时间 涨停最大封单额(万) |
| GP25 | 盘前盘后成交量 开盘成交量(手) 盘后固定成交量(手) |
| GP26 | 拟增减持金额 拟增持金额(万元) 拟减持金额(万元) |
| GP27 | 人气排名 市场人气排名 行业人气排名 |
| GP28 | 股票回购 回购均价(元) 回购数量(万股) |
| GP29 | 证券信息 是否复牌日 是否更名日 |
| GP30 | 分红送转 派息金额(万元) 送转数量(股) |
| GP31 | 转融券 期初余量(股) 期末余量(股) |
| GP32 | 转融券 融出数量(股) 融出市值(元) |
| GP33 | 跌停数据 跌停金额(万元) 开板次数 |
| GP34 | 跌停 首次跌停时间 跌停最大封单额(万) |
| GP35 | 增减持2 增持数量(股) 减持数量(股) |
| GP36 | 竞价涨停买 买入金额(万元) |
| GP37 | 龙虎榜2 上榜类型连续交易日(天) |
| GP38 | 涨停相关1 近1年涨停次数 近1年溢价5%次数 |
| GP39 | 涨停相关2 近1年首板封板率(%) 近1年次日红盘率(%) |
| GP40 | 涨停相关3 近1年连板率(%) 最后涨停时间 |
| GP41 | 股权登记日 配股股权登记日 |
| GP42 | 龙虎榜专业机构买卖净额 买方成交净额(万元) 卖方成交净额(万元) |
| GP43 | 配股实施 配股价格(元) 配股数量(万股) |
| GP44 | 股票评分 综合评分 |
| GP45 | 评级系数 评级系数 |
| GP46 | 拟询价转让 拟转让股数(万股) 拟转让占总股本(%) |

**数据样本：**

```python
{'688318.SH': {'GP3': [{'Date': '20250102', 'Value': ['141405.89', '11113.00']}]}}
```

---

### 3.4 按日期获取股票交易数据 `get_gpjy_value_by_date`

```python
tq.get_gpjy_value_by_date(
    stock_list: List[str] = [],
    field_list: List[str] = [],
    year: int = 0,
    mmdd: int = 0
) -> Dict
```

---

### 3.5 获取板块交易数据 `get_bkjy_value`

```python
tq.get_bkjy_value(
    stock_list: List[str] = [],
    field_list: List[str] = [],
    start_time: str = '',
    end_time: str = ''
) -> Dict
```

获取板块级别市场交易数据（字段 BK5~BK19），包含：PE/PB/PS/PC、市值、涨跌停数、融资融券等。

**完整返回字段（BK5~BK19）：**

| 字段 | 说明 |
|------|------|
| BK5 | 市盈率TTM 整体法 算术平均 |
| BK6 | 市净率MRQ 整体法 算术平均 |
| BK7 | 市销率TTM 整体法 算术平均 |
| BK8 | 市现率TTM 整体法 算术平均 |
| BK9 | 涨跌数 上涨家数 下跌家数 |
| BK10 | 板块总市值(亿元) 整体法 算术平均 |
| BK11 | 板块流通市值(亿元) 整体法 算术平均 |
| BK12 | 涨停数 涨停家数 曾涨停家数 |
| BK13 | 跌停数 跌停家数 曾跌停家数 |
| BK14 | 涨停数据 市场高度(不含ST股和未开板新股) 2板及以上涨停个数 |
| BK15 | 融资融券 沪深京融资余额(万元) 沪深京融券余额(万元) |
| BK16 | 陆股通资金流入 沪股通流入金额(亿元) 深股通流入金额(亿元) |
| BK17 | 开盘成交数 开盘成交额(万元) 开盘成交量(万股) |
| BK18 | 板块股息率(%) 算数平均 整体法 |
| BK19 | 板块自由流通市值(亿元) 整体法 算术平均 |

**数据样本：**

```python
{
    '880660.SH': {
        'BK5': [{'Date': '20250102', 'Value': ['55.28', '55.50']}],
        'BK6': [{'Date': '20250102', 'Value': ['4.62', '3.79']}],
        'BK9': [{'Date': '20250102', 'Value': ['0.00', '35.00']}]
    }
}
```

---

### 3.6 按日期获取板块交易数据 `get_bkjy_value_by_date`

```python
tq.get_bkjy_value_by_date(
    stock_list: List[str] = [],
    field_list: List[str] = [],
    year: int = 0,
    mmdd: int = 0
) -> Dict
```

---

### 3.7 获取市场交易数据 `get_scjy_value`

```python
tq.get_scjy_value(
    field_list: List[str] = [],
    start_time: str = '',
    end_time: str = ''
) -> Dict
```

获取市场整体宏观交易数据（字段 SC01~SC42），包含：融资融券、陆股通、涨停、打板资金、ETF规模、新开户等。无需传 `stock_list`。

**完整返回字段（SC01~SC42）：**

| 字段 | 说明 |
|------|------|
| SC01 | 融资融券 沪深京融资余额(万元) 沪深京融券余额(万元) |
| SC02 | 陆股通资金流入 沪股通流入金额(亿元) 深股通流入金额(亿元) |
| SC03 | 沪深京涨停股个数 涨停股个数 曾涨停股个数 |
| SC04 | 沪深京跌停股个数 跌停股个数 曾跌停股个数 |
| SC05 | 上证50股指期货 净持仓(手) |
| SC06 | 沪深300股指期货 净持仓(手) |
| SC07 | 中证500股指期货 净持仓(手) |
| SC08 | ETF基金规模份额数据 ETF基金规模(亿份) ETF净申赎(亿份) |
| SC09 | 沪月新开A股账户 沪月新开A股账户(万户) |
| SC10 | 增减持统计 增持额(万元) 减持额(万元) |
| SC11 | 大宗交易 溢价的大宗交易额(万元) 折价的大宗交易额(万元) |
| SC12 | 限售解禁 限售解禁计划额(亿元) 限售解禁股份实际上市金额(亿元) |
| SC13 | 分红 市场总分红额(亿元) |
| SC14 | 募资 市场总募资额(亿元) |
| SC15 | 打板资金 封板成功资金(亿元) 封板失败资金(亿元) |
| SC16 | 龙虎榜 买入总金额(亿元) 卖出总金额(亿元) |
| SC17 | 龙虎榜机构数据 买入金额(亿元) 卖出金额(亿元) |
| SC18 | 龙虎榜营业部数据 买入金额(亿元) 卖出金额(亿元) |
| SC19 | 龙虎榜沪深股通数据 买入金额(亿元) 卖出金额(亿元) |
| SC20 | 陆股通净买入 沪股通净买入额(亿元) 深股通净买入额(亿元) |
| SC21 | 每周无限售质押率 深市质押率(%) 沪市质押率(%) |
| SC22 | 每周有限售质押率 深市质押率(%) 沪市质押率(%) |
| SC23 | 连板家数 连板股个数(包含ST) 连板股个数(不含ST) |
| SC24 | 沪深京涨跌停股个数 涨停股个数(不含ST) 跌停股个数（不含ST） |
| SC25 | 融资融券 沪深京融资买入额(万元) 沪深京融券卖出量(万股) |
| SC26 | 每周市场质押比 每周市场质押比例(%) |
| SC27 | 央行公开市场净投放 央行公开市场净投放(亿元) |
| SC28 | 历史A股新高新低数 历史新高A股个数 历史新低A股个数 |
| SC29 | 120天A股新高新低数 120天新高A股个数 120天新低A股个数 |
| SC30 | 涨停数据 市场高度 2板以上涨停个数 |
| SC31 | 涨跌家数 涨家数 跌家数 |
| SC32 | 20天A股新高新低数 20天新高A股个数 20天新低A股个数 |
| SC33 | 市场总封单金额 涨停封单金额(亿元) 跌停封单金额(亿元) |
| SC34 | 涨跌股成交量 上涨股成交量(万手) 下跌股成交量(万手) |
| SC35 | 涨停数据 换手板家数 回封率(%) |
| SC36 | 曾涨跌停股个数 曾涨停股个数 曾跌停股个数 |
| SC37 | 转融券 融出市值(亿元) 期末余额(亿元) |
| SC38 | ETF基金规模金额数据 ETF基金规模(亿元) ETF净申赎(亿元) |
| SC39 | 涨跌5%家数 涨幅大于等于5%家数 跌幅大于等于5%家数 |
| SC40 | 陆股通成交 陆股通成交总额(亿元) 陆股通成交总笔(万笔) |
| SC41 | 中证1000股指期货 净持仓(手) |
| SC42 | 沪深股通成交金额 沪股通成交总额(亿元) 深股通成交总额(亿元) |

---

### 3.8 按日期获取市场交易数据 `get_scjy_value_by_date`

```python
tq.get_scjy_value_by_date(
    field_list: List[str] = [],
    year: int = 0,
    mmdd: int = 0
) -> Dict
```

---

### 3.9 获取股票单个数据字段 `get_gp_one_data`

```python
tq.get_gp_one_data(
    stock_list: List[str] = [],
    field_list: List[str] = []
) -> Dict
```

获取股票单个数据字段（字段 GO1~GO47，不按时间序列，返回当前值），包含：发行价、一致预期EPS、解禁日、机构持股、业绩预告等。

**完整返回字段（GO1~GO47）：**

#### 基本信息

| 字段 | 说明 |
|------|------|
| GO1 | 发行价(元) |
| GO2 | 总发行数量(万股) |

#### 一致预期

| 字段 | 说明 |
|------|------|
| GO3 | 一致预期目标价(元) |
| GO4 | 一致预期T年度 |
| GO5 | 一致预期T年每股收益 |
| GO6 | 一致预期T+1年每股收益 |
| GO7 | 一致预期T+2年每股收益 |
| GO8 | 一致预期T年净利润(万元) |
| GO9 | 一致预期T+1年净利润(万元) |
| GO10 | 一致预期T+2年净利润(万元) |
| GO11 | 一致预期T年营业收入(万元) |
| GO12 | 一致预期T+1年营业收入(万元) |
| GO13 | 一致预期T+2年营业收入(万元) |
| GO14 | 一致预期T年营业利润(万元) |
| GO15 | 一致预期T+1年营业利润(万元) |
| GO16 | 一致预期T+2年营业利润(万元) |
| GO17 | 一致预期T年每股净资产(元) |
| GO18 | 一致预期T+1年每股净资产(元) |
| GO19 | 一致预期T+2年每股净资产(元) |
| GO20 | 一致预期T年净资产收益率(%) |
| GO21 | 一致预期T+1年净资产收益率(%) |
| GO22 | 一致预期T+2年净资产收益率(%) |
| GO23 | 一致预期T年PE |
| GO24 | 一致预期T+1年PE |
| GO25 | 一致预期T+2年PE |

#### 解禁与持股

| 字段 | 说明 |
|------|------|
| GO26 | 最新解禁日(YYMMDD格式) |
| GO27 | 最新解禁数量（万股） |
| GO28 | 下一报告期的预约披露时间 |
| GO29 | 最新持股机构家数 |
| GO30 | 最新机构持股总量（万股） |
| GO31 | 最新持股基金家数 |
| GO32 | 最新基金持股量（万股） |
| GO33 | 最新总股本（万股） |
| GO34 | 最新实际流通A股（万股） |

#### 业绩预告

| 字段 | 说明 |
|------|------|
| GO35 | 最新业绩预告 报告期 |
| GO36 | 最新业绩预告 本期归母净利润下限（万元） |
| GO37 | 最新业绩预告 本期归母净利润上限（万元） |
| GO38 | 最新业绩预告 本期归母净利润预计同比增减幅下限% |
| GO39 | 最新业绩预告 本期归母净利润预计同比增减幅上限% |
| GO40 | 最新业绩快报 报告期 |
| GO41 | 最新业绩快报 归母净利润（万元） |

#### 分红募资

| 字段 | 说明 |
|------|------|
| GO42 | 分红募资 派现总额（万元） |
| GO43 | 分红募资 募资总额（万元） |

#### 扣非业绩预告

| 字段 | 说明 |
|------|------|
| GO44 | 最新业绩预告 本期扣非净利润下限(万元) |
| GO45 | 最新业绩预告 本期扣非净利润上限(万元) |
| GO46 | 最新业绩预告 本期扣非净利润预计同比增减幅下限% |
| GO47 | 最新业绩预告 本期扣非净利润预计同比增减幅上限% |

---

## 四、板块与股票列表接口

### 4.1 获取系统分类股票列表 `get_stock_list`

```python
tq.get_stock_list(
    market = None,
    list_type: int = 0
) -> List
```

获取指定市场/分类的股票列表。

- `list_type`：`0`=只返回代码，`1`=返回代码和名称（`[{'Code':..., 'Name':...}, ...]`）

**market 参数完整说明：**

```
默认为全部A股（market='5'）

0:自选股  1:持仓股
5:所有A股  6:上证指数成份股  7:上证主板  8:深证主板  9:重点指数
10:所有板块指数  11:缺省行业板块  12:概念板块  13:风格板块  14:地区板块
15:缺省行业分类+概念板块  16:研究行业一级  17:研究行业二级  18:研究行业三级
21:含H股  22:含可转债  23:沪深300  24:中证500  25:中证1000
26:国证2000  27:中证2000  28:中证A500
30:REITs  31:ETF基金  32:可转债  33:LOF基金  34:所有可交易基金
35:所有沪深基金  36:T+0基金
49:金融类企业  50:沪深A股  51:创业板  52:科创板  53:北交所
91:ETF追踪的指数
92:国内期货主力合约
101:国内期货  102:港股  103:美股
```

**使用示例：**
```python
# 获取所有A股代码
all_stocks = tq.get_stock_list(market='5')

# 获取ETF基金（含代码和名称）
etf_list = tq.get_stock_list(market='31', list_type=1)

# 获取国内期货主力合约
futures_main = tq.get_stock_list(market='92')
```

---

### 4.2 获取板块列表 `get_sector_list`

```python
tq.get_sector_list(list_type: int = 0) -> List
```

获取 A 股全部板块代码列表（相当于 `get_stock_list('10')`）。

---

### 4.3 获取板块内股票列表 `get_stock_list_in_sector`

```python
tq.get_stock_list_in_sector(
    block_code: str,
    block_type: int = 0,
    list_type: int = 0
) -> List
```

**参数说明：**
- `block_code`：板块代码或板块名称，如 `'BK0707'` 或 `'通达信88'`
- `block_type`：
  - `0`：板块指数代码或板块名称（默认）
  - `1`：自定义板块简称（加 `BKCODE.` 前缀）；特殊板块：`ZXG`=自选股，`TJG`=临时条件股
  - `2`：期货板块代码（加 `QH.` 前缀）
- `list_type`：`0`=只返回代码

---

### 4.4 获取用户自选股板块列表 `get_user_sector`

```python
tq.get_user_sector() -> List
```

获取通达信客户端中用户设置的自选股板块列表。

---

### 4.5 自定义板块管理

#### 创建板块 `create_sector`
```python
tq.create_sector(block_code: str = '', block_name: str = '')
```
- `block_code`：板块简称（不能为空）
- `block_name`：板块显示名称（不能为空）

#### 删除板块 `delete_sector`
```python
tq.delete_sector(block_code: str = '')
```

#### 重命名板块 `rename_sector`
```python
tq.rename_sector(block_code: str = '', block_name: str = '')
```

#### 清空板块 `clear_sector`
```python
tq.clear_sector(block_code: str = '')
```
清空板块内所有成分股（不删除板块本身）。

---

## 五、行情订阅接口

### 5.1 刷新行情缓存 `refresh_cache`

```python
tq.refresh_cache(
    market: str = 'AG',
    force: bool = False
)
```

刷新快照行情和K线缓存。首次调用 `get_market_snapshot` 或 `get_market_data` 时系统会自动刷新，无需手动调用。

**market 参数：**

| 值 | 说明 |
|----|------|
| `'AG'` | A股（默认） |
| `'HK'` | 港股 |
| `'US'` | 美股 |
| `'QH'` | 国内期货 |
| `'QQ'` | 股票期权 |
| `'NQ'` | 新三板 |
| `'ZZ'` | 中证指数 |
| `'ZS'` | 综合指数 |
| `'OF'` | 开放式基金 |

- `force`：`False`=距上次刷新不足10分钟则跳过；`True`=强制刷新

---

### 5.2 刷新历史K线缓存 `refresh_kline`

```python
tq.refresh_kline(
    stock_list: List[str] = [],
    period: str = ''
)
```

**参数说明：**
- `period`：仅支持 `'1m'`、`'5m'`、`'1d'` 三种
- 不建议一次更新太多，会堵塞策略和客户端

---

### 5.3 订阅行情更新 `subscribe_hq`

```python
tq.subscribe_hq(
    stock_list: List[str] = [],
    callback = None
) -> object
```

订阅指定股票的实时行情更新，有行情变化时自动触发回调函数。**最多订阅 100 只。**

**回调参数格式：**
```python
def on_data(datas):
    import json
    d = json.loads(datas)
    # d 格式：{"Code": "000001.SZ", "ErrorId": "0"}
    code = d.get('Code')
    error_id = d.get('ErrorId')
```

**大量股票推荐分批订阅（每批50只）：**
```python
BATCH_SIZE = 50
for i in range(0, len(stocks), BATCH_SIZE):
    batch = stocks[i:i+BATCH_SIZE]
    tq.subscribe_hq(stock_list=batch, callback=my_callback)
```

---

### 5.4 取消订阅 `unsubscribe_hq`

```python
tq.unsubscribe_hq(stock_list: List[str] = [])
```

---

### 5.5 获取已订阅股票列表 `get_subscribe_hq_stock_list`

```python
tq.get_subscribe_hq_stock_list() -> List
```

---

## 六、下载文件接口

### `download_file`

```python
tq.download_file(
    stock_code: str = '',
    down_time: str = '',
    down_type: int = 1
)
```

下载通达信特定文件数据（十大股东、ETF申赎数据、舆情、综合信息等）。
文件保存至 `./PYPlugins/data` 目录。

- `down_type`：`1`=十大股东，`2`=ETF申赎，`3`=舆情，`4`=综合信息

---

## 七、与客户端交互接口

### 7.1 发送消息到客户端 `send_message`

```python
tq.send_message(msg_str: str) -> Dict
```

将字符串消息发送到通达信客户端 TQ 策略管理界面显示。用 `|` 分行。

```python
tq.send_message("MSG,策略运行中|买入信号数：3")
```

---

### 7.2 发送文件路径到客户端 `send_file`

```python
tq.send_file(file_path: str) -> Dict
```

发送 txt/pdf/html 文件到通达信客户端展示。文件须放在 `./PYPlugins/file/` 目录。

```python
tq.send_file("513100.txt")   # 发送txt文件
tq.send_file("report.pdf")   # 发送PDF文件
```

---

### 7.3 发送预警信号 `send_warn`

```python
tq.send_warn(
    stock_list:     List[str] = [],
    time_list:      List[str] = [],
    price_list:     List[str] = [],
    close_list:     List[str] = [],
    volum_list:     List[str] = [],
    bs_flag_list:   List[str] = [],
    warn_type_list: List[str] = [],
    reason_list:    List[str] = [],
    count: int = 1
) -> Dict
```

发送预警信号到通达信客户端 TQ 策略信号界面。

**参数说明：**

| 参数 | 必填 | 说明 |
|------|------|------|
| stock_list | Y | 股票代码列表 |
| time_list | N | 时间列表（`YYYYMMDDHHMMSS`）；缺少时补当前时间 |
| price_list | Y | 价格列表（纯数字字符串） |
| close_list | Y | 收盘/昨收价列表（纯数字字符串） |
| volum_list | Y | 成交量列表（纯数字字符串） |
| bs_flag_list | N | 买卖标志：`'0'`=买（红色B），`'1'`=卖（绿色S），`'2'`=未知（黄色）；缺少时补 `'2'` |
| warn_type_list | N | 预警类型：`'1'` 支持双击闪电买卖；缺少时补 `'-1'` |
| reason_list | N | 预警原因（有效长度25个汉字）；缺少时补空字符串 |
| count | Y | 信号数量，必须 > 0 |

**注意：** `price_list`、`close_list`、`volum_list` 中的元素必须为纯数字字符串；`bs_flag_list` 可以传空字符串 `''`（等同于 `'2'`）。

```python
# 双击预警信号可直接打开闪电买卖界面
tq.send_warn(
    stock_list=['600519.SH'],
    time_list=['20260101100000'],
    price_list=['1823.45'],
    close_list=['1822.50'],
    volum_list=['15000'],
    bs_flag_list=['0'],       # 买入标记
    warn_type_list=['1'],     # 支持双击闪电买卖
    reason_list=['MA金叉信号'],
    count=1
)
```

---

### 7.4 发送回测数据 `send_bt_data`

```python
tq.send_bt_data(
    stock_code: str = '',
    time_list:  List[str] = [],
    data_list:  List[List[str]] = [],
    count: int = 1
) -> Dict
```

输出回测数据到通达信客户端显示，配合 `SIGNALS_TQ` 公式在K线图上展示。

**参数说明：**
- `time_list`：时间列表（`YYYYMMDDHHMMSS` 或 `YYYYMMDD`）；长度必须 ≥ `count`
- `data_list`：二维列表，每个子列表对应一个时间点，最多取前**16个**值（均为纯数字字符串）
- `count`：数据条数，必须 > 0

**data_list 子列表位置与 SIGNALS_TQ ID 对应关系：**
- 位置1（index 0）→ `SIGNALS_TQ(1, TYPE)`
- 位置2（index 1）→ `SIGNALS_TQ(2, TYPE)`
- ...以此类推，最多16个

```python
# 发送MA5、MA10和买卖信号
tq.send_bt_data(
    stock_code='688318.SH',
    time_list=['20260120', '20260121'],
    data_list=[
        ['136.60', '131.74', '1', '0'],   # MA5, MA10, 买信号, 卖信号
        ['135.30', '131.48', '0', '1']
    ],
    count=2
)
```

---

### 7.5 发送股票到自定义板块 `send_user_block`

```python
tq.send_user_block(
    block_code: str = '',
    stocks: List[str] = [],
    show: bool = False
) -> Dict
```

- `block_code`：目标板块简称；**为空字符串时发送到"临时条件股"板块**
- `stocks`：股票代码列表；**传入空列表 `[]` = 清空板块**
- `show`：是否在客户端自动跳转显示该板块

```python
# 发送选股结果到临时条件股
tq.send_user_block(block_code='', stocks=result_list)

# 发送到自定义板块并显示
tq.send_user_block(block_code='MYBLOCK', stocks=result_list, show=True)
```

---

### 7.6 调用客户端功能 `exec_to_tdx`

```python
tq.exec_to_tdx(url: str = '') -> Dict
```

让通达信客户端根据传入的 URL 或功能串执行指定操作。

**参数说明：**
- `url`：功能调用串或网址；若是功能串，请以 `http://www.treeid` 开头

**返回字段：**

| 字段 | 说明 |
|------|------|
| ErrorId | 错误码，`'0'` 表示成功 |
| Msg | 返回信息（内容为传入的 url） |
| run_id | 执行 ID |

```python
from tqcenter import tq
tq.initialize(__file__)

# 调用功能串（跳转到国内期货主界面）
exec_res1 = tq.exec_to_tdx(url='http://www.treeid/MAINQH')

# 打开网页链接（弹出浏览器窗口）
exec_res2 = tq.exec_to_tdx(url='http://www.treeid/dlghttp://www.tdx.com.cn')
print(exec_res2)
# {'ErrorId': '0', 'Msg': 'http://www.treeid/dlghttp://www.tdx.com.cn', 'run_id': '1'}
```

---

### 7.7 导出数据到通达信界面 `print_to_tdx`

```python
tq.print_to_tdx(
    df_list,
    sp_name="",
    xml_filename="",
    jsn_filenames=None,
    vertical=None,
    horizontal=None,
    height=None,
    table_names=None
)
```

将多组 DataFrame 数据导出到通达信客户端 TQ 策略数据浏览中展示。

**参数说明：**

| 参数 | 说明 |
|------|------|
| df_list | DataFrame 列表；第一列必须是日期，后续列是指标名 |
| sp_name | 因子名称，用于生成 `.sp` 文件名 |
| xml_filename | 生成的 XML 文件名（含 `.xml` 后缀） |
| jsn_filenames | JSON 文件名列表，长度必须等于 df_list 长度 |
| vertical | 纵向排列的 table 组数，与 horizontal 二选一 |
| horizontal | 横向排列的 table 组数（优先级更高） |
| height | 各 gridctrl 高度列表（如 `["0.4", "0.6"]`），不传则自动平均分配 |
| table_names | 各组表格标题列表 |

---

## 八、交易日历接口

### 8.1 获取交易日列表 `get_trading_dates`

```python
tq.get_trading_dates(
    market: str,
    start_time: str,
    end_time: str,
    count: int = -1
) -> List
```

获取指定市场在时间范围内的纯交易日列表。**需要先在客户端下载上证指数盘后数据。**

- `market`：如 `'SH'`、`'SZ'`、`'HK'` 等
- `count`：返回数量限制，`-1` 返回全部

---

## 九、通达信公式接口

### 9.1 格式化K线数据 `formula_format_data`

```python
tq.formula_format_data(data_dict: Dict = {}) -> Dict
```

将 `get_market_data` 获取的K线数据格式化为通达信公式接口可识别的格式。

**输入要求：** `data_dict` 必须包含 `Amount`、`Volume`、`Close`、`Open`、`High`、`Low` 六个字段。

**返回值：** `Dict`，键为股票代码，值为 `List[Dict]`，每个 Dict 包含 `Date`、`Amount`、`Volume`、`Close`、`Open`、`High`、`Low`。

---

### 9.2 向公式设置数据 `formula_set_data`

```python
tq.formula_set_data(
    stock_code: str = '',
    stock_period: str = '1d',
    stock_data: List = [],
    count: int = 1,
    dividend_type: int = 0
)
```

在调用公式前设置公式所需的K线数据（须先用 `formula_format_data` 格式化）。

- `stock_data`：`formula_format_data` 格式化后的数据
- `count`：生效的K线条数，`1 ≤ count ≤ 24000`，且 `len(stock_data) ≥ count`
- `dividend_type`：`0`=不复权，`1`=前复权，`2`=后复权

**注意：** 后设置的数据会覆盖前一次设置；与 `formula_set_data_info` 互相覆盖。

---

### 9.3 向公式设置数据信息 `formula_set_data_info`

```python
tq.formula_set_data_info(
    stock_code: str = '',
    stock_period: str = '1d',
    start_time: str = '',
    end_time: str = '',
    count: int = 0,
    dividend_type: int = 0
)
```

**count 参数：**
- `>0`：按条数（最多24000条）
- `=0`：按 start_time/end_time 区间
- `=-1`：获取全部数据
- `=-2`：使用无序列数据（count=-2时为无序列数据）

---

### 9.4 调用通达信公式（单次，需提前设置数据）

#### 调用指标公式 `formula_zb`
```python
tq.formula_zb(
    formula_name: str = '',
    formula_arg: str = '',
    xsflag: int = -1
) -> Dict
```
**返回：** `{'Data': {'DIF': [...], 'DEA': [...], ...}, 'ErrorId': '0'}`

#### 调用选股公式 `formula_xg`
```python
tq.formula_xg(
    formula_name: str = '',
    formula_arg: str = ''
) -> Dict
```
**返回：** `{'Data': {'UP3': ['0', '1', ...], ...}, 'ErrorId': '0'}`

#### 调用表达式公式 `formula_exp`
```python
tq.formula_exp(
    formula_name: str = '',
    formula_arg: str = ''
) -> Dict
```

**参数说明：**
- `formula_name`：公式名称（区分大小写）
- `formula_arg`：逗号分隔的数字字符串，最多16个参数，如 `'12,26,9'`
- `xsflag`：精度控制，`-1` 返回默认精度，最大可返回8位小数

**调用前必须先设置数据（`formula_set_data` 或 `formula_set_data_info`）。**

---

### 9.5 获取公式数据 `formula_get_data`

```python
tq.formula_get_data() -> Dict
```

在调用公式后，获取当前设置的公式数据结果。

---

### 9.6 批量调用选股公式 `formula_process_mul_xg` ⭐推荐

```python
tq.formula_process_mul_xg(
    formula_name: str = '',
    formula_arg: str = '',
    return_count: int = 1,
    return_date: bool = False,
    stock_list: List[str] = [],
    stock_period: str = '1d',
    start_time: str = '',
    end_time: str = '',
    count: int = 0,
    dividend_type: int = 0
) -> Dict
```

对多只股票批量调用通达信选股公式，**无需提前设置数据，效率更高。**

**返回值格式（`return_date=False` 时）：**
```python
{'000001.SZ': {'UP3': ['0', '1', '0']},
 '600519.SH': {'UP3': ['1', '1', '0']},
 'ErrorId': '0'}
```

**返回值格式（`return_date=True` 时）：**
```python
{'000001.SZ': {'UP3': [{'Date': '20260203', 'Value': '0'}, ...]},
 'ErrorId': '0'}
```

**重要：**
- 批量调用无需 `formula_set_data`，且 `formula_set_data` 的设置对批量调用不生效
- `count` 太小会导致结果比客户端条件选股少（需覆盖公式最大回溯K线数）

---

### 9.7 批量调用指标公式 `formula_process_mul_zb`

```python
tq.formula_process_mul_zb(
    formula_name: str = '',
    formula_arg: str = '',
    xsflag: int = -1,
    return_count: int = 1,
    return_date: bool = False,
    stock_list: List[str] = [],
    stock_period: str = '1d',
    start_time: str = '',
    end_time: str = '',
    count: int = 0,
    dividend_type: int = 0
) -> Dict
```

对多只股票批量调用通达信指标公式，参数含义同 `formula_process_mul_xg`，额外支持 `xsflag` 精度控制。

**返回值格式（`return_date=True` 时）：**
```python
{'000001.SZ': {'DIF': [{'Date': '20260203', 'Value': '0.12'}, ...],
               'DEA': [...]},
 'ErrorId': '0'}
```

---

## 十、交易接口

> **注意：** 交易接口需要先在通达信客户端登录交易账号。

### 10.1 获取资金账户句柄 `stock_account`

```python
tq.stock_account(
    account: str = '',
    account_type: str = 'stock'
) -> int
```

- `account`：资金账号（字符串）
- `account_type`：账号类型，如 `'STOCK'`
- **返回值 ≥ 0 为有效句柄，< 0 为无效句柄**

```python
from tqcenter import tq
tq.initialize(__file__)
account_id = tq.stock_account(account="1190008847", account_type="STOCK")
print(account_id)  # 0
```

---

### 10.2 查询委托信息 `query_stock_orders`

```python
tq.query_stock_orders(
    account_id: int = -1,
    stock_code: str = '',
    cancelable_only: bool = False
) -> List[Dict]
```

- `account_id < 0` 时查询当前账户
- **仅能查询当日委托**
- `cancelable_only`：是否仅查可撤委托（暂未生效）

**返回字段：**

| 字段 | 说明 |
|------|------|
| Wtbh | 委托编号 |
| Code | 股票代码 |
| Time | 时间（HHMMSS） |
| BSFlag | 买卖标志：0=买，1=卖，-1=撤单 |
| KPFlag | 开平标志：0=开仓，1=平仓，2=平今 |
| WTFS | 市价方式 |
| Status | 委托状态：0=无效单，1=未成交，2=部分成交，3=全部成交，4=部分成交部分撤单，5=全部撤单 |
| WtDate | 撤单标志：1=已撤，2=夜盘单 |
| WtPrice | 委托价 |
| CjPrice | 成交价 |
| CjVol | 成交数量（撤单时为负值） |
| WtVol | 委托数量（撤单时为负值） |

**数据样本：**
```python
[
    {'Wtbh': '48957', 'Code': '688318.SH', 'Time': '93605', 'BSFlag': -1, 'KPFlag': 0,
     'WTFS': 0, 'Status': 0, 'WtPrice': '125.000', 'CjPrice': '0.000', 'CjVol': '0', 'WtVol': '1000'},
    {'Wtbh': '58545', 'Code': '688318.SH', 'Time': '93853', 'BSFlag': -1, 'KPFlag': 0,
     'WTFS': 0, 'Status': 0, 'WtPrice': '125.000', 'CjPrice': '0.000', 'CjVol': '0', 'WtVol': '1000'}
]
```

---

### 10.3 查询持仓信息 `query_stock_positions`

```python
tq.query_stock_positions(account_id: int = -1) -> List[Dict]
```

**返回字段：**

| 字段 | 说明 |
|------|------|
| Code | 股票代码 |
| Cbj | 成本价 |
| TotalVol | 总持仓 |
| CanUseVol | 可用持仓 |

**数据样本：**
```python
[{'Code': '000001.SZ', 'Cbj': '10.693', 'TotalVol': '100', 'CanUseVol': '100'},
 {'Code': '688318.SH', 'Cbj': '117.425', 'TotalVol': '4000', 'CanUseVol': '4000'}]
```

---

### 10.4 下单 `order_stock`

```python
tq.order_stock(
    account_id: int = -1,
    stock_code: str = '',
    order_type: int = 0,
    order_volume: int = 0,
    price_type: int = 0,
    price: float = 0.0
)
```

- `account_id < 0` 时以当前账户下单
- `order_type` 支持：`STOCK_BUY`(0)买入、`STOCK_SELL`(1)卖出、`CREDIT_BUY`(0)担保品买入、`CREDIT_SELL`(1)担保品卖出、`CREDIT_FIN_BUY`(69)融资买入、`CREDIT_SLO_SELL`(70)融券卖出
- 若 `price_type` 为市价类型，具体市价规则请在客户端→系统设置→参数中配置

**返回字段：**

| 字段 | 说明 |
|------|------|
| Value | 成功标志：0=失败，1=待用户确认，2=成功 |
| Wtbh | 委托编号（仅成功时返回） |
| Msg | 返回提示信息 |

> **注意：** 实盘交易账户下单后需用户在客户端确认（Value=1）。自动下单需联系开户券商开通支持TQ的版本。

```python
from tqcenter import tq, tqconst
tq.initialize(__file__)

account_id = tq.stock_account(account="1190008847", account_type="STOCK")

# 以自填价160.0买入688318.SH 200股
result = tq.order_stock(
    account_id=account_id,
    stock_code="688318.SH",
    order_type=tqconst.STOCK_BUY,
    order_volume=200,
    price_type=tqconst.PRICE_MY,
    price=160.0
)
print(result)  # {'ErrorId': '0', 'Msg': '已发送信号至客户端，待用户确认！', 'Value': 1}
```

---

### 10.5 撤单 `cancel_order_stock`

```python
tq.cancel_order_stock(
    account_id: int = -1,
    stock_code: str = '',
    order_id: str = ''
) -> Dict
```

根据委托编号撤单。

**参数说明：**
- `account_id`：资金账户句柄（由 `stock_account` 获取，`<0` 时使用当前账户）
- `stock_code`：股票代码
- `order_id`：委托编号（由 `query_stock_orders` 返回的 `Wtbh` 字段）

**返回字段：**

| 字段 | 说明 |
|------|------|
| Value | 成功标志：0=失败，1=成功 |
| Msg | 返回提示信息 |

```python
from tqcenter import tq
tq.initialize(__file__)

account_id = tq.stock_account(account="1190008847", account_type="STOCK")
orders = tq.query_stock_orders(account_id=account_id, stock_code="")

cancel_res = tq.cancel_order_stock(
    account_id=account_id,
    stock_code=orders[0]['Code'],
    order_id=orders[0]['Wtbh']
)
print(cancel_res)  # {'Value': 1, 'ErrorId': '0', 'Msg': '提交撤单成功！'}
```

---

### 10.6 查询账户资产 `query_stock_asset`

```python
tq.query_stock_asset(account_id: int = -1) -> Dict
```

查询指定账户的总资产、余额和持仓市值。

**返回字段：**

| 字段 | 说明 |
|------|------|
| Currency | 币种 |
| Balance | 余额 |
| Cash | 可用余额 |
| Asset | 总资产 |
| MarketValue | 总持仓市值 |

**数据样本：**
```python
{'Currency': '人民币', 'Balance': '30234.070', 'Cash': '30234.070',
 'Asset': '1233041.070', 'MarketValue': '1201690.000', 'ErrorId': '0'}
```

---

## 十一、辅助工具

### `tq.price_df`

```python
tq.price_df(
    df: dict,
    price_col: str,
    column_names=None
) -> pd.DataFrame
```

从 `get_market_data` 返回的 dict 中提取指定价格字段，自动处理日期索引、缺失值填充。

```python
df_real = tq.get_market_data(field_list=['Close','Open'], stock_list=codes, ...)
close_df = tq.price_df(df_real, 'Close', column_names=codes)
```

---

## 十二、SIGNALS_TQ 公式系统

通过 `send_bt_data` 发送序列数据后，可在通达信公式管理器中创建技术指标公式，使用 `SIGNALS_TQ(ID, TYPE)` 函数引用数据并在K线图上展示。

### 函数语法

```
SIGNALS_TQ(ID, TYPE)
```

| 参数 | 说明 |
|------|------|
| ID | data_list 子列表中的位置序号（1~16） |
| TYPE | `0`=不平滑处理；`1`=平滑（无数据的周期返回上一周期的值）；`2`=无数据则为0 |

### 通达信公式示例（TQMA510）

在通达信公式管理器中新建名为 `TQMA510` 的公式：

```
{技术指标}
MA5:SIGNALS_TQ(1,0);        {引用ID=1的数据(MA5)}
MA10:SIGNALS_TQ(2,0);       {引用ID=2的数据(MA10)}

{交易信号}
BUY_SIGNAL:=SIGNALS_TQ(3,0);  {买入信号}
SELL_SIGNAL:=SIGNALS_TQ(4,0); {卖出信号}

{回测指标展示}
开仓次数:SIGNALS_TQ(5,0),COLORRED;
平仓次数:SIGNALS_TQ(6,0),COLORGREEN;
单位净值:SIGNALS_TQ(7,0),COLORWHITE;
基准净值:SIGNALS_TQ(8,0),COLORYELLOW;
胜率:SIGNALS_TQ(9,0),COLORMAGENTA;
年化收益率:SIGNALS_TQ(10,0),COLORCYAN;
基准年化收益率:SIGNALS_TQ(11,0),COLORLIBLUE;
贝塔值:SIGNALS_TQ(12,0),COLORBROWN;
最大回撤:SIGNALS_TQ(13,0),COLORGRAY;
夏普比率:SIGNALS_TQ(14,0),COLORLIMAGENTA;
每日资金:SIGNALS_TQ(15,0),COLORLIGRAY;
持仓量:SIGNALS_TQ(16,0),COLORLIRED;

{绘制交易信号图标}
DRAWICON(BUY_SIGNAL, LOW, 1);
DRAWICON(SELL_SIGNAL, HIGH, 2);
```

---

## 使用示例

### 示例1：获取K线数据

```python
import sys
sys.path.insert(0, 'E:/App/new_tdx_test64/PYPlugins/user')
from tqcenter import tq

tq.initialize(__file__)

# 获取最近100条日线数据（前复权）
data = tq.get_market_data(
    field_list=['Open', 'Close', 'High', 'Low', 'Volume', 'Amount'],
    stock_list=['600000.SH', '000001.SZ'],
    period='1d',
    count=100,
    dividend_type='front'
)

close_df = data['Close']  # DataFrame，行=时间，列=股票代码
print(close_df.tail())

tq.close()
```

---

### 示例2：获取实时行情快照

```python
from tqcenter import tq
tq.initialize(__file__)

snapshot = tq.get_market_snapshot(stock_code='300505.SZ')
print(snapshot.get('Now'))      # 当前价
print(snapshot.get('Volume'))   # 成交量
print(snapshot.get('Buyp'))     # 五档买价

tq.close()
```

---

### 示例3：MACD全市场批量选股（推荐方式）

```python
from tqcenter import tq
tq.initialize(__file__)

# 获取所有A股
all_stocks = tq.get_stock_list(market='5')

# 批量调用MACD公式，返回最新2条数据
mul_zb_result = tq.formula_process_mul_zb(
    formula_name='MACD',
    formula_arg='12,26,9',
    xsflag=6,
    return_count=2,
    return_date=False,
    stock_list=all_stocks,
    stock_period='1d',
    count=100,
    dividend_type=1
)

# 筛选金叉股票
macd_stocks = []
if mul_zb_result:
    for key in mul_zb_result:
        if key != "ErrorId":
            dif = mul_zb_result[key].get('DIF', [])
            dea = mul_zb_result[key].get('DEA', [])
            if len(dif) >= 2 and len(dea) >= 2:
                if float(dif[-2]) < float(dea[-2]) and float(dif[-1]) >= float(dea[-1]):
                    macd_stocks.append(key)

print(f"MACD金叉股票：{len(macd_stocks)}只")
# 发送到临时条件股
tq.send_user_block(block_code='', stocks=macd_stocks)
tq.close()
```

---

### 示例4：连续N日上涨选股并加入自定义板块

```python
import pandas as pd
import numpy as np
from datetime import datetime
from tqcenter import tq

tq.initialize(__file__)

batch_codes = tq.get_stock_list_in_sector('通达信88')
N = 3           # 连续上涨天数
block_code = 'LZXG'
block_name = '连涨选股'

df_real = tq.get_market_data(
    field_list=['Close'],
    stock_list=batch_codes,
    start_time="20251025",
    end_time=datetime.now().strftime("%Y%m%d"),
    dividend_type='front',
    period='1d',
    fill_data=True
)
close_df = tq.price_df(df_real, 'Close', column_names=batch_codes)

# 计算连续上涨天数
is_up = close_df > close_df.shift(1)
up_mask = np.where(is_up, 1, np.nan)
up_mask_df = pd.DataFrame(up_mask, index=close_df.index, columns=close_df.columns)
filled_df = up_mask_df.ffill()
consec_up_days = filled_df.notna().cumsum()
reset_counts = consec_up_days.where(~is_up).ffill().fillna(0)
consec_up_days = (consec_up_days - reset_counts).astype(int)

# 筛选最新交易日连续上涨≥N天的股票
latest_date = consec_up_days.index[-1]
latest_consec_up = consec_up_days.loc[latest_date]
target_stocks_list = latest_consec_up[latest_consec_up >= N].sort_values(ascending=False).index.tolist()

# 创建板块并推送
tq.create_sector(block_code=block_code, block_name=block_name)
tq.send_user_block(block_code=block_code, stocks=target_stocks_list, show=True)
tq.send_message(f"MSG,连涨≥{N}天股票：{len(target_stocks_list)}只")
tq.close()
```

---

### 示例5：订阅行情实时预警（分批订阅+防抖）

```python
import json, time, signal, sys
from datetime import datetime
from collections import defaultdict
from tqcenter import tq

PRICE_RISE_THRESHOLD = 5.0   # 涨幅阈值
ANTI_SHAKE_SECONDS = 10       # 防抖间隔（秒）
BATCH_SIZE = 50               # 每批订阅数量

tq.initialize(__file__)

all_stocks = tq.get_stock_list_in_sector('通达信88')
last_warn_time = defaultdict(int)
TRIGGERED = set()
EXIT_FLAG = False

def signal_handler(signum, frame):
    global EXIT_FLAG
    EXIT_FLAG = True
    tq.unsubscribe_hq(stock_list=all_stocks)
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def price_rise_callback(data_str):
    code_json = json.loads(data_str)
    code = code_json.get('Code')
    if code_json.get('ErrorId') != "0" or not code or code in TRIGGERED:
        return

    snapshot = tq.get_market_snapshot(code)
    if not snapshot:
        return
    now_price = float(snapshot.get('Now', 0))
    last_close = float(snapshot.get('LastClose', 0))
    if now_price <= 0 or last_close <= 0:
        return

    rise_rate = (now_price - last_close) / last_close * 100
    current_time = int(time.time())
    if rise_rate > PRICE_RISE_THRESHOLD and (current_time - last_warn_time[code]) >= ANTI_SHAKE_SECONDS:
        TRIGGERED.add(code)
        last_warn_time[code] = current_time
        tq.unsubscribe_hq(stock_list=[code])
        tq.send_warn(
            stock_list=[code],
            time_list=[datetime.now().strftime("%Y%m%d%H%M%S")],
            price_list=[str(now_price)],
            close_list=[str(last_close)],
            volum_list=[snapshot.get('Volume', '0')],
            bs_flag_list=['0'],
            warn_type_list=['3'],
            reason_list=[f'涨幅突破{PRICE_RISE_THRESHOLD}%'],
            count=1
        )

# 分批订阅
for i in range(0, len(all_stocks), BATCH_SIZE):
    tq.subscribe_hq(stock_list=all_stocks[i:i+BATCH_SIZE], callback=price_rise_callback)

print(f"监控启动，共{len(all_stocks)}只，按Ctrl+C退出...")
while not EXIT_FLAG:
    time.sleep(0.1)
```

---

### 示例6：MA均线调仓信号+发送预警（可双击闪电买卖）

```python
import vectorbt as vbt
import pandas as pd
from datetime import datetime, timedelta
from tqcenter import tq

tq.initialize(__file__)

N = 5  # 均线周期
batch_codes = tq.get_stock_list_in_sector('通达信88')
end_date = datetime.now().strftime("%Y%m%d")
start_date = (datetime.now() - timedelta(days=2 * N + 20)).strftime("%Y%m%d")

df_real = tq.get_market_data(
    field_list=['Close'],
    stock_list=batch_codes,
    start_time=start_date,
    end_time=end_date,
    dividend_type='front',
    period='1d',
    fill_data=True
)
close_df = tq.price_df(df_real, 'Close', column_names=batch_codes)

ma = vbt.MA.run(close_df, window=N).ma
ma.columns = close_df.columns
entries = close_df.vbt.crossed_above(ma)
exits = close_df.vbt.crossed_below(ma)
latest_date = close_df.index[-1]
prev_date = close_df.index[-2] if len(close_df.index) >= 2 else latest_date

# 收集买卖信号
buy_signals, sell_signals = {}, {}
for code in batch_codes:
    if code not in close_df.columns:
        continue
    today_close = round(close_df.loc[latest_date, code], 2)
    prev_close = round(close_df.loc[prev_date, code], 2)
    if entries.loc[latest_date, code]:
        buy_signals[code] = {'today_close': today_close, 'prev_close': prev_close}
    if exits.loc[latest_date, code]:
        sell_signals[code] = {'today_close': today_close, 'prev_close': prev_close}

# 发送预警（warn_type='1' 支持双击闪电买卖）
all_signals = [(c, i, '买入') for c, i in buy_signals.items()] + \
              [(c, i, '卖出') for c, i in sell_signals.items()]
if all_signals:
    warn_time = datetime.now().strftime("%Y%m%d%H%M%S")
    tq.send_warn(
        stock_list=[s[0] for s in all_signals],
        time_list=[warn_time] * len(all_signals),
        price_list=[str(s[1]['today_close']) for s in all_signals],
        close_list=[str(s[1]['prev_close']) for s in all_signals],
        volum_list=['0'] * len(all_signals),
        bs_flag_list=['0' if s[2] == '买入' else '1' for s in all_signals],
        warn_type_list=['1'] * len(all_signals),    # 支持双击闪电买卖
        reason_list=[f"{s[2]}信号" for s in all_signals],
        count=len(all_signals)
    )
tq.close()
```

---

### 示例7：vectorbt 简单回测

```python
import pandas as pd
import vectorbt as vbt
from tqcenter import tq

tq.initialize(__file__)

pd.set_option('future.no_silent_downcasting', True)

stock_code_list = ['688318.SH']
window = 5
target_start = '20250701'
target_end = '20251231'
start_time = (pd.to_datetime(target_start) - pd.Timedelta(days=window + 10)).strftime('%Y%m%d')

df_real = tq.get_market_data(
    field_list=['Close', 'Open'],
    stock_list=stock_code_list,
    start_time=start_time,
    end_time=target_end,
    dividend_type='front',
    period='1d',
    fill_data=True
)
close_df = tq.price_df(df_real, 'Close', column_names=stock_code_list)
open_df = tq.price_df(df_real, 'Open', column_names=stock_code_list)

ma = vbt.MA.run(close_df, window=window).ma.ffill()
ma.columns = close_df.columns

entries_df = close_df.vbt.crossed_above(ma).shift(1).fillna(False).astype(bool)
exits_df = close_df.vbt.crossed_below(ma).shift(1).fillna(False).astype(bool)

portfolio = vbt.Portfolio.from_signals(
    close=close_df,
    entries=entries_df,
    exits=exits_df,
    price=open_df,
    init_cash=100000,
    fees=0.0003,
    freq='D',
    size_granularity=100    # A股最小交易单位100股
)

# 输出结果并绘图
print(portfolio.stats())
portfolio[stock_code_list[0]].plot().show()

tq.close()
# 注意：vectorbt 不支持分红送股等权益变动
```

---

### 示例8：发送回测数据+通达信公式展示

```python
from tqcenter import tq
import pandas as pd

tq.initialize(__file__)

stock_code = '688318.SH'
data = tq.get_market_data(
    field_list=['Close'],
    stock_list=[stock_code],
    period='1d',
    count=60,
    dividend_type='front'
)
close_series = data['Close'][stock_code]
ma5 = close_series.rolling(5).mean()
ma10 = close_series.rolling(10).mean()
buy_signal = ((ma5.shift(1) <= ma10.shift(1)) & (ma5 > ma10)).astype(int)
sell_signal = ((ma5.shift(1) >= ma10.shift(1)) & (ma5 < ma10)).astype(int)

time_list = close_series.index.strftime('%Y%m%d').tolist()
data_list = []
for i in range(len(close_series)):
    data_list.append([
        f"{ma5.iloc[i]:.2f}" if not pd.isna(ma5.iloc[i]) else "0",
        f"{ma10.iloc[i]:.2f}" if not pd.isna(ma10.iloc[i]) else "0",
        str(int(buy_signal.iloc[i])),
        str(int(sell_signal.iloc[i]))
    ])

tq.send_bt_data(
    stock_code=stock_code,
    time_list=time_list,
    data_list=data_list,
    count=len(time_list)
)
# 在通达信公式管理器中用 SIGNALS_TQ(1,0)~SIGNALS_TQ(4,0) 引用上述数据
tq.close()
```

---

### 示例9：交易接口完整流程

```python
from tqcenter import tq, tqconst
tq.initialize(__file__)

# 获取账户句柄（须先在客户端登录）
account_id = tq.stock_account(account="1190008847", account_type="STOCK")
print(f"账户句柄: {account_id}")  # ≥0 有效

# 查询持仓
positions = tq.query_stock_positions(account_id=account_id)
for pos in positions:
    print(f"{pos['Code']}: 成本价={pos['Cbj']}, 总持仓={pos['TotalVol']}")

# 查询委托
orders = tq.query_stock_orders(account_id=account_id, stock_code="")
print(f"当日委托: {orders}")

# 下单（自填价买入）
result = tq.order_stock(
    account_id=account_id,
    stock_code="688318.SH",
    order_type=tqconst.STOCK_BUY,
    order_volume=100,
    price_type=tqconst.PRICE_MY,
    price=130.0
)
print(result)  # {'ErrorId': '0', 'Msg': '已发送信号至客户端，待用户确认！', 'Value': 1}

tq.close()
```

---

## 故障排除

### "TQ数据接口初始化失败或已有同名策略运行"
1. 确保通达信客户端已运行并登录
2. 使用 `sys.path.insert(0, ...)` 而非 `sys.path.append()`
3. 确认 `PYPlugins/` 目录下存在 `TPyth.dll` 和 `TPythClient.dll`
4. 检查是否已有相同 `__file__` 路径的策略在运行（ErrorId='12'）

### 菜单一直显示"正在开启TQ策略..."
检查是否有防火墙弹窗，若有请允许访问。

### 获取数据时指标值前面是 None
`count` 参数设置的K线数量不足以覆盖公式计算中的最大参数值。例如 `MA(C,20)` 需要至少20根K线，若 `count=10` 则前10根K线的MA值为 None。

### `send_warn` 报 ValueError
- 确认 `count > 0`
- 确认 `stock_list`、`price_list`、`close_list`、`volum_list` 的长度均 ≥ `count`
- 确认 `price_list`、`close_list`、`volum_list` 中的元素均为纯数字字符串

### 批量公式选股结果比客户端少
`count` 参数设置太小，不够覆盖公式计算所需的最大回溯K线数，适当增大 `count` 值。

### Module Not Found 错误
- 确认 `PYPlugins/user` 路径正确
- 使用绝对路径而非相对路径
- 使用 `sys.path.insert(0, ...)` 保证优先加载

---

## 技术说明

### 版本信息
- **tqcenter.py 版本**：1.0.6（2026-03-27）
- **Python 支持**：3.7 ~ 3.14（64位），推荐 3.13

### 依赖项
- **TPyth.dll / TPythClient.dll**：与通达信客户端通信，位于 `PYPlugins/` 目录下（`tqcenter.py` 所在目录的上级目录）
- **Python库**：`json`, `ctypes`, `numpy`, `pandas`, `weakref`, `sys`, `os`, `shutil`, `time`, `pathlib`, `typing`, `collections`, `datetime`, `re`, `atexit`, `inspect`, `concurrent.futures`, `threading`
- **可选第三方库**：`vectorbt`（回测），`plotly`（图形）

### refresh_cache 市场代码对照表
| 字符串 | 含义 |
|--------|------|
| `'AG'` | A股（默认） |
| `'QH'` | 国内期货 |
| `'HK'` | 港股 |
| `'US'` | 美股 |
| `'NQ'` | 新三板 |
| `'QQ'` | 股票期权 |
| `'ZZ'` | 中证指数 |
| `'ZS'` | 综合指数 |
| `'OF'` | 开放式基金 |

---
