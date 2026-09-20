/**
 * 评估中心（FE-E / T-FE-14/15/16，2026-09-20 三段式改版）
 *
 * 数据源：`/api/v1/eval/*`（eval_scores 表唯一读取面 + 长序列侧车）。
 *
 * 三段式（图形为主，但每一块都要能单独说清一件事）：
 * 1. **总览带**：等级分布堆叠条 + 对象数/平均分/红线数/**证据覆盖率**（分数会被缺维的
 *    薄卡拉高，所以要并排给出「几个对象有 ≥3 维实证」）；
 * 2. **榜单**：分数条 + 等级 + 维度覆盖条（有色=已算 / 斜纹=缺省）+ 该类最关键的一个
 *    实测量（模型→实测 RankIC、因子→ICIR、策略→年化/回撤、账户→利用率、选股→T+H 超额）；
 * 3. **详情**：实测量 → 分维得分 → 换手成本 → 一问一图（长序列）→ 历史曲线 → 页脚
 *    （来源/口径/时间戳）。
 *
 * 展示名：主标题优先 display_name（后端解析），原 object_id 作副标题。
 * 本文件只做编排，口径全在 `evalCenterModel` / `evalInsightModel` / `evalSeriesModel`。
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  Award,
  CalendarCheck,
  Cpu,
  HeartPulse,
  Inbox,
  RefreshCw,
  Sigma,
  Target,
  Wallet,
} from 'lucide-react';
import {
  getEvalObjectTypes,
  getScoreHistory,
  getStrategyHealth,
  listScores,
} from '../services/evalCenterService';
import type { EvalObjectType, EvalScoreRow, StrategyHealthArchive } from '../types/evalCenter';
import { useUiMode } from '../../shared/useUiMode';
import { SelfHealthUpload } from './SelfHealthUpload';
import { EvalOverviewBand } from './EvalOverviewBand';
import { EvalRankList } from './EvalRankList';
import { EvalDetail } from './EvalDetail';
import { HealthArchivePanel } from './HealthArchivePanel';

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败';
}

const TYPE_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  factor: Sigma,
  model: Cpu,
  strategy: Target,
  account: Wallet,
  daily_selection: CalendarCheck,
  strategy_health: HeartPulse,
};

export const EvalCenterPanel: React.FC = () => {
  const { isSimple } = useUiMode();
  const [objectTypes, setObjectTypes] = useState<EvalObjectType[]>([]);
  const [activeType, setActiveType] = useState<string>('factor');
  const [rows, setRows] = useState<EvalScoreRow[]>([]);
  const [selectedId, setSelectedId] = useState<string>('');
  const [history, setHistory] = useState<EvalScoreRow[]>([]);
  const [archive, setArchive] = useState<StrategyHealthArchive | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    void (async () => {
      try {
        const resp = await getEvalObjectTypes();
        const types = resp?.data || [];
        setObjectTypes(types);
        if (types.length > 0 && !types.some((t) => t.object_type === activeType)) {
          setActiveType(types[0].object_type);
        }
      } catch (err: unknown) {
        setError(errorText(err));
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void loadRows(activeType);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeType]);

  useEffect(() => {
    // 守卫：选中项必须属于当前类型（切页签瞬间旧 selectedId 未清，直查体检接口会 400）
    const row = rows.find((r) => r.object_id === selectedId);
    if (!selectedId || !row || row.object_type !== activeType) {
      setHistory([]);
      setArchive(null);
      return;
    }
    void loadDetail(activeType, selectedId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, activeType, rows]);

  const loadRows = async (type: string) => {
    setLoading(true);
    setError('');
    setSelectedId('');
    setArchive(null);
    setHistory([]);
    try {
      const resp = await listScores({ objectType: type, latestOnly: true, limit: 200 });
      const data = resp?.data || [];
      setRows(data);
      if (data.length > 0) setSelectedId(data[0].object_id);
    } catch (err: unknown) {
      setError(errorText(err));
      setRows([]);
    } finally {
      setLoading(false);
    }
  };

  const loadDetail = async (type: string, objectId: string) => {
    try {
      if (type === 'strategy_health') {
        if (!/^\d+$/.test(objectId)) {
          // 策略体检仅数字策略 id（后端契约）；非数字直接空态，不发请求
          setArchive(null);
          setHistory([]);
          return;
        }
        const resp = await getStrategyHealth(objectId);
        setArchive(resp?.data || null);
        setHistory([]);
      } else {
        const resp = await getScoreHistory(type, objectId);
        setHistory(resp?.data || []);
        setArchive(null);
      }
    } catch (err: unknown) {
      setError(errorText(err));
    }
  };

  const selectedRow = useMemo(
    () => rows.find((r) => r.object_id === selectedId) || null,
    [rows, selectedId]
  );

  const isHealth = activeType === 'strategy_health';

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-slate-800">评估中心</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            六类评分卡与体检档案（数据源 eval_scores；评分由每日 EOD 任务与回测体检自动生成）
          </p>
        </div>
        <button
          type="button"
          onClick={() => void loadRows(activeType)}
          disabled={loading}
          className="inline-flex items-center gap-1 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          刷新
        </button>
      </div>

      {/* 类型页签（图标 + 胶囊） */}
      <div className="flex w-fit max-w-full flex-wrap items-center gap-1 rounded-full border border-slate-200 bg-slate-100 p-0.5">
        {objectTypes.map((otype) => {
          const Icon = TYPE_ICONS[otype.object_type] || Award;
          const active = activeType === otype.object_type;
          return (
            <button
              key={otype.object_type}
              type="button"
              onClick={() => setActiveType(otype.object_type)}
              className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[11px] font-bold transition-colors ${
                active ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              <Icon className="w-3 h-3" />
              {otype.label}
            </button>
          );
        })}
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded-2xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex h-48 flex-col items-center justify-center gap-3">
          <RefreshCw className="w-6 h-6 animate-spin text-blue-500" />
          <span className="text-xs text-slate-400">正在读取评分卡…</span>
        </div>
      ) : rows.length === 0 ? (
        isHealth ? (
          <div className="space-y-4">
            <div className="rounded-2xl border border-slate-200/80 bg-slate-50 p-6 text-center text-sm text-slate-500">
              暂无体检留档（回测完成后自动生成；月度复检每月留档）——下方可直接自助体检：
            </div>
            <SelfHealthUpload />
          </div>
        ) : (
          <div className="flex flex-col items-center gap-2 rounded-2xl border border-slate-200/80 bg-slate-50 p-10 text-sm text-slate-500">
            <Inbox className="w-8 h-8 text-slate-300" />
            暂无该类型评分记录（评分任务在 EOD 跑批/回测体检后自动写入）
          </div>
        )
      ) : (
        <>
          {!isHealth && <EvalOverviewBand rows={rows} />}

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
            <div className="lg:col-span-2 space-y-2 max-h-[720px] overflow-y-auto pr-1">
              <EvalRankList rows={rows} selectedId={selectedId} onSelect={setSelectedId} />
            </div>

            <div className="lg:col-span-3">
              {isHealth ? (
                <div className="space-y-4">
                  {archive ? (
                    <HealthArchivePanel archive={archive} />
                  ) : (
                    <div className="rounded-2xl border border-slate-200/80 bg-slate-50 p-8 text-center text-sm text-slate-500">
                      选择左侧策略查看体检档案
                    </div>
                  )}
                  <SelfHealthUpload />
                </div>
              ) : (
                <EvalDetail
                  row={selectedRow}
                  objectType={activeType}
                  history={history}
                  isSimple={isSimple}
                />
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
};
