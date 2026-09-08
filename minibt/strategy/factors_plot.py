import numpy as np
import pandas as pd
from scipy import stats
# from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LinearRegression
from tqdm import tqdm
from bokeh.plotting import figure  # , show, output_file
from bokeh.layouts import gridplot, row, column, layout
from bokeh.models import ColumnDataSource, HoverTool, ColorBar, LinearColorMapper, Range1d, Div, Tabs, LabelSet, PrintfTickFormatter, Paragraph
from bokeh.palettes import Spectral6, Viridis256, Category10, Plasma256
from bokeh.transform import linear_cmap
import holoviews as hv
# from holoviews import opts
# import pandas_ta as pta
# 处理Bokeh版本差异
try:
    from bokeh.models import TabPanel
except ImportError:
    from bokeh.models import Panel as TabPanel

hv.extension('bokeh')
# ======================
# Bokeh工具函数 - 改进自适应布局
# ======================


def create_bar_plot(data, title, x_label, y_label, height=400):
    """创建柱状图 - 改进自适应宽度"""
    # 确保x轴数据是字符串类型
    data = data.copy()
    values = data.values
    data[x_label] = data[x_label].astype(str)

    source = ColumnDataSource(data=data)
    # 不设置固定宽度，使用自适应
    p = figure(
        title=title,
        x_range=data[x_label].tolist(),
        height=height,
        tools="pan,wheel_zoom,box_zoom,reset,save",
        sizing_mode="stretch_width"  # 让图表宽度随父布局拉伸
    )

    # 创建颜色映射
    colors = Spectral6[:len(data)] if len(
        data) <= 6 else Category10[10][:len(data)]

    p.vbar(x=values[:, 0], top=values[:, 1], width=0.9,
           fill_color=colors, line_color="black")

    p.add_tools(HoverTool(
        tooltips=[
            ("分组", f"@{x_label}"),
            ("收益", f"@{y_label}{{0.0000}}")
        ]
    ))

    p.xaxis.axis_label = x_label
    p.yaxis.axis_label = y_label
    p.xgrid.grid_line_color = None
    min_val = min(0, min(data[y_label]) - 0.001)
    max_val = max(data[y_label]) + 0.001
    p.y_range = Range1d(start=min_val, end=max_val)
    return p


def create_line_plot(x_data, y_data, title, x_label, y_label, height=400,
                     color="blue", line_width=2, legend_label="折线图"):
    """创建折线图 - 改进自适应宽度"""
    # 不设置固定宽度，使用自适应
    p = figure(title=title, height=height,
               tools="pan,wheel_zoom,box_zoom,reset,save",
               sizing_mode="stretch_width")

    if isinstance(y_data, dict):
        for label, y in y_data.items():
            p.line(x_data, y, line_width=line_width, color=Category10[10][list(y_data.keys()).index(label)],
                   legend_label=label)
    else:
        p.line(x_data, y_data, line_width=line_width,
               color=color, legend_label=legend_label)

    p.add_tools(HoverTool(
        tooltips=[
            (x_label, "@x"),
            (y_label, "@y{0.0000}")
        ],
        mode='vline'
    ))

    p.xaxis.axis_label = x_label
    p.yaxis.axis_label = y_label
    p.legend.location = "top_left"
    p.legend.click_policy = "hide"
    return p


def create_scatter_plot(x_data, y_data, title, x_label, y_label, height=400):
    """创建散点图 - 改进自适应宽度"""
    source = ColumnDataSource(data=dict(x=x_data, y=y_data))
    # 不设置固定宽度，使用自适应
    p = figure(
        title=title,
        height=height,
        tools="pan,wheel_zoom,box_zoom,reset,save",
        sizing_mode="stretch_width"  # 宽度自适应
    )
    p.scatter('x', 'y', source=source, size=5, alpha=0.4, color="navy")

    # 添加回归线
    slope, intercept, r_value, p_value, std_err = stats.linregress(
        x_data, y_data)
    regression_line = slope * np.array(x_data) + intercept
    p.line(x_data, regression_line, color="red", line_width=2,
           legend_label=f"回归线 (斜率={slope:.4f}, R²={r_value**2:.4f}")

    p.add_tools(HoverTool(
        tooltips=[
            (x_label, "@x{0.0000}"),
            (y_label, "@y{0.0000}")
        ]
    ))

    p.xaxis.axis_label = x_label
    p.yaxis.axis_label = y_label
    return p


