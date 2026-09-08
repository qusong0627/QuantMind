
from .other import Meta

__all__ = ["MaType", "Model", "LineDash", "Markers", "Colors"]


class MaType(int, metaclass=Meta):
    """
    ### 均线类型枚举类（继承int类型，便于数值传递和比较）

    该类定义了所有支持的均线计算类型，每个属性对应一个唯一的整数值，
    可直接用于均线指标的参数配置、逻辑判断等场景。

    #### 属性说明
    - SMA : int
        简单移动平均线（Simple Moving Average），取值为0
    - EMA : int
        指数移动平均线（Exponential Moving Average），取值为1
    - WMA : int
        加权移动平均线（Weighted Moving Average），取值为2
    - DEMA : int
        双指数移动平均线（Double Exponential Moving Average），取值为3
    - TEMA : int
        三重指数移动平均线（Triple Exponential Moving Average），取值为4
    - TRIMA : int
        三角移动平均线（Triangular Moving Average），取值为5
    - KAMA : int
        适应性移动平均线（Kaufman Adaptive Moving Average），取值为6
    - MAMA : int
        自适应移动平均线（MESA Adaptive Moving Average），取值为7
    - T3 : int
        T3移动平均线（T3 Moving Average），取值为8

    #### 可选值汇总
    >>> SMA = 0
        EMA = 1
        WMA = 2
        DEMA = 3
        TEMA = 4
        TRIMA = 5
        KAMA = 6
        MAMA = 7
        T3 = 8
    """
    SMA = 0
    EMA = 1
    WMA = 2
    DEMA = 3
    TEMA = 4
    TRIMA = 5
    KAMA = 6
    MAMA = 7
    T3 = 8


class Model(str, metaclass=Meta):
    """
    ### 多线程/并行计算库枚举类（继承str类型，可直接作为参数传入相关函数）

    - 该类定义了所有支持的并行计算框架，属性值与对应库名称一致，
    - 用于指定数据处理、模型训练等场景的并行计算引擎。

    #### 属性说明
    - joblib : str
        joblib库，适用于小型任务并行、内存数据缓存，取值为"joblib"
    - dask : str
        Dask库，适用于大规模数据集、分布式计算，取值为"dask"
    - sklearn : str
        scikit-learn内置并行引擎，适用于sklearn模型的并行训练，取值为"sklearn"
    - multiprocessing : str
        Python内置multiprocessing库，适用于通用多进程并行，取值为"multiprocessing"

    #### 可选值汇总
    >>> ['dask', 'joblib', 'sklearn', 'multiprocessing']
    """
    joblib = "joblib"
    dask = "dask"
    sklearn = "sklearn"
    multiprocessing = "multiprocessing"


class LineDash(str, metaclass=Meta):
    """
    ### 绘图线型枚举类（继承str类型，可直接用于可视化库的线型配置）

    - 该类定义了所有支持的图表线条样式，属性值与可视化库。
    - 兼容的线型名称一致，用于配置K线图、指标线等图表的线条展示样式。

    #### 属性说明
    - solid : str
        实线样式，取值为'solid'，适用于常规指标线展示
    - dashed : str
        虚线样式，取值为'dashed'，适用于辅助线、压力线展示
    - dotted : str
        点线样式，取值为'dotted'，适用于次要参考线展示
    - dotdash : str
        点虚线样式（点-横交替），取值为'dotdash'，适用于特殊标记线展示
    - dashdot : str
        虚点线样式（横-点交替），取值为'dashdot'，与dotdash样式互补
    - vbar : str
        竖线/柱状样式，取值为'vbar'，适用于成交量、乖离率等柱状图展示

    #### lightweight_charts
    LINE_STYLE = Literal['solid', 'dotted', 'dashed', 'large_dashed', 'sparse_dotted']

    #### 可选值汇总
    >>> ['solid', 'dashed', 'dotted', 'dotdash', 'dashdot', 'vbar']
    """
    solid = 'solid'
    dashed = 'dashed'
    dotted = 'dotted'
    dotdash = 'dotdash'
    dashdot = 'dashdot'
    vbar = 'vbar'
    ######### lightweight_charts##########
    large_dashed = "large_dashed"
    sparse_dotted = "sparse_dotted"


