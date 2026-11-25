/**
 * 技术指标面板组件
 *
 * 功能：
 * - RSI指标展示
 * - MACD指标展示
 * - KDJ指标展示
 * - 指标选择器
 * - 参数配置
 * - 实时计算
 */

import React, { useMemo, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { Card, Select, InputNumber, Space, Tooltip, Divider, Tag } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import type { EChartsOption } from 'echarts';

const { Option } = Select;

// 指标类型
export type IndicatorType = 'RSI' | 'MACD' | 'KDJ';

// RSI数据
export interface RSIData {
  timestamp: number;
  value: number;
}

// MACD数据
export interface MACDData {
  timestamp: number;
  dif: number;
  dea: number;
  macd: number;
}

// KDJ数据
export interface KDJData {
  timestamp: number;
  k: number;
  d: number;
  j: number;
}

// 指标数据联合类型
export type IndicatorChartData = RSIData[] | MACDData[] | KDJData[];

// 组件Props
export interface IndicatorPanelProps {
  // 必填属性
  symbol: string;
  type: IndicatorType;
  data: IndicatorChartData;
  dates: string[];

  // 可选属性
  height?: number;
  theme?: 'light' | 'dark';
  showConfig?: boolean;

  // RSI参数
  rsiPeriod?: number;

  // MACD参数
  macdFast?: number;
  macdSlow?: number;
  macdSignal?: number;

  // KDJ参数
  kdjPeriod?: number;
  kdjM1?: number;
  kdjM2?: number;

  // 回调函数
  onConfigChange?: (config: any) => void;
}

/**
 * 技术指标面板组件
 */
export const IndicatorPanel: React.FC<IndicatorPanelProps> = ({
  symbol,
  type,
  data,
  dates,
  height = 300,
  theme = 'light',
  showConfig = true,
  rsiPeriod = 14,
  macdFast = 12,
  macdSlow = 26,
  macdSignal = 9,
  kdjPeriod = 9,
  kdjM1 = 3,
  kdjM2 = 3,
  onConfigChange,
}) => {
  // 配置状态
  const [config, setConfig] = useState({
    rsiPeriod,
    macdFast,
    macdSlow,
    macdSignal,
    kdjPeriod,
    kdjM1,
    kdjM2,
  });

  // 更新配置
  const handleConfigChange = (key: string, value: number) => {
    const newConfig = { ...config, [key]: value };
    setConfig(newConfig);
    onConfigChange?.(newConfig);
  };

  // RSI图表配置
  const getRSIOption = (): EChartsOption => {
    const rsiData = data as RSIData[];
    const values = rsiData.map(d => d.value);

    return {
      backgroundColor: theme === 'dark' ? '#1f1f1f' : '#ffffff',
      title: {
        text: `RSI(${config.rsiPeriod})`,
        left: 'center',
        textStyle: {
          color: theme === 'dark' ? '#ffffff' : '#333333',
          fontSize: 14,
        },
      },
      grid: {
        left: '10%',
        right: '10%',
        top: '20%',
        bottom: '15%',
      },
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: {
          lineStyle: {
            color: theme === 'dark' ? '#555' : '#ccc',
          },
        },
        axisLabel: {
          color: theme === 'dark' ? '#999' : '#666',
        },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLine: {
          lineStyle: {
            color: theme === 'dark' ? '#555' : '#ccc',
          },
        },
        axisLabel: {
          color: theme === 'dark' ? '#999' : '#666',
        },
        splitLine: {
          lineStyle: {
            color: theme === 'dark' ? '#333' : '#eee',
          },
        },
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: theme === 'dark' ? 'rgba(50, 50, 50, 0.9)' : 'rgba(255, 255, 255, 0.9)',
        borderColor: theme === 'dark' ? '#555' : '#ccc',
        textStyle: {
          color: theme === 'dark' ? '#ffffff' : '#333333',
        },
      },
      series: [
        {
          name: 'RSI',
          type: 'line',
          data: values,
          smooth: true,
          lineStyle: {
            color: '#42a5f5',
            width: 2,
          },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(66, 165, 245, 0.3)' },
                { offset: 1, color: 'rgba(66, 165, 245, 0.1)' },
              ],
            },
          },
        },
      ],
      visualMap: {
        show: false,
        pieces: [
          { gte: 0, lte: 30, color: '#26a69a' },
          { gt: 30, lte: 70, color: '#42a5f5' },
          { gt: 70, lte: 100, color: '#ef5350' },
        ],
      },
      markLine: {
        silent: true,
        data: [
          {
            yAxis: 30,
            lineStyle: { color: '#26a69a', type: 'dashed' },
            label: { formatter: '超卖: 30' },
          },
          {
            yAxis: 70,
            lineStyle: { color: '#ef5350', type: 'dashed' },
            label: { formatter: '超买: 70' },
          },
        ],
      },
    };
  };

  // MACD图表配置
  const getMACDOption = (): EChartsOption => {
    const macdData = data as MACDData[];
    const difData = macdData.map(d => d.dif);
    const deaData = macdData.map(d => d.dea);
    const macdValues = macdData.map(d => d.macd);

    return {
      backgroundColor: theme === 'dark' ? '#1f1f1f' : '#ffffff',
      title: {
        text: `MACD(${config.macdFast},${config.macdSlow},${config.macdSignal})`,
        left: 'center',
        textStyle: {
          color: theme === 'dark' ? '#ffffff' : '#333333',
          fontSize: 14,
        },
      },
      legend: {
        data: ['DIF', 'DEA', 'MACD'],
        top: 30,
        textStyle: {
          color: theme === 'dark' ? '#ffffff' : '#333333',
        },
      },
      grid: {
        left: '10%',
        right: '10%',
        top: '25%',
        bottom: '15%',
      },
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: {
          lineStyle: {
            color: theme === 'dark' ? '#555' : '#ccc',
          },
        },
        axisLabel: {
          color: theme === 'dark' ? '#999' : '#666',
        },
      },
      yAxis: {
        type: 'value',
        axisLine: {
          lineStyle: {
            color: theme === 'dark' ? '#555' : '#ccc',
          },
        },
        axisLabel: {
          color: theme === 'dark' ? '#999' : '#666',
        },
        splitLine: {
          lineStyle: {
            color: theme === 'dark' ? '#333' : '#eee',
          },
        },
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: theme === 'dark' ? 'rgba(50, 50, 50, 0.9)' : 'rgba(255, 255, 255, 0.9)',
        borderColor: theme === 'dark' ? '#555' : '#ccc',
        textStyle: {
          color: theme === 'dark' ? '#ffffff' : '#333333',
        },
      },
      series: [
        {
          name: 'DIF',
          type: 'line',
          data: difData,
          smooth: true,
          lineStyle: {
            color: '#42a5f5',
            width: 2,
          },
          showSymbol: false,
        },
        {
          name: 'DEA',
          type: 'line',
          data: deaData,
          smooth: true,
          lineStyle: {
            color: '#ffa726',
            width: 2,
          },
          showSymbol: false,
        },
        {
          name: 'MACD',
          type: 'bar',
          data: macdValues.map((value, idx) => ({
            value,
            itemStyle: {
              color: value >= 0 ? '#ef5350' : '#26a69a',
            },
          })),
        },
      ],
    };
  };

  // KDJ图表配置
  const getKDJOption = (): EChartsOption => {
    const kdjData = data as KDJData[];
    const kData = kdjData.map(d => d.k);
    const dData = kdjData.map(d => d.d);
    const jData = kdjData.map(d => d.j);

    return {
      backgroundColor: theme === 'dark' ? '#1f1f1f' : '#ffffff',
      title: {
        text: `KDJ(${config.kdjPeriod},${config.kdjM1},${config.kdjM2})`,
        left: 'center',
        textStyle: {
          color: theme === 'dark' ? '#ffffff' : '#333333',
          fontSize: 14,
        },
      },
      legend: {
        data: ['K', 'D', 'J'],
        top: 30,
        textStyle: {
          color: theme === 'dark' ? '#ffffff' : '#333333',
        },
      },
      grid: {
        left: '10%',
        right: '10%',
        top: '25%',
        bottom: '15%',
      },
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: {
          lineStyle: {
            color: theme === 'dark' ? '#555' : '#ccc',
          },
        },
        axisLabel: {
          color: theme === 'dark' ? '#999' : '#666',
        },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLine: {
          lineStyle: {
            color: theme === 'dark' ? '#555' : '#ccc',
          },
        },
        axisLabel: {
          color: theme === 'dark' ? '#999' : '#666',
        },
        splitLine: {
          lineStyle: {
            color: theme === 'dark' ? '#333' : '#eee',
          },
        },
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: theme === 'dark' ? 'rgba(50, 50, 50, 0.9)' : 'rgba(255, 255, 255, 0.9)',
        borderColor: theme === 'dark' ? '#555' : '#ccc',
        textStyle: {
          color: theme === 'dark' ? '#ffffff' : '#333333',
        },
      },
      series: [
        {
          name: 'K',
          type: 'line',
          data: kData,
          smooth: true,
          lineStyle: {
            color: '#42a5f5',
            width: 2,
          },
          showSymbol: false,
        },
        {
          name: 'D',
          type: 'line',
          data: dData,
          smooth: true,
          lineStyle: {
            color: '#ffa726',
            width: 2,
          },
          showSymbol: false,
        },
        {
          name: 'J',
          type: 'line',
          data: jData,
          smooth: true,
          lineStyle: {
            color: '#ab47bc',
            width: 2,
          },
          showSymbol: false,
        },
      ],
      markLine: {
        silent: true,
        data: [
          {
            yAxis: 20,
            lineStyle: { color: '#26a69a', type: 'dashed' },
            label: { formatter: '超卖: 20' },
          },
          {
            yAxis: 80,
            lineStyle: { color: '#ef5350', type: 'dashed' },
            label: { formatter: '超买: 80' },
          },
        ],
      },
    };
  };

  // 获取当前图表配置
  const chartOption = useMemo(() => {
    switch (type) {
      case 'RSI':
        return getRSIOption();
      case 'MACD':
        return getMACDOption();
      case 'KDJ':
        return getKDJOption();
      default:
        return getRSIOption();
    }
  }, [type, data, dates, config, theme]);

  // 获取指标说明
  const getIndicatorDescription = () => {
    switch (type) {
      case 'RSI':
        return 'RSI相对强弱指标：衡量价格变动的速度和幅度。RSI>70为超买，RSI<30为超卖。';
      case 'MACD':
        return 'MACD指数平滑异同移动平均线：由DIF、DEA和MACD柱组成。DIF上穿DEA称金叉，下穿称死叉（仅为形态描述，不构成操作建议）。';
      case 'KDJ':
        return 'KDJ随机指标：由K、D、J三条线组成。K>D为强势，J>80超买，J<20超卖。';
      default:
        return '';
    }
  };

  // 获取当前信号
  const getCurrentSignal = () => {
    if (!data || data.length === 0) return null;

    const lastData = data[data.length - 1];

    switch (type) {
      case 'RSI': {
        const rsi = (lastData as RSIData).value;
        if (rsi > 70) return { text: '超买', color: 'red' };
        if (rsi < 30) return { text: '超卖', color: 'green' };
        return { text: '中性', color: 'blue' };
      }
      case 'MACD': {
        const macd = lastData as MACDData;
        if (macd.dif > macd.dea && macd.macd > 0) return { text: '强势', color: 'red' };
        if (macd.dif < macd.dea && macd.macd < 0) return { text: '弱势', color: 'green' };
        return { text: '震荡', color: 'blue' };
      }
      case 'KDJ': {
        const kdj = lastData as KDJData;
        if (kdj.j > 80) return { text: '超买', color: 'red' };
        if (kdj.j < 20) return { text: '超卖', color: 'green' };
        if (kdj.k > kdj.d) return { text: '多头', color: 'orange' };
        return { text: '空头', color: 'blue' };
      }
      default:
        return null;
    }
  };

  const signal = getCurrentSignal();

  return (
    <Card
      title={
        <Space>
          <span>{symbol} - {type}指标</span>
          {signal && <Tag color={signal.color}>{signal.text}</Tag>}
          <Tooltip title={getIndicatorDescription()}>
            <InfoCircleOutlined style={{ color: '#999' }} />
          </Tooltip>
        </Space>
      }
      extra={
        showConfig && (
          <Space>
            {type === 'RSI' && (
              <>
                <span>周期:</span>
                <InputNumber
                  min={5}
                  max={30}
                  value={config.rsiPeriod}
                  onChange={(value) => handleConfigChange('rsiPeriod', value || 14)}
                  size="small"
                  style={{ width: 60 }}
                />
              </>
            )}
            {type === 'MACD' && (
              <>
                <span>快:</span>
                <InputNumber
                  min={5}
                  max={20}
                  value={config.macdFast}
                  onChange={(value) => handleConfigChange('macdFast', value || 12)}
                  size="small"
                  style={{ width: 50 }}
                />
                <span>慢:</span>
                <InputNumber
                  min={15}
                  max={40}
                  value={config.macdSlow}
                  onChange={(value) => handleConfigChange('macdSlow', value || 26)}
                  size="small"
                  style={{ width: 50 }}
                />
                <span>信号:</span>
                <InputNumber
                  min={5}
                  max={15}
                  value={config.macdSignal}
                  onChange={(value) => handleConfigChange('macdSignal', value || 9)}
                  size="small"
                  style={{ width: 50 }}
                />
              </>
            )}
            {type === 'KDJ' && (
              <>
                <span>N:</span>
                <InputNumber
                  min={5}
                  max={20}
                  value={config.kdjPeriod}
                  onChange={(value) => handleConfigChange('kdjPeriod', value || 9)}
                  size="small"
                  style={{ width: 50 }}
                />
                <span>M1:</span>
                <InputNumber
                  min={2}
                  max={5}
                  value={config.kdjM1}
                  onChange={(value) => handleConfigChange('kdjM1', value || 3)}
                  size="small"
                  style={{ width: 50 }}
                />
                <span>M2:</span>
                <InputNumber
                  min={2}
                  max={5}
                  value={config.kdjM2}
                  onChange={(value) => handleConfigChange('kdjM2', value || 3)}
                  size="small"
                  style={{ width: 50 }}
                />
              </>
            )}
          </Space>
        )
      }
      variant="borderless"
      style={{ marginBottom: 16 }}
    >
      <ReactECharts
        option={chartOption}
        style={{ width: '100%', height }}
        notMerge={true}
        lazyUpdate={true}
        theme={theme}
      />
    </Card>
  );
};

export default IndicatorPanel;
