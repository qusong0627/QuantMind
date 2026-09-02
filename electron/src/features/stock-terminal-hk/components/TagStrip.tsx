/** P3 智能标签：命中标签流 + 组合预设 + 同类股票展开 */

import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Tag as TagIcon, Sparkles, ChevronRight, X, Filter } from 'lucide-react';
import { Modal, Table, Spin, message } from 'antd';
import { stockTerminalService } from '../services/stockTerminalService';
import { StockListItem } from '../types';

interface MatchedTag { id: string; name: string; category: string; desc: string; value: number | null; }
interface MatchedPreset { id: string; name: string; matched: number; total: number; }

interface Props {
  symbol: string | null;
  onSelectStock?: (item: StockListItem) => void;
  /** 竖排模式：右侧栏用 */
  vertical?: boolean;
  /** 点击标签的筛选按钮 -> 父级筛左侧列表 */
  onSelectTag?: (tag: { id: string; name: string }) => void;
  /** 当前筛选中的标签 id（高亮） */
  activeTagId?: string | null;
}

const CATEGORY_COLORS: Record<string, string> = {
  '宽基指数': 'bg-violet-50 text-violet-600 border-violet-100',
  '规模与流动性': 'bg-sky-50 text-sky-600 border-sky-100',
  '行业板块': 'bg-teal-50 text-teal-600 border-teal-100',
  '价值与成长': 'bg-emerald-50 text-emerald-600 border-emerald-100',
  '技术形态': 'bg-orange-50 text-orange-600 border-orange-100',
  '资金趋势': 'bg-rose-50 text-rose-600 border-rose-100',
  '主题热点': 'bg-fuchsia-50 text-fuchsia-600 border-fuchsia-100',
  '筹码分析': 'bg-indigo-50 text-indigo-600 border-indigo-100',
  '市场情绪': 'bg-amber-50 text-amber-600 border-amber-100',
  '融资融券': 'bg-cyan-50 text-cyan-600 border-cyan-100',
  '因子选股': 'bg-blue-50 text-blue-600 border-blue-100',
};
const FALLBACK = 'bg-slate-50 text-slate-600 border-slate-100';