class Markers(str, metaclass=Meta):
    """
    ### 绘图标记样式枚举类（继承str类型，可直接用于可视化库的标记配置）

    - 该类定义了所有支持的图表标记样式，属性值与可视化库。
    - 兼容的标记名称一致，用于配置图表中数据点、买卖信号等的标记展示样式。

    #### 属性说明
    - asterisk : str
        星号标记，对应符号"*"，取值为"asterisk"，用于重要信号标记
    - cross : str
        十字标记，对应符号"+"，取值为"cross"，用于普通数据点标记
    - circle : str
        圆形标记，对应符号"o"，取值为"circle"，适用于常规数据点展示
    - circle_cross : str
        圆形十字复合标记，对应符号"o+"，取值为"circle_cross"，用于关键数据点标记
    - circle_dot : str
        圆形点复合标记，对应符号"o."，取值为"circle_dot"，用于次要数据点标记
    - circle_x : str
        圆形X复合标记，对应符号"ox"，取值为"circle_x"，用于负面信号标记
    - circle_y : str
        圆形Y复合标记，对应符号"oy"，取值为"circle_y"，用于特殊信号标记
    - dash : str
        短横线标记，对应符号"-"，取值为"dash"，用于线性数据点标记
    - dot : str
        点标记，对应符号"."，取值为"dot"，用于密集数据点展示
    - inverted_triangle : str
        倒三角形标记，对应符号"v"，取值为"inverted_triangle"，用于卖出信号标记
    - triangle : str
        正三角形标记，对应符号"^"，取值为"triangle"，用于买入信号标记
    - triangle_dot : str
        三角形点复合标记，对应符号"^."，取值为"triangle_dot"，用于潜在买卖信号标记

    #### lightweight_charts
    - MARKER_SHAPE = Literal['arrow_up', 'arrow_down', 'circle', 'square']

    #### 符号与标记映射
    - "*"  : "asterisk"（星号）
    - "+"  : "cross"（十字）
    - "o"  : "circle"（圆形）
    - "o+" : "circle_cross"（圆形十字）
    - "o." : "circle_dot"（圆形点）
    - "ox" : "circle_x"（圆形X）
    - "oy" : "circle_y"（圆形Y）
    - "-"  : "dash"（短横线）
    - "."  : "dot"（点）
    - "v"  : "inverted_triangle"（倒三角形）
    - "^"  : "triangle"（正三角形）
    - "^." : "triangle_dot"（三角形点）
    """
    asterisk = "asterisk"
    cross = "cross"
    circle = "circle"
    circle_cross = "circle_cross"
    circle_dot = "circle_dot"
    circle_x = "circle_x"
    circle_y = "circle_y"
    dash = "dash"
    dot = "dot"
    inverted_triangle = "inverted_triangle"
    triangle = "triangle"
    triangle_dot = "triangle_dot"
    ######### lightweight_charts##########
    arrow_up = "arrow_up"
    arrow_down = "arrow_down"
    square = "square"


