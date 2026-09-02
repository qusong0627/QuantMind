/** P5 图表策略回测：表达式 DSL -> 跑回测 -> 买卖点+净值叠加 */

import { useEffect, useRef, useState } from 'react';
import { FlaskConical, Play, Loader2, Sparkles } from 'lucide-react';
import { Modal, Input, message, Tooltip } from 'antd';
import { stockTerminalService } from '../services/stockTerminalService';

interface BacktestResult {
  trades: { date: string; side: string; price: number; pnl: number | null }[];
  total_return: number;
  buy_hold_return: number;
  win_rate: number | null;
  trade_count: number;
  max_drawdown: number;
  points: { date: string; close: number; equity: number }[];
}

const PRESETS = [
  { name: '双均线金叉死叉', buy: 'CROSSUP(MA(CLOSE,5),MA(CLOSE,20))', sell: 'CROSSDOWN(MA(CLOSE,5),MA(CLOSE,20))' },
  { name: '放量突破', buy: 'AND(CLOSE>MA(CLOSE,20), VOLUME>MA(VOLUME,5)*1.5)', sell: 'CROSSDOWN(CLOSE,MA(CLOSE,20))' },
  { name: 'RSI超卖反弹', buy: 'AND(REF(RSI(CLOSE,14),1)<30, CROSS(RSI(CLOSE,14),30))', sell: 'RSI(CLOSE,14)>70' },
  { name: '布林带策略', buy: 'CROSSDOWN(CLOSE, LLV(LOW,20))', sell: 'CROSSUP(CLOSE, HHV(HIGH,20))' },
];

export interface ChartBacktestData { trades: any[]; points: { date: string; equity: number }[]; }

interface Props {
  symbol: string;
  onResult?: (data: ChartBacktestData | null) => void;
}

export function ChartBacktestPanel({ symbol, onResult }: Props) {
  const [open, setOpen] = useState(false);
  const [buyExpr, setBuyExpr] = useState(PRESETS[0].buy);
  const [sellExpr, setSellExpr] = useState(PRESETS[0].sell);
  const [hint, setHint] = useState('');
  const [loading, setLoading] = useState(false);
  const [aiLoading, setAiLoading] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const timer = useRef<any>(null);

  // 关闭时清除叠加
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); onResult?.(null); }, [onResult]);

  const aiGenerate = async () => {
    if (!symbol) { message.warning('先选择股票'); return; }
    setAiLoading(true);
    try {
      const r = await stockTerminalService.getAiBacktest(symbol, hint);
      if (r.buy) setBuyExpr(r.buy);
      if (r.sell) setSellExpr(r.sell);
      if (r.llm_error) message.info(`AI 生成回退默认（${r.llm_error}），配置 LLM Key 后可用`);
    } catch {
      message.error('AI 生成失败');
    } finally {
      setAiLoading(false);
    }
  };

  const run = async () => {
    if (!symbol) { message.warning('先选择股票'); return; }
    setLoading(true);
    try {
      const resp = await stockTerminalService.getChartBacktest(symbol, buyExpr, sellExpr);
      setResult(resp);
      onResult?.({
        trades: resp.trades,
        points: resp.points,
      });
    } catch (e: any) {
      message.error(`回测失败: ${e?.message ?? e}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-1 text-[11px] font-bold text-slate-500 hover:text-blue-600 transition-colors px-2 py-1 rounded-lg hover:bg-blue-50"
        title="表达式策略回测"
      >
        <FlaskConical className="w-3 h-3" /> 策略回测
      </button>
      <Modal
        open={open}
        onCancel={() => setOpen(false)}
        onOk={run}
        okText={loading ? '回测中…' : '运行回测'}
        okButtonProps={{ disabled: loading, icon: loading ? <Loader2 className="animate-spin" /> : <Play /> }}
        cancelText="关闭"
        title={<span className="text-sm font-black text-slate-800">图表策略回测（表达式 DSL）</span>}
        width={640}
        destroyOnHidden
      >
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <Input
              value={hint}
              onChange={e => setHint(e.target.value)}
              placeholder="AI 意图描述，如：底部放量突破"
              className="text-xs"
              onPressEnter={aiGenerate}
            />
            <Tooltip title="AI 基于命中标签+技术形态生成买卖表达式">
              <button
                onClick={aiGenerate}
                disabled={aiLoading}
                className="flex items-center gap-1 px-2 py-1 rounded-lg bg-violet-50 text-violet-600 border border-violet-100 text-[11px] font-bold hover:bg-violet-100 shrink-0 disabled:opacity-50"
              >
                <Sparkles className="w-3 h-3" /> {aiLoading ? '生成中…' : 'AI 生成'}
              </button>
            </Tooltip>
          </div>
          <div className="flex flex-wrap gap-1">
            {PRESETS.map(p => (
              <button
                key={p.name}
                onClick={() => { setBuyExpr(p.buy); setSellExpr(p.sell); }}
                className="px-2 py-0.5 rounded-md bg-slate-50 border border-slate-200 text-[10px] font-bold text-slate-600 hover:bg-blue-50 hover:border-blue-200 hover:text-blue-600"
              >
                {p.name}
              </button>
            ))}
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-[11px] font-bold text-slate-500">买入条件</span>
            <Input
              value={buyExpr}
              onChange={e => setBuyExpr(e.target.value)}
              className="font-mono text-xs"
              placeholder="CROSSUP(MA(CLOSE,5),MA(CLOSE,20))"
            />
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-[11px] font-bold text-slate-500">卖出条件（留空=持有到期）</span>
            <Input
              value={sellExpr}
              onChange={e => setSellExpr(e.target.value)}
              className="font-mono text-xs"
              placeholder="CROSSDOWN(MA(CLOSE,5),MA(CLOSE,20))"
            />
          </div>
          <div className="text-[10px] text-slate-400 bg-slate-50 rounded-lg p-2 leading-relaxed">
            函数: MA/EMA/SMA/REF/HHV/LLV/CROSSUP/CROSSDOWN/CROSS/AND/OR/NOT · 变量: CLOSE/OPEN/HIGH/LOW/VOLUME
            <br />撮合: 信号次日开盘成交（防未来函数）· 费用: 万2.5+印花税0.1%
          </div>
          {result && (
            <div className="grid grid-cols-4 gap-2">
              <div className="bg-white/70 rounded-xl border border-slate-100 p-2">
                <div className="text-[10px] text-slate-400">策略收益</div>
                <div className={`text-base font-black ${result.total_return >= 0 ? 'text-rose-500' : 'text-emerald-500'}`}>
                  {result.total_return >= 0 ? '+' : ''}{result.total_return}%
                </div>
              </div>
              <div className="bg-white/70 rounded-xl border border-slate-100 p-2">
                <div className="text-[10px] text-slate-400">买入持有</div>
                <div className="text-base font-black text-slate-700">{result.buy_hold_return >= 0 ? '+' : ''}{result.buy_hold_return}%</div>
              </div>
              <div className="bg-white/70 rounded-xl border border-slate-100 p-2">
                <div className="text-[10px] text-slate-400">胜率 / 交易</div>
                <div className="text-base font-black text-slate-700">{result.win_rate ?? '--'}% / {result.trade_count}</div>
              </div>
              <div className="bg-white/70 rounded-xl border border-slate-100 p-2">
                <div className="text-[10px] text-slate-400">最大回撤</div>
                <div className="text-base font-black text-emerald-500">{result.max_drawdown}%</div>
              </div>
            </div>
          )}
        </div>
      </Modal>
    </>
  );
}
