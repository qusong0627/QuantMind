/**
 * 策略模板库（T-FE-11）：模板浏览（三行说明）+ 一键「立即回测」（到回测页自动选中，≤2 步）。
 *
 * 数据源：/api/v1/strategies/templates（86 套存量模板，含 minibt×11；个人中心已由后端
 * 自动镜像同步，无需先"复制"即可在回测页以"个人中心/策略模板"双入口使用）。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useDispatch } from 'react-redux';
import { setCurrentTab } from '../../../store/slices/aiStrategySlice';
import { BookOpen, ChevronDown, Play, RefreshCw, Search } from 'lucide-react';
import { strategyTemplateService } from '../../../features/strategy-wizard/services/strategyTemplateService';
import {
  difficultyLabel,
  filterTemplates,
  marketLabel,
  normalizeTemplate,
  templateLines,
  templateStats,
  type GalleryTemplate,
} from './templateGalleryModel';

const DIFFICULTY_OPTIONS = [
  { key: '', label: '全部' },
  { key: 'beginner', label: '入门' },
  { key: 'intermediate', label: '进阶' },
  { key: 'advanced', label: '高级' },
];

const MARKET_OPTIONS = [
  { key: '', label: '全市场' },
  { key: 'a_share', label: 'A股' },
  { key: 'hong_kong', label: '港股' },
  { key: 'us_stock', label: '美股' },
];

export const StrategyTemplateGallery: React.FC = () => {
  const navigate = useNavigate();
  const dispatch = useDispatch();
  const [templates, setTemplates] = useState<GalleryTemplate[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [keyword, setKeyword] = useState('');
  const [difficulty, setDifficulty] = useState('');
  const [market, setMarket] = useState('');
  const [expanded, setExpanded] = useState<string>('');

  const load = async (refresh = false) => {
    setLoading(true);
    setError('');
    try {
      const raw = refresh
        ? await strategyTemplateService.refresh()
        : await strategyTemplateService.getTemplates();
      setTemplates(
        (raw || []).map((t) => normalizeTemplate(t as unknown as Record<string, unknown>))
      );
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '模板加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const filtered = useMemo(
    () => filterTemplates(templates, { keyword, difficulty, market }),
    [templates, keyword, difficulty, market]
  );
  const stats = useMemo(() => templateStats(templates), [templates]);

  const runBacktest = async (t: GalleryTemplate) => {
    // 模板经后端 _perform_sync 自动镜像进"个人中心"（去重键 parameters.strategy_type=模板 id）
    // → 走回测中心已支持的"个人中心策略"预选机制（选中即为该模板策略）
    try {
      const { strategyManagementService } = await import('../../../services/strategyManagementService');
      const items = await strategyManagementService.loadStrategies(undefined, undefined);
      const match = (items || []).find(
        (s: { parameters?: { strategy_type?: string } }) =>
          String(s?.parameters?.strategy_type || '') === t.id
      );
      if (match) {
        localStorage.setItem('selected_backtest_strategy_id', String((match as { id: number | string }).id));
        dispatch(setCurrentTab('backtest' as never));
        navigate('/');
        return;
      }
    } catch {
      // 查询失败落兜底（不阻断跳转）
    }
    // 兜底：模板 id 预选键
    localStorage.setItem('selected_backtest_template_id', t.id);
    dispatch(setCurrentTab('backtest' as never));
    navigate('/');
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-slate-800">策略模板库</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            共 {stats.total} 套（minibt {stats.minibt} · 入门 {stats.beginner}）——选中模板 →「立即回测」，
            两步出回测
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load(true)}
          disabled={loading}
          className="px-3 py-1.5 text-xs rounded-xl border border-gray-200 bg-white hover:bg-gray-100 text-gray-700 disabled:opacity-50"
        >
          <span className="inline-flex items-center gap-1">
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </span>
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-2" />
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索模板名 / 说明 / 目录"
            className="pl-8 pr-3 py-1.5 text-xs rounded-xl border border-gray-200 w-56 focus:outline-none focus:border-blue-400"
          />
        </div>
        {DIFFICULTY_OPTIONS.map((opt) => (
          <button
            key={opt.key}
            type="button"
            onClick={() => setDifficulty(opt.key)}
            className={`px-3 py-1.5 text-xs rounded-xl border ${
              difficulty === opt.key
                ? 'border-blue-500 bg-blue-50 text-blue-700'
                : 'border-gray-200 bg-white text-slate-600 hover:bg-gray-50'
            }`}
          >
            {opt.label}
          </button>
        ))}
        <span className="text-slate-200">|</span>
        {MARKET_OPTIONS.map((opt) => (
          <button
            key={opt.key}
            type="button"
            onClick={() => setMarket(opt.key)}
            className={`px-3 py-1.5 text-xs rounded-xl border ${
              market === opt.key
                ? 'border-blue-500 bg-blue-50 text-blue-700'
                : 'border-gray-200 bg-white text-slate-600 hover:bg-gray-50'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {error && (
        <div className="bg-amber-50 border border-amber-200 rounded-2xl p-3 text-xs text-amber-800">
          {error}
        </div>
      )}

      {loading && templates.length === 0 ? (
        <div className="flex items-center justify-center h-48">
          <RefreshCw className="w-6 h-6 text-blue-500 animate-spin" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="bg-gray-50 rounded-2xl border border-gray-200 p-10 text-center text-sm text-gray-500">
          无匹配模板——调整筛选或关键词
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3">
          {filtered.map((t) => (
            <div key={t.id} className="rounded-2xl border border-gray-200 bg-white p-4 flex flex-col">
              <div className="flex items-start justify-between gap-2">
                <h3 className="text-sm font-semibold text-slate-800">{t.name}</h3>
                {t.isMinibt && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-violet-50 text-violet-700 border border-violet-200 shrink-0">
                    minibt
                  </span>
                )}
              </div>
              <div className="flex flex-wrap gap-1 mt-1.5 text-[10px] text-slate-500">
                <span className="px-1.5 py-0.5 rounded bg-slate-50 border border-slate-100">
                  {difficultyLabel(t.difficulty)}
                </span>
                <span className="px-1.5 py-0.5 rounded bg-slate-50 border border-slate-100">
                  {t.category || 'basic'}
                </span>
                <span className="px-1.5 py-0.5 rounded bg-slate-50 border border-slate-100">
                  {(t.markets.length ? t.markets : ['a_share']).map(marketLabel).join('/')}
                </span>
                {t.dir && (
                  <span className="px-1.5 py-0.5 rounded bg-slate-50 border border-slate-100 truncate max-w-[160px]">
                    {t.dir}
                  </span>
                )}
              </div>
              <div className="mt-2 space-y-0.5 flex-1">
                {templateLines(t).map((line, i) => (
                  <p key={i} className="text-[11px] text-slate-500 leading-4 line-clamp-1">
                    {line}
                  </p>
                ))}
              </div>
              <div className="mt-3 flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => void runBacktest(t)}
                  className="inline-flex items-center gap-1 rounded-xl bg-blue-600 px-3 py-1.5 text-[11px] font-bold text-white hover:bg-blue-500"
                >
                  <Play className="w-3 h-3" />
                  立即回测
                </button>
                <button
                  type="button"
                  onClick={() => setExpanded(expanded === t.id ? '' : t.id)}
                  className="inline-flex items-center gap-1 rounded-xl border border-gray-200 px-3 py-1.5 text-[11px] text-slate-600 hover:bg-gray-50"
                >
                  <BookOpen className="w-3 h-3" />
                  查看代码
                  <ChevronDown className={`w-3 h-3 transition-transform ${expanded === t.id ? 'rotate-180' : ''}`} />
                </button>
              </div>
              {expanded === t.id && (
                <pre className="mt-2 max-h-[260px] overflow-auto rounded-xl bg-slate-50 border border-slate-100 p-2 text-[10px] leading-4 text-slate-600 whitespace-pre-wrap">
                  {t.code || '（无代码）'}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