class Colors(str, metaclass=Meta):
    """
    ### 颜色枚举类（继承str类型，可直接用于可视化库的颜色配置）

    - 该类定义了大量标准Web颜色及自定义RGB十六进制颜色，属性值与主流可视化库
    - （如Bokeh、Plotly、Seaborn）兼容的颜色名称/十六进制字符串一致，
    - 可直接用于图表元素（线条、标记、填充、文字等）的配色配置，提升可视化效果的规范性。

    #### 可选值汇总
    >>> ["aliceblue", "antiquewhite", "aqua", "aquamarine", "azure",
        "beige", "bisque", "black", "blanchedalmond", "blue",
        "blueviolet", "brown", "burlywood", "cadetblue", "chartreuse",
        "chocolate", "coral", "cornflowerblue", "cornsilk", "crimson",
        "cyan", "darkblue", "darkcyan", "darkgoldenrod", "darkgray",
        "darkgreen", "darkgrey", "darkkhaki", "darkmagenta",
        "darkolivegreen", "darkorange", "darkorchid", "darkred",
        "darksalmon", "darkseagreen", "darkslateblue", "darkslategray",
        "darkslategrey", "darkturquoise", "darkviolet", "deeppink",
        "deepskyblue", "dimgray", "dimgrey", "dodgerblue", "firebrick",
        "floralwhite", "forestgreen", "fuchsia", "gainsboro", "ghostwhite",
        "gold", "goldenrod", "gray", "green", "greenyellow", "grey",
        "honeydew", "hotpink", "indianred", "indigo", "ivory", "khaki",
        "lavender", "lavenderblush", "lawngreen", "lemonchiffon",
        "lightblue", "lightcoral", "lightcyan", "lightgoldenrodyellow",
        "lightgray", "lightgreen", "lightgrey", "lightpink", "lightsalmon",
        "lightseagreen", "lightskyblue", "lightslategray",
        "lightslategrey", "lightsteelblue", "lightyellow",
        "lime", "limegreen", "linen", "magenta", "maroon",
        "mediumaquamarine", "mediumblue", "mediumorchid",
        "mediumpurple", "mediumseagreen", "mediumslateblue",
        "mediumspringgreen", "mediumturquoise", "mediumvioletred",
        "midnightblue", "mintcream", "mistyrose", "moccasin", "navajowhite",
        "navy", "oldlace", "olive", "olivedrab", "orange", "orangered",
        "orchid", "palegoldenrod", "palegreen", "paleturquoise",
        "palevioletred", "papayawhip", "peachpuff", "peru", "pink",
        "plum", "powderblue", "purple", "rebeccapurple", "red",
        "rosybrown", "royalblue", "saddlebrown", "salmon", "sandybrown",
        "seagreen", "seashell", "sienna", "silver", "skyblue", "slateblue",
        "slategray", "slategrey", "snow", "springgreen", "steelblue",
        "tan", "teal", "thistle", "tomato", "turquoise", "violet",
        "wheat", "white", "whitesmoke", "yellow", "yellowgreen",]
    """
    aliceblue = "aliceblue"
    antiquewhite = "antiquewhite"
    aqua = "aqua"
    aquamarine = "aquamarine"
    azure = "azure"
    beige = "beige"
    bisque = "bisque"
    black = "black"
    blanchedalmond = "blanchedalmond"
    blue = "blue"
    blueviolet = "blueviolet"
    brown = "brown"
    burlywood = "burlywood"
    cadetblue = "cadetblue"
    chartreuse = "chartreuse"
    chocolate = "chocolate"
    coral = "coral"
    cornflowerblue = "cornflowerblue"
    cornsilk = "cornsilk"
    crimson = "crimson"
    cyan = "cyan"
    darkblue = "darkblue"
    darkcyan = "darkcyan"
    darkgoldenrod = "darkgoldenrod"
    darkgray = "darkgray"
    darkgreen = "darkgreen"
    darkgrey = "darkgrey"
    darkkhaki = "darkkhaki"
    darkmagenta = "darkmagenta"
    darkolivegreen = "darkolivegreen"
    darkorange = "darkorange"
    darkorchid = "darkorchid"
    darkred = "darkred"
    darksalmon = "darksalmon"
    darkseagreen = "darkseagreen"
    darkslateblue = "darkslateblue"
    darkslategray = "darkslategray"
    darkslategrey = "darkslategrey"
    darkturquoise = "darkturquoise"
    darkviolet = "darkviolet"
    deeppink = "deeppink"
    deepskyblue = "deepskyblue"
    dimgray = "dimgray"
    dimgrey = "dimgrey"
    dodgerblue = "dodgerblue"
    firebrick = "firebrick"
    floralwhite = "floralwhite"
    forestgreen = "forestgreen"
    fuchsia = "fuchsia"
    gainsboro = "gainsboro"
    ghostwhite = "ghostwhite"
    gold = "gold"
    goldenrod = "goldenrod"
    gray = "gray"
    green = "green"
    greenyellow = "greenyellow"
    grey = "grey"
    honeydew = "honeydew"
    hotpink = "hotpink"
    indianred = "indianred"
    indigo = "indigo"
    ivory = "ivory"
    khaki = "khaki"
    lavender = "lavender"
    lavenderblush = "lavenderblush"
    lawngreen = "lawngreen"
    lemonchiffon = "lemonchiffon"
    lightblue = "lightblue"
    lightcoral = "lightcoral"
    lightcyan = "lightcyan"
    lightgoldenrodyellow = "lightgoldenrodyellow"
    lightgray = "lightgray"
    lightgreen = "lightgreen"
    lightgrey = "lightgrey"
    lightpink = "lightpink"
    lightsalmon = "lightsalmon"
    lightseagreen = "lightseagreen"
    lightskyblue = "lightskyblue"
    lightslategray = "lightslategray"
    lightslategrey = "lightslategrey"
    lightsteelblue = "lightsteelblue"
    lightyellow = "lightyellow"
    lime = "lime"
    limegreen = "limegreen"
    linen = "linen"
    magenta = "magenta"
    maroon = "maroon"
    mediumaquamarine = "mediumaquamarine"
    mediumblue = "mediumblue"
    mediumorchid = "mediumorchid"
    mediumpurple = "mediumpurple"
    mediumseagreen = "mediumseagreen"
    mediumslateblue = "mediumslateblue"
    mediumspringgreen = "mediumspringgreen"
    mediumturquoise = "mediumturquoise"
    mediumvioletred = "mediumvioletred"
    midnightblue = "midnightblue"
    mintcream = "mintcream"
    mistyrose = "mistyrose"
    moccasin = "moccasin"
    navajowhite = "navajowhite"
    navy = "navy"
    oldlace = "oldlace"
    olive = "olive"
    olivedrab = "olivedrab"
    orange = "orange"
    orangered = "orangered"
    orchid = "orchid"
    palegoldenrod = "palegoldenrod"
    palegreen = "palegreen"
    paleturquoise = "paleturquoise"
    palevioletred = "palevioletred"
    papayawhip = "papayawhip"
    peachpuff = "peachpuff"
    peru = "peru"
    pink = "pink"
    plum = "plum"
    powderblue = "powderblue"
    purple = "purple"
    rebeccapurple = "rebeccapurple"
    red = "red"
    rosybrown = "rosybrown"
    royalblue = "royalblue"
    saddlebrown = "saddlebrown"
    salmon = "salmon"
    sandybrown = "sandybrown"
    seagreen = "seagreen"
    seashell = "seashell"
    sienna = "sienna"
    silver = "silver"
    skyblue = "skyblue"
    slateblue = "slateblue"
    slategray = "slategray"
    slategrey = "slategrey"
    snow = "snow"
    springgreen = "springgreen"
    steelblue = "steelblue"
    tan = "tan"
    teal = "teal"
    thistle = "thistle"
    tomato = "tomato"
    turquoise = "turquoise"
    violet = "violet"
    wheat = "wheat"
    white = "white"
    whitesmoke = "whitesmoke"
    yellow = "yellow"
    yellowgreen = "yellowgreen"
    RGB666666 = "#666666"
    
    bear_color = 'rgba(200, 97, 100, 100)'
    bull_color = 'rgba(39, 157, 130, 100)'
    # 退出信号颜色（与入场信号区分）
    exit_long_color = "orange"      # 多头平仓信号颜色
    exit_short_color = "#908EEB"       # 空头平仓信号颜色