def create_heatmap(data, title, height=500):
    """创建热力图 - 改进自适应宽度，添加方框内的相关系数标注"""
    df = data.stack().reset_index()
    df.columns = ['x', 'y', 'value']

    # 确保x和y是字符串
    df['x'] = df['x'].astype(str)
    df['y'] = df['y'].astype(str)

    # 使用更浅的渐变色板
    mapper = linear_cmap(field_name='value', palette="Viridis256",
                         low=min(df['value']), high=max(df['value']))

    source = ColumnDataSource(df)
    # 不设置固定宽度，使用自适应
    p = figure(title=title, x_range=list(data.index.astype(str)),
               y_range=list(data.columns.astype(str))[::-1],
               height=height,
               tools="hover,save,reset",
               tooltips=[('因子', '@x, @y'), ('相关系数', '@value{0.00}')],
               sizing_mode="stretch_width")

    p.rect(x="x", y="y", width=1, height=1, source=source,
           line_color=None, fill_color=mapper)

    # 根据值的正负设置不同的文本颜色，确保可读性
    df['text_color'] = ['white' if v < 0.8 else 'black' for v in df['value']]
    labels_source = ColumnDataSource(df)

    # 添加相关系数文本标注
    labels = LabelSet(x="x", y="y", text="value", text_align="center",
                      text_baseline="middle", source=labels_source,
                      text_color="text_color", text_font_size="10pt")
    p.add_layout(labels)

    color_bar = ColorBar(color_mapper=mapper['transform'], width=8,
                         location=(0, 0), title="相关系数")
    p.add_layout(color_bar, 'right')

    p.xaxis.major_label_orientation = 45
    p.grid.grid_line_color = None
    p.axis.axis_line_color = None
    return p
# ======================
# 新增：计算累计收益函数
# ======================


def calculate_cumulative_returns(returns, periods=[1, 3, 5, 10]):
    """计算不同预测期的累计收益"""
    cumulative_returns = {}

    for period in periods:
        # 计算滚动累计收益 (1+R1)*(1+R2)*...-1
        rolling_cumulative = (
            1 + returns).rolling(period).apply(np.prod, raw=True) - 1
        cumulative_returns[period] = rolling_cumulative

    return cumulative_returns

# ======================
# 新增：创建累计收益图表
# ======================


def create_cumulative_return_plot(cumulative_returns, title, height=400):
    """创建累计收益折线图"""
    # 确保所有序列长度一致（可能需要处理NaN）
    dates = cumulative_returns[list(cumulative_returns.keys())[0]].index

    # 创建图表
    p = figure(
        title=title,
        x_axis_type='datetime',
        height=height,
        tools="pan,wheel_zoom,box_zoom,reset,save",
        sizing_mode="stretch_width"
    )

    # 为每个预测期添加曲线
    colors = Category10[10]
    for i, (period, returns) in enumerate(cumulative_returns.items()):
        p.line(
            x=dates,
            y=returns,
            line_width=2,
            color=colors[i % len(colors)],
            legend_label=f"{period}天累计收益"
        )

    # 添加水平线标记0
    p.line(x=dates, y=[0]*len(dates), line_dash='dashed', line_color='black')

    # 添加悬停工具
    p.add_tools(HoverTool(
        tooltips=[
            ("日期", "@x{%F}"),
            ("累计收益", "@y{0.00%}")
        ],
        formatters={'@x': 'datetime'},
        mode='mouse'
    ))

    p.xaxis.axis_label = "日期"
    p.yaxis.axis_label = "累计收益"
    p.yaxis.formatter = PrintfTickFormatter(
        format="%0.2f%%")  # 使用 PrintfTickFormatter 格式化百分比
    p.legend.location = "top_left"
    p.legend.click_policy = "hide"

    return p

# ======================
# 单因子分析流程修改（添加文字分析总结）
# ======================