export function TagStrip({ symbol, onSelectStock, vertical = false, onSelectTag, activeTagId }: Props) {
  const [tags, setTags] = useState<MatchedTag[]>([]);
  const [presets, setPresets] = useState<MatchedPreset[]>([]);
  const [openTag, setOpenTag] = useState<{ id: string; name: string } | null>(null);
  const [similar, setSimilar] = useState<any[]>([]);
  const [scoreMin, setScoreMin] = useState<number | null>(null);
  const [scoreMax, setScoreMax] = useState<number | null>(null);
  const [similarLoading, setSimilarLoading] = useState(false);

  // 分数显示：按当前模型全市场 min/max 动态归一化到 0-100（旧逻辑固定 ×100，
  // 不同模型分数量级差异大时失真——新模型 0.001 分 ×100 后无法区分优劣）
  const renderScore = (v: number | null) => {
    if (v == null) return '--';
    const lo = scoreMin, hi = scoreMax;
    if (lo == null || hi == null || hi <= lo) {
      return (Number(v) * 100).toFixed(2); // 接口未带极值时降级旧显示
    }
    const norm = ((Number(v) - lo) / (hi - lo)) * 100;
    return `${norm >= 0 ? '+' : ''}${norm.toFixed(1)}`;
  };

  useEffect(() => {
    if (!symbol) { setTags([]); setPresets([]); return; }
    let c = false;
    stockTerminalService.getTags(symbol).then(r => {
      if (!c) { setTags(r.tags); setPresets(r.presets); }
    }).catch(() => message.error('标签加载失败'));
    return () => { c = true; };
  }, [symbol]);

  const openSimilar = async (id: string, name: string) => {
    setOpenTag({ id, name });
    setSimilarLoading(true);
    setSimilar([]);
    setScoreMin(null);
    setScoreMax(null);
    try {
      const data = await stockTerminalService.getTagStocks(id, 30);
      setSimilar(data.items);
      setScoreMin(data.score_min);
      setScoreMax(data.score_max);
    } catch {
      message.error('同类股票加载失败');
    } finally {
      setSimilarLoading(false);
    }
  };

  return vertical ? (
    <div className="flex flex-col gap-1.5 p-3 h-full">
      <div className="flex items-center gap-1.5 pb-2 border-b border-slate-100 shrink-0">
        <Sparkles className="w-3 h-3 text-violet-400" />
        <span className="text-[11px] font-black text-slate-700">智能标签</span>
      </div>
      <div className="grid grid-cols-4 gap-1.5 overflow-y-auto">
        {tags.map(t => (
          <div key={t.id} className="flex items-center gap-1 min-w-0">
            <button
              onClick={() => openSimilar(t.id, t.name)}
              title={t.desc + (t.value != null ? `（值=${t.value?.toFixed(2)}）` : '')}
              className={`flex-1 flex items-center justify-between gap-1 px-2 py-1.5 rounded-lg border text-[10px] font-bold text-left transition-colors hover:scale-[1.02] min-w-0 ${CATEGORY_COLORS[t.category] ?? FALLBACK}`}
            >
              <span className="flex items-center gap-1 min-w-0">
                <TagIcon className="w-2.5 h-2.5 shrink-0" />
                <span className="truncate">{t.name}</span>
              </span>
              <ChevronRight className="w-2.5 h-2.5 opacity-50 shrink-0" />
            </button>
            {onSelectTag && (
              <button
                onClick={() => onSelectTag({ id: t.id, name: t.name })}
                title="按此标签筛选左侧列表"
                className={`w-5 h-5 rounded-md flex items-center justify-center border transition-colors shrink-0 ${
                  activeTagId === t.id
                    ? 'bg-violet-500 text-white border-violet-500'
                    : 'bg-white text-violet-400 border-slate-200 hover:bg-violet-50'
                }`}
              >
                <Filter className="w-3 h-3" />
              </button>
            )}
          </div>
        ))}
        {!tags.length && <span className="col-span-4 text-[10px] text-slate-400">选择股票后自动匹配智能标签</span>}
      </div>
      {presets.length > 0 && (
        <div className="pt-2 border-t border-slate-100 shrink-0">
          <div className="text-[10px] text-slate-400 font-bold mb-1">命中组合</div>
          <div className="grid grid-cols-4 gap-1.5">
            {presets.map(p => (
              <span key={p.id} className="truncate px-1.5 py-1 rounded-md bg-blue-50 text-blue-600 border border-blue-100 text-[10px] font-bold text-center">
                {p.name}
              </span>
            ))}
          </div>
        </div>
      )}

      <Modal
        open={!!openTag}
        onCancel={() => setOpenTag(null)}
        footer={null}
        title={<span className="text-sm font-black text-slate-800">「{openTag?.name}」同类股票</span>}
        width={560}
      >
        <Spin spinning={similarLoading}>
          <Table
            size="small"
            rowKey="symbol"
            pagination={false}
            dataSource={similar}
            scroll={{ y: 360 }}
            columns={[
              { title: '名称', dataIndex: 'name', width: 110, render: (v, r) => (
                <button
                  className="text-blue-600 font-bold hover:underline"
                  onClick={() => { onSelectStock?.({ symbol: r.symbol, name: r.name } as StockListItem); setOpenTag(null); }}
                >{v}</button>
              )},
              { title: '代码', dataIndex: 'symbol', width: 100, render: v => <span className="font-mono text-slate-500">{v}</span> },
              { title: '行业', dataIndex: 'industry', width: 90, render: v => v || '--' },
              { title: '价格', dataIndex: 'close', width: 70, align: 'right', render: v => v?.toFixed?.(2) ?? '--' },
              { title: '分数', dataIndex: 'fusion', width: 70, align: 'right', render: (v) => v == null ? '--' : <span className="text-blue-600 font-bold font-mono">{renderScore(v)}</span> },
              { title: '标签值', dataIndex: 'metric', align: 'right', render: (v) => v == null ? '--' : Number(v).toFixed(2) },
            ]}
          />
        </Spin>
      </Modal>
    </div>
  ) : (
    <div className="px-4 py-2 flex items-center gap-2 border-b border-slate-100 min-h-[36px]">
      <Sparkles className="w-3 h-3 text-violet-400 shrink-0" />
      <div className="flex flex-wrap gap-1 items-center flex-1 min-w-0">
        {tags.map(t => (
          <span key={t.id} className="flex items-center gap-0.5">
            <button
              onClick={() => openSimilar(t.id, t.name)}
              title={t.desc + (t.value != null ? `（值=${t.value?.toFixed(2)}）` : '')}
              className={`flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[10px] font-bold transition-transform hover:scale-105 ${CATEGORY_COLORS[t.category] ?? FALLBACK}`}
            >
              <TagIcon className="w-2.5 h-2.5" />
              {t.name}
              <ChevronRight className="w-2.5 h-2.5 opacity-50" />
            </button>
            {onSelectTag && (
              <button
                onClick={() => onSelectTag({ id: t.id, name: t.name })}
                title="按此标签筛选左侧列表"
                className={`w-4 h-4 rounded flex items-center justify-center border transition-colors ${
                  activeTagId === t.id
                    ? 'bg-violet-500 text-white border-violet-500'
                    : 'bg-white text-violet-400 border-slate-200 hover:bg-violet-50'
                }`}
              >
                <Filter className="w-2.5 h-2.5" />
              </button>
            )}
          </span>
        ))}
        {!tags.length && <span className="text-[10px] text-slate-400">选择股票后自动匹配智能标签</span>}
      </div>
      {presets.length > 0 && (
        <div className="flex flex-wrap items-center gap-1 shrink-0 border-l border-slate-100 pl-2 max-w-[420px]">
          <span className="text-[9px] text-slate-400 font-bold shrink-0">命中组合</span>
          {presets.map(p => (
            <span key={p.id} className="shrink-0 whitespace-nowrap px-1.5 py-0.5 rounded-md bg-blue-50 text-blue-600 border border-blue-100 text-[10px] font-bold">
              {p.name}
            </span>
          ))}
        </div>
      )}
      <Modal
        open={!!openTag}
        onCancel={() => setOpenTag(null)}
        footer={null}
        title={<span className="text-sm font-black text-slate-800">「{openTag?.name}」同类股票</span>}
        width={560}
      >
        <Spin spinning={similarLoading}>
          <Table
            size="small"
            rowKey="symbol"
            pagination={false}
            dataSource={similar}
            scroll={{ y: 360 }}
            columns={[
              { title: '名称', dataIndex: 'name', width: 110, render: (v, r) => (
                <button
                  className="text-blue-600 font-bold hover:underline"
                  onClick={() => { onSelectStock?.({ symbol: r.symbol, name: r.name } as StockListItem); setOpenTag(null); }}
                >{v}</button>
              )},
              { title: '代码', dataIndex: 'symbol', width: 100, render: v => <span className="font-mono text-slate-500">{v}</span> },
              { title: '行业', dataIndex: 'industry', width: 90, render: v => v || '--' },
              { title: '价格', dataIndex: 'close', width: 70, align: 'right', render: v => v?.toFixed?.(2) ?? '--' },
              { title: '分数', dataIndex: 'fusion', width: 70, align: 'right', render: (v) => v == null ? '--' : <span className="text-blue-600 font-bold font-mono">{renderScore(v)}</span> },
              { title: '标签值', dataIndex: 'metric', align: 'right', render: (v) => v == null ? '--' : Number(v).toFixed(2) },
            ]}
          />
        </Spin>
      </Modal>
    </div>
  );
}