def single_factor_analysis(factor_data, periods=[1, 3, 5, 10], n_groups=5):
    """
    单因子分析流程（支持多期预测，添加文字分析总结）
    :param factor_data: 包含因子值和收益率的数据框
    :param factor_name: 分析的因子名称
    :param periods: 预测未来收益的期数列表
    :param n_groups: 分组数量
    """
    # 复制数据避免修改原始数据
    # df = factor_data.copy()
    df = factor_data
    factor_name = df.lines

    # 存储结果和图表
    results = {"name": factor_name}
    group_plots = []
    scatter_plots = []

    # 因子分组分析标准说明
    group_analysis_criteria = f"""
    <h3>因子分组分析标准</h3>
    1. 分组作用：将股票（或资产）按因子值从小到大分成{n_groups}组，观察不同组未来收益差异，判断因子对收益的预测能力。<br>
    2. 分组标准：使用 pd.qcut 按因子值分位数分组，确保每组样本数量大致相等（处理重复值时可能调整）。<br>
    3. 有效判断：若因子值越大的组，未来收益越高（或越低，依因子逻辑），说明因子能区分收益，预测能力强。
    """

    # 对每个预测期进行分析
    for period in periods:
        # 计算未来收益
        df[f'forward_{period}'] = df['return'].shift(-period)
        # 使用copy避免SettingWithCopyWarning
        df_temp = df.dropna(subset=[f'forward_{period}']).copy()

        # 按因子值分组 - 使用.loc避免警告
        df_temp.loc[:, 'group'] = pd.qcut(
            df_temp[factor_name], n_groups, labels=False, duplicates='drop')

        # 计算每组的平均收益
        group_returns = df_temp.groupby(
            'group')[f'forward_{period}'].mean().reset_index()
        group_returns.columns = ['Group', f'{period}天收益']

        # 计算IC（信息系数）
        ic = df_temp[[factor_name, f'forward_{period}']].corr(
            method='spearman').iloc[0, 1]

        # 计算因子收益率（多空组合）
        long_group = group_returns.loc[group_returns[f'{period}天收益'].idxmax(
        ), 'Group']
        short_group = group_returns.loc[group_returns[f'{period}天收益'].idxmin(
        ), 'Group']
        long_short_return = group_returns.loc[long_group,
                                              f'{period}天收益'] - group_returns.loc[short_group, f'{period}天收益']

        # 存储结果
        results[period] = {
            'ic': ic,
            'long_short_return': long_short_return,
            'group_returns': group_returns
        }

        # 创建分组收益柱状图
        bar_plot = create_bar_plot(
            group_returns,
            title=f'{period}天预测期 - 分组收益 (IC: {ic:.4f})',
            x_label='Group',
            y_label=f'{period}天收益',
            height=350
        )

        # 当期分组收益分析总结
        group_analysis_summary = f"""
        <h4>{period}天预测期 - 分组收益分析</h4>
        - IC值：{ic:.4f}，IC绝对值越接近1，因子与收益相关性越强；此处IC接近0，因子预测能力弱。<br>
        - 多空收益：{long_short_return:.6f}，多空收益为正说明做多高因子值组、做空低因子值组可能盈利（反之则反）；此处多空收益绝对值小，策略价值低。<br>
        - 分组收益分布：各组收益无明显单调趋势（若有，应是Group0到Group{n_groups-1}收益逐渐递增/递减），因子对收益区分度差。
        """
        group_plots.append(column(Div(text=group_analysis_summary),
                           bar_plot, sizing_mode="stretch_width", width_policy='max'))

        # 创建因子与未来收益的散点图
        scatter_plot = create_scatter_plot(
            x_data=df_temp[factor_name].values,
            y_data=df_temp[f'forward_{period}'].values,
            title=f'{period}天预测期 - 因子与收益关系',
            x_label=factor_name,
            y_label=f'{period}天未来收益',
            height=350
        )

        # 当期因子-收益关系分析总结
        r_squared = (stats.linregress(
            df_temp[factor_name].values, df_temp[f'forward_{period}'].values)[2])**2
        scatter_analysis_summary = f"""
        <h4>{period}天预测期 - 因子与收益关系分析</h4>
        - 散点分布：若点集中分布在回归线附近，说明因子与收益线性关系强；此处散点较分散，关系较弱。<br>
        - 回归线斜率：回归线斜率为{stats.linregress(df_temp[factor_name].values, df_temp[f'forward_{period}'].values)[0]:.4f}，表示因子每增加1单位，预期收益变化量；斜率接近0说明影响微弱。<br>
        - R²值：{r_squared:.4f}，表示因子解释收益变动的比例；此处R²低，因子解释力有限。
        """
        scatter_plots.append(column(Div(text=scatter_analysis_summary),
                             scatter_plot, sizing_mode="stretch_width", width_policy='max'))

    # 新增：计算累计收益
    cumulative_returns = calculate_cumulative_returns(df['return'], periods)
    cumulative_return_plot = create_cumulative_return_plot(
        cumulative_returns,
        f'不同预测期的累计收益 - 因子{factor_name}',
        height=400
    )

    # 累计收益分析总结
    cum_return_summary = f"""
    <h3>累计收益分析总结</h3>
    - 分析标准：通过比较不同预测期的累计收益曲线，评估因子在不同时间维度上的持续性和稳定性。<br>
    - 趋势观察：若曲线持续向上，说明因子长期有效；若波动剧烈，说明因子不稳定。<br>
    - 周期对比：短期（如1天）收益波动通常较大，长期（如10天）收益更能反映因子的真实效果。
    """

    # 创建分组图布局 - 改进布局
    group_layout = column(
        Div(text=group_analysis_criteria),
        *group_plots, sizing_mode="stretch_width", width_policy='max')
    scatter_layout = column(
        *scatter_plots, sizing_mode="stretch_width", width_policy='max')

    # 创建多期IC和收益比较图
    ic_values = [results[p]['ic'] for p in periods]
    ls_returns = [results[p]['long_short_return'] for p in periods]

    ic_plot = create_line_plot(
        x_data=periods,
        y_data=ic_values,
        title='不同预测期的信息系数(IC)',
        x_label='预测期(天)',
        y_label='IC值',
        color="blue",
        legend_label="IC值"
    )
    ic_plot.line(periods, [0]*len(periods),
                 line_dash='dashed', line_color='red')

    ls_plot = create_line_plot(
        x_data=periods,
        y_data=ls_returns,
        title='不同预测期的多空收益',
        x_label='预测期(天)',
        y_label='多空收益',
        color="green",
        legend_label="多空收益"
    )
    ls_plot.line(periods, [0]*len(periods),
                 line_dash='dashed', line_color='red')

    # 多期表现分析总结
    multi_period_summary = f"""
    <h3>多期表现分析总结</h3>
    - IC稳定性：IC值在各预测期的波动反映因子预测能力的稳定性。若IC值持续为正（或负），说明因子方向稳定。<br>
    - 最佳预测期：观察发现，{max(periods, key=lambda p: abs(results[p]['ic']))}天预测期的IC绝对值最大({max(abs(results[p]['ic']) for p in periods):.4f})，表明该因子在该周期预测效果最佳。<br>
    - 多空收益：多空收益曲线若持续高于0，说明因子可用于构建多空策略；若波动频繁穿越0线，策略风险较高。
    """

    multi_period_layout = column(
        Div(text=multi_period_summary),
        row(ic_plot, ls_plot, sizing_mode="stretch_width"),
        sizing_mode="stretch_width", width_policy='max'
    )

    # 创建单因子分析结果面板
    tabs = Tabs(tabs=[
        TabPanel(child=column(
            Div(text=f"<h2>因子 {factor_name} 分组分析</h2>"),
            group_layout, width_policy='max'
        ), title="分组收益"),

        TabPanel(child=column(
            Div(text=f"<h2>因子 {factor_name} 相关性分析</h2>"),
            scatter_layout, width_policy='max'
        ), title="因子-收益关系"),

        TabPanel(child=column(
            Div(text=f"<h2>因子 {factor_name} 多期表现</h2>"),
            multi_period_layout, width_policy='max'
        ), title="多期表现"),

        # 新增：累计收益分析
        TabPanel(child=column(
            Div(text=f"<h2>因子 {factor_name} 累计收益分析</h2>"),
            Div(text=cum_return_summary),
            cumulative_return_plot, width_policy='max'
        ), title="累计收益")
    ], sizing_mode="stretch_both", width_policy='max')

    # 显示结果 - 使用return而不是show，在外部统一显示
    return results, tabs

# ======================
# 多因子分析流程修改（添加文字分析总结）
# ======================


def multi_factor_analysis(factor_data, periods=[1, 3, 5, 10]):
    """
    多因子分析流程（支持多期预测，添加文字分析总结）
    :param factor_data: 包含因子值和收益率的数据框
    :param factor_names: 分析的因子名称列表
    :param periods: 预测未来收益的期数列表
    """
    # 复制数据避免修改原始数据
    df = factor_data  # .copy()
    factor_names = df.lines

    # 存储结果
    results = {}

    # 因子相关性分析标准说明
    corr_analysis_criteria = """
    <h3>因子相关性分析标准</h3>
    1. 分析目的：评估因子间的冗余性和互补性，避免在组合中使用高度相关的因子。<br>
    2. 判断标准：相关系数接近1表示强正相关，接近-1表示强负相关，接近0表示不相关。<br>
    3. 应用建议：构建多因子模型时，应优先选择低相关性的因子，以增强模型的稳定性和解释力。
    """

    # 因子相关性分析（与预测期无关）
    factor_corr = df[factor_names].corr()

    # 创建因子相关性热图
    heatmap = create_heatmap(
        data=factor_corr,
        title='因子相关性矩阵',
        height=500
    )

    # 对每个预测期进行分析
    for period in tqdm(periods, desc="多因子多期分析进度"):
        # 计算未来收益
        df[f'forward_{period}'] = df['return'].shift(-period)
        # 使用copy避免SettingWithCopyWarning
        df_temp = df.dropna(subset=[f'forward_{period}']).copy()

        # 因子合成（等权加权）
        df_temp.loc[:, 'composite_factor'] = df_temp[factor_names].mean(axis=1)

        # 计算因子IC
        ic_results = {}
        for factor in factor_names:
            ic = df_temp[[factor, f'forward_{period}']].corr(
                method='spearman').iloc[0, 1]
            ic_results[factor] = ic

        # 计算合成因子IC
        composite_ic = df_temp[['composite_factor', f'forward_{period}']].corr(
            method='spearman').iloc[0, 1]
        ic_results['composite_factor'] = composite_ic

        # 因子回归分析
        X = df_temp[factor_names]
        y = df_temp[f'forward_{period}']
        model = LinearRegression()
        model.fit(X, y)
        r_squared = model.score(X, y)
        factor_returns = pd.Series(model.coef_, index=factor_names)

        # 计算合成因子的多空收益
        df_temp.loc[:, 'composite_rank'] = pd.qcut(
            df_temp['composite_factor'], 5, labels=False)
        long_group = df_temp[df_temp['composite_rank'] == 4]
        short_group = df_temp[df_temp['composite_rank'] == 0]
        composite_long_short_return = long_group[f'forward_{period}'].mean(
        ) - short_group[f'forward_{period}'].mean()

        # 存储结果
        results[period] = {
            'ic_results': ic_results,
            'r_squared': r_squared,
            'factor_returns': factor_returns,
            'composite_long_short_return': composite_long_short_return
        }

    # 新增：计算累计收益
    cumulative_returns = calculate_cumulative_returns(df['return'], periods)
    cumulative_return_plot = create_cumulative_return_plot(
        cumulative_returns,
        '不同预测期的累计收益 - 多因子组合',
        height=400
    )

    # 累计收益分析总结
    cum_return_summary = f"""
    <h3>累计收益分析总结</h3>
    - 分析标准：通过比较不同预测期的累计收益曲线，评估多因子组合在不同时间维度上的持续性和稳定性。<br>
    - 与单因子对比：若多因子组合的累计收益曲线优于任一单因子，说明因子间存在互补性；反之则需重新筛选因子。<br>
    - 波动特征：观察曲线的波动幅度和回撤深度，评估多因子组合的风险控制能力。
    """

    # 创建IC分析图表
    ic_data = {}
    for factor in factor_names + ['composite_factor']:
        ic_values = [results[p]['ic_results'][factor] for p in periods]
        ic_data[factor] = ic_values

    ic_plot = figure(
        title='不同预测期的因子IC值',
        x_range=[str(p) for p in periods],
        height=400,
        tools="pan,wheel_zoom,box_zoom,reset,save",
        sizing_mode="stretch_width"
    )

    for i, (factor, values) in enumerate(ic_data.items()):
        ic_plot.line(
            x=[str(p) for p in periods],
            y=values,
            line_width=2,
            color=Category10[10][i % len(Category10[10])],
            legend_label=factor
        )
        # 替换为 scatter 方法
        ic_plot.scatter(
            x=[str(p) for p in periods],
            y=values,
            size=8,
            color=Category10[10][i % len(Category10[10])]
        )

    ic_plot.line(x=[str(p) for p in periods], y=[
        0]*len(periods), line_dash='dashed', line_color='red')
    ic_plot.xaxis.axis_label = "预测期(天)"
    ic_plot.yaxis.axis_label = "IC值"
    ic_plot.legend.location = "top_left"
    ic_plot.legend.click_policy = "hide"

    # IC分析总结
    ic_analysis_summary = f"""
    <h3>IC分析总结</h3>
    - 因子对比：对比各因子的IC值，{max(factor_names, key=lambda f: max(abs(results[p]['ic_results'][f]) for p in periods))}因子在各预测期的IC绝对值相对最高，说明其预测能力最强。<br>
    - 合成因子效果：合成因子IC值为{max(results[p]['ic_results']['composite_factor'] for p in periods):.4f}，若高于任一单因子，说明因子组合有效；若低于单因子，需调整因子权重或筛选因子。<br>
    - 预测期选择：各因子在{max(periods, key=lambda p: max(abs(results[p]['ic_results'][f]) for f in factor_names))}天预测期的IC表现最佳，建议优先考虑该周期的策略应用。
    """

    # 创建因子收益比较图
    r2_values = [results[p]['r_squared'] for p in periods]
    r2_plot = create_line_plot(
        x_data=periods,
        y_data=r2_values,
        title='不同预测期的因子模型R²值',
        x_label='预测期(天)',
        y_label='R²值',
        color="purple",
        legend_label="R²值"
    )

    # 创建收益率分析图表
    return_data = {}
    for factor in factor_names:
        return_values = [results[p]['factor_returns'][factor] for p in periods]
        return_data[factor] = return_values

    return_plot = figure(
        title='不同预测期的因子收益率',
        x_range=[str(p) for p in periods],
        height=400,
        tools="pan,wheel_zoom,box_zoom,reset,save",
        sizing_mode="stretch_width"
    )

    for i, (factor, values) in enumerate(return_data.items()):
        return_plot.line(
            x=[str(p) for p in periods],
            y=values,
            line_width=2,
            color=Category10[10][i % len(Category10[10])],
            legend_label=factor
        )
        # 替换为 scatter 方法
        return_plot.scatter(
            x=[str(p) for p in periods],
            y=values,
            size=8,
            color=Category10[10][i % len(Category10[10])]
        )

    return_plot.line(x=[str(p) for p in periods], y=[
        0]*len(periods), line_dash='dashed', line_color='red')
    return_plot.xaxis.axis_label = "预测期(天)"
    return_plot.yaxis.axis_label = "因子收益率"
    return_plot.legend.location = "top_left"
    return_plot.legend.click_policy = "hide"

    # 收益率分析总结
    return_analysis_summary = f"""
    <h3>收益率分析总结</h3>
    - 因子贡献：通过回归系数（因子收益率）判断各因子对收益的贡献度。{max(factor_names, key=lambda f: max(abs(results[p]['factor_returns'][f]) for p in periods))}因子的回归系数绝对值最大，对收益影响最显著。<br>
    - R²分析：R²值反映模型整体解释力。{max(periods, key=lambda p: results[p]['r_squared'])}天预测期的R²值最高({max(r2_values):.4f})，说明该周期下因子组合对收益的解释能力最强。<br>
    - 因子方向：观察回归系数的正负，判断因子与收益的方向关系。若某因子在多个预测期系数符号一致，说明其影响方向稳定。
    """

    # 创建多因子分析结果面板
    tabs = Tabs(tabs=[
        TabPanel(child=column(
            Div(text="<h2>因子相关性分析</h2>"),
            Div(text=corr_analysis_criteria),
            heatmap, width_policy='max'
        ), title="因子相关性"),

        TabPanel(child=column(
            Div(text="<h2>因子IC分析</h2>"),
            Div(text=ic_analysis_summary),
            ic_plot, width_policy='max'
        ), title="IC分析"),

        TabPanel(child=column(
            Div(text="<h2>因子收益率分析</h2>"),
            Div(text=return_analysis_summary),
            return_plot,
            r2_plot, width_policy='max'
        ), title="收益率分析"),

        # 新增：累计收益分析
        TabPanel(child=column(
            Div(text="<h2>多因子组合累计收益分析</h2>"),
            Div(text=cum_return_summary),
            cumulative_return_plot, width_policy='max'
        ), title="累计收益")
    ], sizing_mode="stretch_both", width_policy='max')

    return tabs, results, factor_corr

# ======================
# 结果解释与总结
# ======================


def interpret_results(single_result=None, multi_result=None, periods=None):
    """解释分析结果 - 支持单因子或多因子单独分析"""
    # 创建结果总结HTML
    summary_html = f"""
    <div style="font-family: Arial, sans-serif; width: 80%; margin: 0 auto; max-width: 1200px;">
        <h1 style="color: #2c3e50; text-align: center;">因子分析结果总结</h1>
    """

    # 单因子分析总结
    if single_result is not None and periods is not None:
        summary_html += f"""
        <div style="background-color: #f9f9f9; padding: 20px; border-radius: 10px; margin-bottom: 20px;">
            <h2 style="color: #3498db;">单因子分析总结</h2>
            <p><strong>分析因子:</strong> {single_result["name"]}</p>
            <table style="width: 100%; border-collapse: collapse; margin-top: 15px;">
                <tr style="background-color: #3498db; color: white;">
                    <th style="padding: 10px; border: 1px solid #ddd;">预测期(天)</th>
                    <th style="padding: 10px; border: 1px solid #ddd;">信息系数(IC)</th>
                    <th style="padding: 10px; border: 1px solid #ddd;">多空收益</th>
                </tr>
        """

        for period in periods:
            res = single_result[period]
            summary_html += f"""
                    <tr>
                        <td style="padding: 10px; border: 1px solid #ddd; text-align: center;">{period}</td>
                        <td style="padding: 10px; border: 1px solid #ddd; text-align: center; color: {'#e74c3c' if res['ic'] < 0 else '#27ae60'}">{res['ic']:.4f}</td>
                        <td style="padding: 10px; border: 1px solid #ddd; text-align: center; color: {'#e74c3c' if res['long_short_return'] < 0 else '#27ae60'}">{res['long_short_return']:.6f}</td>
                    </tr>
            """

        # 找出最佳预测期
        best_period = max(periods, key=lambda p: abs(single_result[p]['ic']))
        summary_html += f"""
                </table>
                <p style="margin-top: 15px;"><strong>最佳预测期:</strong> {best_period}天 (IC={single_result[best_period]['ic']:.4f})</p>
            </div>
        """

    # 多因子分析总结（修正键引用错误）
    if multi_result is not None and periods is not None:
        summary_html += f"""
        <div style="background-color: #f9f9f9; padding: 20px; border-radius: 10px; margin-bottom: 20px;">
            <h2 style="color: #3498db;">多因子分析总结</h2>
            <p><strong>分析因子:</strong> {', '.join(multi_result['results'][periods[0]]['factor_returns'].index.tolist())}</p>
            <h3>因子相关性矩阵:</h3>
            {multi_result['factor_corr'].to_html(classes='IndFrame', border=0)}
        """

        # 不同预测期的表现
        summary_html += """
                <h3>不同预测期的表现:</h3>
                <table style="width: 100%; border-collapse: collapse; margin-top: 15px;">
                    <tr style="background-color: #3498db; color: white;">
                        <th style="padding: 10px; border: 1px solid #ddd;">预测期(天)</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">回归R²</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">平均IC</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">合成因子IC</th>
                    </tr>
        """

        for period in periods:
            res = multi_result['results'][period]
            # 修正：计算平均IC时排除 'composite_factor'（原代码误写为 'composite'）
            avg_ic = np.mean(
                [ic for f, ic in res['ic_results'].items() if f !=
                 'composite_factor']
            )
            # 修正：合成因子IC的键是 'composite_factor'（原代码误写为 'composite'）
            composite_ic = res['ic_results']['composite_factor']

            summary_html += f"""
                    <tr>
                        <td style="padding: 10px; border: 1px solid #ddd; text-align: center;">{period}</td>
                        <td style="padding: 10px; border: 1px solid #ddd; text-align: center; color: {'#e74c3c' if res['r_squared'] < 0 else '#27ae60'}">{res['r_squared']:.4f}</td>
                        <td style="padding: 10px; border: 1px solid #ddd; text-align: center; color: {'#e74c3c' if avg_ic < 0 else '#27ae60'}">{avg_ic:.4f}</td>
                        <td style="padding: 10px; border: 1px solid #ddd; text-align: center; color: {'#e74c3c' if composite_ic < 0 else '#27ae60'}">{composite_ic:.4f}</td>
                    </tr>
            """

        # 找出最佳预测期
        best_period = max(
            periods, key=lambda p: multi_result['results'][p]['r_squared']
        )
        best_res = multi_result['results'][best_period]
        best_factor = max(
            best_res['factor_returns'].items(), key=lambda x: abs(x[1])
        )
        worst_factor = min(
            best_res['factor_returns'].items(), key=lambda x: abs(x[1])
        )

        summary_html += f"""
                </table>
                <p><strong>最佳预测期:</strong> {best_period}天 (R²={multi_result['results'][best_period]['r_squared']:.4f})</p>
                
                <h3>投资建议:</h3>
                <ul>
                    <li>在{best_period}天预测期，最具预测性的因子: <strong>{best_factor[0]}</strong> (收益率贡献: {best_factor[1]:.6f})</li>
                    <li>在{best_period}天预测期，最不具预测性的因子: <strong>{worst_factor[0]}</strong> (收益率贡献: {worst_factor[1]:.6f})</li>
        """

        # 修正：比较合成因子IC时使用正确的键 'composite_factor'
        if best_res['ic_results']['composite_factor'] > max(
            ic for f, ic in best_res['ic_results'].items() if f != 'composite_factor'
        ):
            summary_html += "<li>合成因子表现优于单个因子，建议使用因子组合</li>"

        # 分析因子稳定性（修正键引用）
        stable_factors = []
        for factor in multi_result['results'][periods[0]]['factor_returns'].index:
            ics = [multi_result['results'][p]['ic_results'][factor]
                   for p in periods]
            if all(ic > 0 for ic in ics) or all(ic < 0 for ic in ics):
                stable_factors.append(factor)

        if stable_factors:
            summary_html += f"<li>以下因子在所有预测期保持稳定方向: <strong>{', '.join(stable_factors)}</strong></li>"

        summary_html += """
                </ul>
            </div>
        """

    summary_html += """
    </div>
    """

    # 创建总结面板，设置sizing_mode让布局适配
    summary_div = Div(text=summary_html, width_policy='max')
    return column(
        Div(text="<h1 style='text-align: center; color: #2c3e50;'>因子分析总结报告</h1>"),
        summary_div,
        width_policy='max'
    )

# ======================
# 主执行流程 - 支持单独运行单因子或多因子分析
# ======================


# 配置分析类型 - 可以选择 'single', 'multi' 或 'both'
# ANALYSIS_TYPE = 'both'  # 切换这里选择分析类型

# # 执行分析
# periods = [1, 3, 5, 10]
# single_results = None
# single_tabs = None
# multi_tabs = None
# multi_results = None
# factor_corr = None


def factors_plot(processed_factors):
    analysis_type = processed_factors.analysis_type
    periods = processed_factors.periods
    n_groups = processed_factors.n_groups
    if analysis_type in ['single', 'both']:
        # print("\n开始单因子多期分析...")
        single_results, single_tabs = single_factor_analysis(
            processed_factors, periods=periods, n_groups=n_groups)

    if analysis_type in ['multi', 'both']:
        # print("\n开始多因子多期分析...")
        multi_tabs, multi_analysis_results, factor_corr = multi_factor_analysis(
            processed_factors,
            periods=periods
        )
        multi_results = {
            'results': multi_analysis_results,
            'factor_corr': factor_corr
        }

    # 生成总结报告
    if analysis_type == 'single':
        summary_panel = interpret_results(
            single_result=single_results,
            periods=periods
        )
        final_layout = Tabs(tabs=[
            TabPanel(child=single_tabs, title="单因子分析"),
            TabPanel(child=summary_panel, title="分析总结")
        ], sizing_mode="stretch_both", width_policy='max')

    elif analysis_type == 'multi':
        summary_panel = interpret_results(
            multi_result=multi_results,
            periods=periods
        )
        final_layout = Tabs(tabs=[
            TabPanel(child=multi_tabs, title="多因子分析"),
            TabPanel(child=summary_panel, title="分析总结")
        ], sizing_mode="stretch_both", width_policy='max')

    else:  # both
        summary_panel = interpret_results(
            single_result=single_results,
            multi_result=multi_results,
            periods=periods
        )
        final_layout = Tabs(tabs=[
            TabPanel(child=single_tabs, title="单因子分析"),
            TabPanel(child=multi_tabs, title="多因子分析"),
            TabPanel(child=summary_panel, title="分析总结")
        ], sizing_mode="stretch_both", width_policy='max')

    # 设置输出为网页
    # output_file("factor_analysis_results.html")

    # 显示所有结果
    # show(final_layout)
    return final_layout
