/**
 * ReplayPage.tsx — 时光回放页
 *
 * 功能：
 * - 创建回放会话（选择日期区间、初始资金、自动/手动模式）
 * - 自动模式：单步推演 / 自动推进
 * - 手动模式：生成提案 → 勾选/改数量 → 确认执行 / 跳过今日
 * - 自动推进完成后跳转报告页
 * - 展示当前账户状态和成交记录
 */

import { useAppSelector } from '../../../store';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import React, { useState, useEffect, useCallback, useReducer } from 'react';
import {
    Clock, Play, Trash2, Plus, Loader2, AlertTriangle,
    SkipForward, CheckSquare, Square, Shield, ChevronDown, ChevronUp,
    FastForward, Pause, RotateCcw, BarChart3,
    Cpu, BookOpen, Settings2, Zap,
    ShieldCheck, Info,
} from 'lucide-react';
import type {
    ReplaySession, StepResult, CreateSessionParams,
    ProposalItem, ProposalResponse, ConfirmedOrder,
    StrategyTemplate, StrategyTemplateParam,
} from '../../../services/replayService';
import {
    listSessions, createSession,
    stepSession, deleteSession, proposeSession,
    listStrategyTemplates,
} from '../../../services/replayService';
import { modelTrainingService, type SystemModelRecord, type UserModelRecord } from '../../../services/modelTrainingService';
import { modelDisplayName, getMeta, getMetrics, extractModelTypeShort } from '../../modelRegistryUtils';
import { useAutoAdvance, type AutoAdvanceSpeed, type DailyRecord } from '../../../hooks/useAutoAdvance';
import ReplayReportPage from './ReplayReportPage';
import {
  StockPoolSelectField,
  type StockPoolSelection,
} from '../../../components/backtest/StockPoolSelectField';

// ---------------------------------------------------------------------------
// Status badge
// ---------------------------------------------------------------------------

const STATUS_MAP: Record<ReplaySession['status'], { label: string; color: string }> = {
    creating:          { label: '创建中',     color: 'bg-gray-100 text-gray-600' },
    generating:        { label: '生成信号',   color: 'bg-blue-50 text-blue-600' },
    ready:             { label: '就绪',       color: 'bg-green-50 text-green-700' },
    stepping:          { label: '执行中',     color: 'bg-yellow-50 text-yellow-700' },
    awaiting_confirm:  { label: '待确认',     color: 'bg-amber-50 text-amber-700' },
    finished:          { label: '已完成',     color: 'bg-gray-100 text-gray-500' },
    failed:            { label: '失败',       color: 'bg-red-50 text-red-600' },
    discarded:         { label: '已丢弃',     color: 'bg-gray-100 text-gray-400' },
};

function StatusBadge({ status }: { status: ReplaySession['status'] }) {
    const { label, color } = STATUS_MAP[status] || STATUS_MAP.creating;
    return <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${color}`}>{label}</span>;
}

// ---------------------------------------------------------------------------
// Create form — single-page sectioned layout
// ---------------------------------------------------------------------------

/** 格式化 IC/ICIR 等指标 */
function fmtMetric(val: number | undefined, digits = 4): string {
    if (val === undefined || val === null || isNaN(val)) return '—';
    return val.toFixed(digits);
}

function ModelMetricsBadge({ metrics, label }: { metrics: Record<string, number | undefined>; label: string }) {
    const ic = metrics.mean_ic;
    const icir = metrics.icir;
    const sharpe = metrics.sharpe;
    if (ic === undefined && icir === undefined && sharpe === undefined) return null;
    return (
        <div className="flex items-center gap-2 text-[10px] text-gray-400">
            <span className="font-medium text-gray-500">{label}</span>
            {ic !== undefined && <span>IC {fmtMetric(ic)}</span>}
            {icir !== undefined && <span>ICIR {fmtMetric(icir)}</span>}
            {sharpe !== undefined && <span>Sharpe {fmtMetric(sharpe, 2)}</span>}
        </div>
    );
}

function CreateSessionForm({ onCreate }: { onCreate: (s: ReplaySession) => void }) {
    const currentMarket = useAppSelector(selectCurrentMarket);

    // Step 1: Model selection
    const [systemModels, setSystemModels] = useState<SystemModelRecord[]>([]);
    const [userModels, setUserModels] = useState<UserModelRecord[]>([]);
    const [selectedModelId, setSelectedModelId] = useState<string | null>(null);
    const [modelsLoading, setModelsLoading] = useState(true);

    // Step 2: Strategy template
    const [templates, setTemplates] = useState<StrategyTemplate[]>([]);
    const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null);
    const [templatesLoading, setTemplatesLoading] = useState(true);

    // Step 3: Params（默认区间：近半年，结束日为今天）
    const fmtDate = (d: Date) =>
        `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const [startDate, setStartDate] = useState(() => {
        const d = new Date();
        d.setMonth(d.getMonth() - 6);
        return fmtDate(d);
    });
    const [endDate, setEndDate] = useState(() => fmtDate(new Date()));
    const [initialCash, setInitialCash] = useState('1000000');
    const [stopLossPct, setStopLossPct] = useState('');
    const [paramOverrides, setParamOverrides] = useState<Record<string, unknown>>({});
    const [stockPool, setStockPool] = useState<StockPoolSelection | null>(null);

    // Step 4: Mode
    const [autoTrade, setAutoTrade] = useState(true);

    // Submit
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Load models
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const [sys, usr] = await Promise.all([
                    modelTrainingService.listSystemModels(currentMarket),
                    modelTrainingService.listUserModels(true, currentMarket), // include archived
                ]);
                if (cancelled) return;
                setSystemModels(sys);
                setUserModels(usr.items);
                // Auto-select default user model
                const defaultModel = usr.items.find(m => m.is_default && m.status === 'active');
                if (defaultModel) setSelectedModelId(defaultModel.model_id);
                else if (usr.items.length > 0) setSelectedModelId(usr.items[0].model_id);
                else if (sys.length > 0) setSelectedModelId(sys[0].model_id);
            } catch {
                // ignore — user can still proceed without model
            } finally {
                if (!cancelled) setModelsLoading(false);
            }
        })();
        return () => { cancelled = true; };
    }, [currentMarket]);

    // Load templates
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const tpls = await listStrategyTemplates();
                if (cancelled) return;
                setTemplates(tpls);
                // 默认选中「默认 Top-K 选股策略」，找不到时退回第一个模板
                if (tpls.length > 0) {
                    const preferred = tpls.find(t => t.id === 'standard_topk') ?? tpls[0];
                    setSelectedTemplateId(preferred.id);
                }
            } catch {
                // ignore
            } finally {
                if (!cancelled) setTemplatesLoading(false);
            }
        })();
        return () => { cancelled = true; };
    }, []);

    // When template changes, reset param overrides
    useEffect(() => {
        setParamOverrides({});
    }, [selectedTemplateId]);

    // Derived: selected template
    const selectedTemplate = templates.find(t => t.id === selectedTemplateId) ?? null;

    // Derived: selected model
    const selectedSystemModel = systemModels.find(m => m.model_id === selectedModelId) ?? null;
    const selectedUserModel = userModels.find(m => m.model_id === selectedModelId) ?? null;

    // Build final strategy_params from template replay_params + overrides
    const buildStrategyParams = (): Record<string, unknown> => {
        const base = selectedTemplate ? { ...selectedTemplate.replay_params } : {};
        const merged = { ...base, ...paramOverrides };
        if (stockPool?.ref) {
            merged.pool_id = stockPool.ref;
        }
        return merged;
    };

    // Build final stop_loss_pct
    const buildStopLossPct = (): number | null => {
        // Explicit input takes priority
        if (stopLossPct.trim() !== '') return parseFloat(stopLossPct) / 100;
        // Then template replay_params
        if (selectedTemplate?.replay_params.stop_loss_pct != null) {
            return Number(selectedTemplate.replay_params.stop_loss_pct);
        }
        return null;
    };

    const handleSubmit = async () => {
        setLoading(true);
        setError(null);
        try {
            const params: CreateSessionParams = {
                name: `${startDate} ~ ${endDate}`,
                model_id: selectedModelId ?? undefined,
                strategy_params: buildStrategyParams(),
                start_date: startDate,
                end_date: endDate,
                initial_cash: parseFloat(initialCash),
                auto_trade: autoTrade,
                stop_loss_pct: buildStopLossPct(),
            };
            const session = await createSession(params);
            onCreate(session);
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '创建失败';
            setError(msg);
        } finally {
            setLoading(false);
        }
    };

    // --- Section heading helper (SessionCard 头部条风格) ---
    const SectionTitle = ({ icon: Icon, title, desc }: { icon: React.ElementType; title: string; desc?: string }) => (
        <div className="flex items-center gap-2.5 px-4 py-3 bg-slate-50/60 border-b border-slate-100">
            <Icon size={15} className="text-slate-400" />
            <span className="text-sm font-bold text-slate-800">{title}</span>
            {desc && <span className="text-[11px] text-slate-400">{desc}</span>}
        </div>
    );

    // --- Model section: default option + selectable card grid ---
    const renderModelSection = () => {
        const allModelOptions: Array<{
            id: string;
            label: string;
            sublabel: string;
            group: string;
            metrics: Record<string, number | undefined> | null;
            isDefault: boolean;
            status: string;
        }> = [];

        for (const m of userModels) {
            if (m.status === 'archived') continue;
            const meta = getMeta(m);
            const metrics = getMetrics(m);
            const name = modelDisplayName(m);
            const algo = extractModelTypeShort(m);
            const testMetrics = (metrics?.test ?? metrics?.performance_metrics?.test ?? null) as Record<string, number | undefined> | null;
            allModelOptions.push({
                id: m.model_id,
                label: name,
                sublabel: algo ? `${algo} · ${m.status}` : m.status,
                group: '我的模型',
                metrics: testMetrics,
                isDefault: m.is_default,
                status: m.status,
            });
        }

        for (const m of systemModels) {
            allModelOptions.push({
                id: m.model_id,
                label: m.display_name,
                sublabel: `${m.algorithm} · v${m.version}`,
                group: '系统模型',
                metrics: m.performance_metrics?.test ?? null,
                isDefault: false,
                status: 'system',
            });
        }

        const userOpts = allModelOptions.filter(o => o.group === '我的模型');
        const sysOpts = allModelOptions.filter(o => o.group === '系统模型');
        const defaultOpt = allModelOptions.find(o => o.isDefault) ?? allModelOptions[0] ?? null;

        const optionCard = (opt: typeof allModelOptions[number]) => {
            const selected = selectedModelId === opt.id;
            return (
                <button
                    key={opt.id}
                    type="button"
                    onClick={() => setSelectedModelId(opt.id)}
                    className={`text-left px-3 py-2.5 rounded-xl border transition-all ${
                        selected
                            ? 'border-blue-400 bg-blue-50/70 shadow-2xs'
                            : 'border-slate-200 bg-white hover:border-blue-200 hover:bg-blue-50/30'
                    }`}
                >
                    <div className="flex items-center gap-1.5 flex-wrap">
                        <span className={`text-xs font-bold truncate ${selected ? 'text-blue-700' : 'text-slate-700'}`}>{opt.label}</span>
                        {opt.isDefault && (
                            <span className="px-2 py-0.5 rounded-lg bg-emerald-50 text-emerald-600 text-[10px] font-bold border border-emerald-200 shrink-0">默认</span>
                        )}
                        {opt.status !== 'active' && opt.status !== 'system' && (
                            <span className="px-2 py-0.5 rounded-lg bg-slate-100 text-slate-500 text-[10px] font-bold border border-slate-200 shrink-0">{opt.status}</span>
                        )}
                    </div>
                    <div className="mt-0.5 text-[10px] text-slate-400">{opt.sublabel}</div>
                    {opt.metrics && <ModelMetricsBadge metrics={opt.metrics} label="测试集" />}
                </button>
            );
        };

        return (
            <section className="rounded-2xl border border-slate-200/80 bg-white shadow-xs overflow-hidden">
                <SectionTitle icon={Cpu} title="信号模型" desc="不选则使用系统默认模型" />
                <div className="px-4 py-3">
                {modelsLoading ? (
                    <div className="flex items-center gap-2 py-4 text-slate-400">
                        <Loader2 size={16} className="animate-spin" />
                        <span className="text-xs">加载模型列表…</span>
                    </div>
                ) : (
                    <div className="space-y-3">
                        <button
                            type="button"
                            onClick={() => setSelectedModelId(null)}
                            className={`w-full text-left px-3 py-2.5 rounded-xl border transition-all ${
                                selectedModelId === null
                                    ? 'border-blue-400 bg-blue-50/70 shadow-2xs'
                                    : 'border-dashed border-slate-300 bg-slate-50/50 hover:border-blue-200 hover:bg-blue-50/30'
                            }`}
                        >
                            <div className="flex items-center gap-1.5">
                                <span className={`text-xs font-bold ${selectedModelId === null ? 'text-blue-700' : 'text-slate-600'}`}>使用默认模型</span>
                                {defaultOpt && (
                                    <span className="text-[10px] text-slate-400 truncate">{defaultOpt.label}</span>
                                )}
                            </div>
                        </button>

                        {(userOpts.length > 0 || sysOpts.length > 0) && (
                            <div className="max-h-[212px] overflow-y-auto custom-scrollbar pr-1 space-y-3">
                                {userOpts.length > 0 && (
                                    <div>
                                        <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1.5">我的模型</div>
                                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">{userOpts.map(optionCard)}</div>
                                    </div>
                                )}

                                {sysOpts.length > 0 && (
                                    <div>
                                        <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1.5">系统模型</div>
                                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">{sysOpts.map(optionCard)}</div>
                                    </div>
                                )}
                            </div>
                        )}

                        {allModelOptions.length === 0 && (
                            <p className="text-xs text-slate-400 py-2">暂无可用模型，将使用系统默认模型。</p>
                        )}
                    </div>
                )}
                </div>
            </section>
        );
    };

    // --- Strategy template section: default option + selectable card list ---
    const renderStrategySection = () => {
        const difficultyLabel = (d: string) =>
            d === 'beginner' ? '入门' : d === 'intermediate' ? '进阶' : '高级';
        const difficultyTone = (d: string) =>
            d === 'beginner' ? 'bg-emerald-50 text-emerald-600 border-emerald-200'
                : d === 'intermediate' ? 'bg-amber-50 text-amber-600 border-amber-200'
                    : 'bg-red-50 text-red-600 border-red-200';

        return (
            <section className="rounded-2xl border border-slate-200/80 bg-white shadow-xs overflow-hidden">
                <SectionTitle icon={BookOpen} title="策略模板" desc="自动填充调仓参数和止损规则" />
                <div className="px-4 py-3">
                {templatesLoading ? (
                    <div className="flex items-center gap-2 py-4 text-slate-400">
                        <Loader2 size={16} className="animate-spin" />
                        <span className="text-xs">加载策略模板…</span>
                    </div>
                ) : (
                    <div className="space-y-2">
                        <button
                            type="button"
                            onClick={() => setSelectedTemplateId(null)}
                            className={`w-full text-left px-3 py-2.5 rounded-xl border transition-all ${
                                selectedTemplateId === null
                                    ? 'border-blue-400 bg-blue-50/70 shadow-2xs'
                                    : 'border-dashed border-slate-300 bg-slate-50/50 hover:border-blue-200 hover:bg-blue-50/30'
                            }`}
                        >
                            <span className={`text-xs font-bold ${selectedTemplateId === null ? 'text-blue-700' : 'text-slate-600'}`}>使用默认参数</span>
                        </button>
                        <div className="space-y-2 max-h-[292px] overflow-y-auto custom-scrollbar pr-1">
                        {templates.length === 0 ? (
                            <p className="text-xs text-slate-400 py-2">暂无策略模板，将使用默认参数。</p>
                        ) : (
                            templates.map(t => {
                                const selected = selectedTemplateId === t.id;
                                return (
                                    <button
                                        key={t.id}
                                        type="button"
                                        onClick={() => setSelectedTemplateId(t.id)}
                                        className={`w-full text-left px-3 py-2.5 rounded-xl border transition-all ${
                                            selected
                                                ? 'border-blue-400 bg-blue-50/70 shadow-2xs'
                                                : 'border-slate-200 bg-white hover:border-blue-200 hover:bg-blue-50/30'
                                        }`}
                                    >
                                        <div className="flex items-center gap-1.5 flex-wrap">
                                            <span className={`text-xs font-bold ${selected ? 'text-blue-700' : 'text-slate-700'}`}>{t.name}</span>
                                            <span className="px-2 py-0.5 rounded-lg bg-slate-100 text-slate-500 text-[10px] font-bold border border-slate-200">{t.category}</span>
                                            <span className={`px-2 py-0.5 rounded-lg text-[10px] font-bold border ${difficultyTone(t.difficulty)}`}>
                                                {difficultyLabel(t.difficulty)}
                                            </span>
                                        </div>
                                        <p className="mt-1 text-[11px] text-slate-500 leading-relaxed line-clamp-2">{t.description}</p>
                                        <div className="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[10px] font-mono text-slate-400">
                                            {t.replay_params.topk != null && <span>TopK={String(t.replay_params.topk)}</span>}
                                            {t.replay_params.weight_mode != null && <span>权重={String(t.replay_params.weight_mode)}</span>}
                                            {t.replay_params.rebalance_days != null && <span>每{String(t.replay_params.rebalance_days)}日调仓</span>}
                                            {t.replay_params.n_drop_ratio != null && <span>调仓比例{(Number(t.replay_params.n_drop_ratio) * 100).toFixed(0)}%</span>}
                                            {t.replay_params.max_position_pct != null && <span>最大持仓={String(t.replay_params.max_position_pct)}</span>}
                                            {t.replay_params.stop_loss_pct != null && <span>止损={(Number(t.replay_params.stop_loss_pct) * 100).toFixed(1)}%</span>}
                                        </div>
                                    </button>
                                );
                            })
                        )}
                        </div>
                    </div>
                )}
                </div>
            </section>
        );
    };

    // --- Params section: date range / cash / stop loss / template params ---
    const renderParamsSection = () => {
        const templateParams = selectedTemplate?.params ?? [];
        const inputClass = "w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-500 bg-white transition-all shadow-2xs font-medium text-slate-800";
        return (
            <section className="h-full rounded-2xl border border-slate-200/80 bg-white shadow-xs overflow-hidden">
                <SectionTitle icon={Settings2} title="推演参数" />
                <div className="px-4 py-3">
                <div className="mb-3">
                    <label className="block text-xs font-semibold text-slate-600 mb-1.5">股票池（可选）</label>
                    <StockPoolSelectField
                        value={stockPool}
                        onChange={setStockPool}
                        title="时光回放股票池"
                        compact
                    />
                    <p className="mt-1 text-[10px] text-slate-400">限定信号只在池成分内；留空则不过滤</p>
                </div>
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                    <div>
                        <label className="block text-xs font-semibold text-slate-600 mb-1.5">起始日</label>
                        <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)} className={inputClass} />
                    </div>
                    <div>
                        <label className="block text-xs font-semibold text-slate-600 mb-1.5">结束日</label>
                        <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)} className={inputClass} />
                    </div>
                    <div>
                        <label className="block text-xs font-semibold text-slate-600 mb-1.5">初始资金</label>
                        <input type="number" value={initialCash} onChange={e => setInitialCash(e.target.value)} className={inputClass} />
                    </div>
                    <div>
                        <label className="block text-xs font-semibold text-slate-600 mb-1.5">
                            止损比例 (%)
                            {selectedTemplate?.replay_params.stop_loss_pct != null && (
                                <span className="ml-1 font-normal text-slate-400">
                                    模板默认 {(Number(selectedTemplate.replay_params.stop_loss_pct) * 100).toFixed(1)}%
                                </span>
                            )}
                        </label>
                        <input
                            type="number"
                            value={stopLossPct}
                            onChange={e => setStopLossPct(e.target.value)}
                            placeholder={selectedTemplate?.replay_params.stop_loss_pct != null
                                ? `${(Number(selectedTemplate.replay_params.stop_loss_pct) * 100).toFixed(1)}`
                                : '如 8 表示 8%'}
                            min={0}
                            max={100}
                            step={0.5}
                            className={inputClass}
                        />
                    </div>
                </div>

                {/* Template params overrides */}
                {templateParams.length > 0 && (
                    <div className="space-y-2.5 pt-3 mt-3 border-t border-slate-100">
                        <h5 className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">策略参数</h5>
                        {templateParams.map(p => (
                            <TemplateParamInput
                                key={p.name}
                                param={p}
                                value={paramOverrides[p.name] ?? p.default}
                                onChange={v => {
                                    const next: Record<string, unknown> = { ...paramOverrides, [p.name]: v };
                                    setParamOverrides(next);
                                }}
                            />
                        ))}
                    </div>
                )}
                </div>
            </section>
        );
    };

    // --- Mode + summary + create section ---
    const renderModeSection = () => {
        const finalParams = buildStrategyParams();
        const finalStopLoss = buildStopLossPct();
        const valid = !!startDate && !!endDate && !isNaN(parseFloat(initialCash)) && parseFloat(initialCash) > 0;
        return (
            <section className="h-full flex flex-col rounded-2xl border border-slate-200/80 bg-white shadow-xs overflow-hidden">
                <SectionTitle icon={Zap} title="执行模式与确认" />
                <div className="px-4 py-3 flex-1 flex flex-col gap-3.5">

                {/* Mode selection */}
                <div className="grid grid-cols-2 gap-3">
                    <button
                        onClick={() => setAutoTrade(true)}
                        className={`px-3.5 py-3 rounded-xl border text-left transition-all ${
                            autoTrade
                                ? 'border-blue-400 bg-blue-50/70 shadow-2xs'
                                : 'border-slate-200 bg-white hover:border-slate-300'
                        }`}
                    >
                        <div className="flex items-center gap-2 mb-1">
                            <Zap size={14} className={autoTrade ? 'text-blue-600' : 'text-slate-400'} />
                            <span className="text-xs font-bold text-slate-800">自动执行</span>
                        </div>
                        <p className="text-[11px] text-slate-500">按策略信号自动买卖，支持自动推进</p>
                    </button>
                    <button
                        onClick={() => setAutoTrade(false)}
                        className={`px-3.5 py-3 rounded-xl border text-left transition-all ${
                            !autoTrade
                                ? 'border-purple-400 bg-purple-50/70 shadow-2xs'
                                : 'border-slate-200 bg-white hover:border-slate-300'
                        }`}
                    >
                        <div className="flex items-center gap-2 mb-1">
                            <ShieldCheck size={14} className={!autoTrade ? 'text-purple-600' : 'text-slate-400'} />
                            <span className="text-xs font-bold text-slate-800">手动确认</span>
                        </div>
                        <p className="text-[11px] text-slate-500">逐日生成提案，勾选/改量后确认执行</p>
                    </button>
                </div>

                {/* Config summary */}
                <div className="rounded-xl bg-slate-50/80 border border-slate-200/80 p-3.5 space-y-2">
                    <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-xs">
                        <span className="text-slate-400 font-medium">模型</span>
                        <span className="text-slate-700 font-semibold truncate">
                            {selectedSystemModel?.display_name ?? (selectedUserModel ? modelDisplayName(selectedUserModel) : null) ?? '系统默认'}
                        </span>
                        <span className="text-slate-400 font-medium">策略模板</span>
                        <span className="text-slate-700 font-semibold truncate">{selectedTemplate?.name ?? '默认参数'}</span>
                        <span className="text-slate-400 font-medium">日期区间</span>
                        <span className="text-slate-700 font-semibold font-mono">{startDate} ~ {endDate}</span>
                        <span className="text-slate-400 font-medium">初始资金</span>
                        <span className="text-slate-700 font-semibold font-mono">{parseFloat(initialCash || '0').toLocaleString('zh-CN')}</span>
                        <span className="text-slate-400 font-medium">止损</span>
                        <span className="text-slate-700 font-semibold">{finalStopLoss != null ? `${(finalStopLoss * 100).toFixed(1)}%` : '无'}</span>
                        <span className="text-slate-400 font-medium">股票池</span>
                        <span className="text-slate-700 font-semibold truncate">{stockPool?.name ?? '全市场'}</span>
                    </div>

                    {Object.keys(finalParams).length > 0 && (
                        <div className="pt-2 border-t border-slate-200">
                            <div className="text-[10px] font-semibold text-slate-400 mb-1.5">策略参数</div>
                            <div className="flex flex-wrap gap-1.5">
                                {Object.entries(finalParams).map(([k, v]) => (
                                    <span key={k} className="px-2 py-0.5 rounded-lg bg-white border border-slate-200 text-[11px] font-mono text-slate-600">
                                        {k}={String(v)}
                                    </span>
                                ))}
                            </div>
                        </div>
                    )}
                </div>

                {error && <p className="text-xs text-red-500">{error}</p>}

                <button
                    onClick={handleSubmit}
                    disabled={loading || !valid}
                    className="mt-auto w-full inline-flex items-center justify-center gap-2 px-5 py-2.5 rounded-xl bg-emerald-500 hover:bg-emerald-600 text-white text-xs font-bold shadow-xs disabled:opacity-40 disabled:cursor-not-allowed transition-all"
                >
                    {loading ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                    创建回放任务
                </button>
                </div>
            </section>
        );
    };

    return (
        <div className="space-y-4">
            <div className="grid grid-cols-1 lg:grid-cols-[1.15fr_0.85fr] gap-4 items-start">
                {renderModelSection()}
                {renderStrategySection()}
            </div>
            {/* 第二排去掉 items-start，两卡等高对齐 */}
            <div className="grid grid-cols-1 lg:grid-cols-[1.15fr_0.85fr] gap-4">
                {renderParamsSection()}
                {renderModeSection()}
            </div>
        </div>
    );
}

/** Template param input — renders number/text with min/max/default */
function TemplateParamInput({
    param,
    value,
    onChange,
}: {
    param: StrategyTemplateParam;
    value: unknown;
    onChange: (v: unknown) => void;
}) {
    const strVal = String(value ?? param.default ?? '');
    const isNum = param.min !== null || param.max !== null || typeof param.default === 'number';

    return (
        <div className="flex items-center gap-3">
            <div className="flex-1">
                <div className="flex items-center gap-1.5">
                    <label className="text-xs text-slate-600 font-medium">{param.name}</label>
                    {param.description && (
                        <span className="text-[10px] text-slate-400" title={param.description}>
                            <Info size={11} className="inline" />
                        </span>
                    )}
                </div>
                {isNum ? (
                    <input
                        type="number"
                        value={strVal}
                        onChange={e => {
                            const v = parseFloat(e.target.value);
                            onChange(isNaN(v) ? e.target.value : v);
                        }}
                        min={param.min ?? undefined}
                        max={param.max ?? undefined}
                        step="any"
                        className="w-full mt-1 px-3.5 py-2 rounded-xl border border-slate-200 text-sm focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-500 bg-white transition-all shadow-2xs font-medium text-slate-800"
                    />
                ) : (
                    <input
                        type="text"
                        value={strVal}
                        onChange={e => onChange(e.target.value)}
                        className="w-full mt-1 px-3.5 py-2 rounded-xl border border-slate-200 text-sm focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-500 bg-white transition-all shadow-2xs font-medium text-slate-800"
                    />
                )}
            </div>
            {(param.min !== null || param.max !== null) && (
                <span className="text-xs text-slate-400 font-mono mt-4">
                    {param.min != null && param.max != null
                        ? `[${param.min}, ${param.max}]`
                        : param.min != null
                            ? `≥ ${param.min}`
                            : `≤ ${param.max}`}
                </span>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Proposal table (manual mode)
// ---------------------------------------------------------------------------

interface ProposalRowState {
    checked: boolean;
    quantity: number;
    rejectReason: string | null;
}

type RowAction =
    | { type: 'RESET'; proposals: ProposalItem[] }
    | { type: 'TOGGLE'; idx: number; cancellable: boolean }
    | { type: 'SET_QTY'; idx: number; raw: string; proposal: ProposalItem; lotSize: number }
    | { type: 'TOGGLE_ALL'; proposals: ProposalItem[] }
    | { type: 'SET_REJECT'; idx: number; reason: string | null };

function initRows(proposals: ProposalItem[]): ProposalRowState[] {
    return proposals.map(p => ({
        checked: true,
        quantity: p.quantity,
        rejectReason: null,
    }));
}

function rowReducer(state: ProposalRowState[], action: RowAction): ProposalRowState[] {
    switch (action.type) {
        case 'RESET':
            return initRows(action.proposals);
        case 'TOGGLE': {
            const next = [...state];
            if (!action.cancellable) return next;
            next[action.idx] = { ...next[action.idx], checked: !next[action.idx].checked };
            return next;
        }
        case 'SET_QTY': {
            const next = [...state];
            const val = parseInt(action.raw, 10);
            if (isNaN(val) || val < 0) {
                next[action.idx] = { ...next[action.idx], quantity: 0, rejectReason: '数量无效' };
            } else if (val > action.proposal.quantity) {
                next[action.idx] = { ...next[action.idx], quantity: action.proposal.quantity, rejectReason: `不能超过原计划数量 ${action.proposal.quantity}` };
            } else if (action.proposal.side === 'BUY' && val % action.lotSize !== 0) {
                next[action.idx] = { ...next[action.idx], quantity: val, rejectReason: `买入须为整手（${action.lotSize} 的倍数）` };
            } else {
                next[action.idx] = { ...next[action.idx], quantity: val, rejectReason: null };
            }
            return next;
        }
        case 'TOGGLE_ALL': {
            const allChecked = state.every(r => r.checked);
            return state.map(r => ({ ...r, checked: !allChecked }));
        }
        case 'SET_REJECT': {
            const next = [...state];
            next[action.idx] = { ...next[action.idx], rejectReason: action.reason };
            return next;
        }
        default:
            return state;
    }
}

function ProposalTable({
    proposals,
    lotSize,
    onConfirm,
    onSkip,
    loading,
}: {
    proposals: ProposalItem[];
    lotSize: number;
    onConfirm: (orders: ConfirmedOrder[]) => void;
    onSkip: () => void;
    loading: boolean;
}) {
    const [rows, dispatch] = useReducer(rowReducer, proposals, initRows);

    useEffect(() => {
        dispatch({ type: 'RESET', proposals });
    }, [proposals]);

    const handleConfirm = () => {
        const confirmed: ConfirmedOrder[] = [];
        for (let i = 0; i < proposals.length; i++) {
            const p = proposals[i];
            const r = rows[i];
            const isStopLoss = p.origin === 'stop_loss';
            if ((r.checked || isStopLoss) && r.quantity > 0) {
                confirmed.push({
                    symbol: p.symbol,
                    side: p.side,
                    quantity: r.quantity,
                });
            }
        }
        onConfirm(confirmed);
    };

    const checkedCount = rows.filter((r, i) => r.checked || proposals[i].origin === 'stop_loss').length;
    const totalEstAmount = proposals.reduce((acc, p, i) => {
        const r = rows[i];
        if (r.checked || p.origin === 'stop_loss') {
            return acc + r.quantity * p.est_price;
        }
        return acc;
    }, 0);

    return (
        <div className="space-y-3">
            {/* Table */}
            <div className="overflow-x-auto rounded-xl border border-slate-200">
                <table className="w-full text-xs">
                    <thead>
                        <tr className="bg-slate-50 border-b border-slate-200 text-slate-500 font-semibold">
                            <th className="py-2.5 px-3 text-left w-8">
                                <button
                                    onClick={() => dispatch({ type: 'TOGGLE_ALL', proposals })}
                                    className="text-slate-400 hover:text-slate-600"
                                >
                                    {rows.every(r => r.checked) ? <CheckSquare size={14} className="text-blue-600" /> : <Square size={14} />}
                                </button>
                            </th>
                            <th className="py-2.5 px-2 text-left">标的</th>
                            <th className="py-2.5 px-2 text-center">方向</th>
                            {/* 「建议股数」→「原计划股数」：这一列是**回放当天引擎提出的**
                                数量（可改、有整手校验），是重演出来的历史事实，不是给今天的建议。 */}
                            <th className="py-2.5 px-2 text-right">原计划股数</th>
                            <th className="py-2.5 px-2 text-right">预估价</th>
                            <th className="py-2.5 px-2 text-right">预计金额</th>
                            <th className="py-2.5 px-2 text-right">预估盈亏</th>
                            <th className="py-2.5 px-2 text-left">来源</th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 bg-white">
                        {proposals.map((p, i) => {
                            const r = rows[i];
                            const isStopLoss = p.origin === 'stop_loss';
                            const isBuy = p.side === 'BUY';
                            return (
                                <tr key={p.symbol} className={r.checked || isStopLoss ? 'bg-white' : 'bg-slate-50/50 opacity-60'}>
                                    <td className="py-2 px-3">
                                        <button
                                            onClick={() => dispatch({ type: 'TOGGLE', idx: i, cancellable: p.cancellable !== false })}
                                            disabled={!p.cancellable}
                                            className="text-slate-400 hover:text-slate-600 disabled:opacity-40 disabled:cursor-not-allowed"
                                        >
                                            {r.checked || isStopLoss ? <CheckSquare size={14} className="text-blue-600" /> : <Square size={14} />}
                                        </button>
                                    </td>
                                    <td className="py-2 px-2 font-mono font-medium text-slate-800">{p.symbol}</td>
                                    <td className="py-2 px-2 text-center">
                                        <span className={`inline-flex items-center px-1.5 py-0.5 rounded-md text-[10px] font-bold border ${isBuy ? 'bg-red-50 text-red-600 border-red-200' : 'bg-emerald-50 text-emerald-600 border-emerald-200'}`}>
                                            {isBuy ? '买入' : '卖出'}
                                        </span>
                                    </td>
                                    <td className="py-2 px-2 text-right">
                                        {isStopLoss ? (
                                            <span className="font-mono font-semibold">{p.quantity}</span>
                                        ) : (
                                            <input
                                                type="number"
                                                value={r.quantity}
                                                onChange={e => dispatch({ type: 'SET_QTY', idx: i, raw: e.target.value, proposal: p, lotSize })}
                                                step={isBuy ? lotSize : 1}
                                                min={0}
                                                max={p.quantity}
                                                className="w-20 px-2 py-1 text-right font-mono rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-500 font-semibold"
                                            />
                                        )}
                                    </td>
                                    <td className="py-2 px-2 text-right font-mono text-slate-700">{p.est_price.toFixed(2)}</td>
                                    <td className="py-2 px-2 text-right font-mono font-semibold text-slate-900">
                                        {((r.checked || isStopLoss) ? r.quantity * p.est_price : 0).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}
                                    </td>
                                    <td className="py-2 px-2 text-right">
                                        {isBuy ? (
                                            <span className="text-slate-400">—</span>
                                        ) : p.avg_cost != null && p.est_pnl != null ? (
                                            <span className={`font-mono font-semibold ${p.est_pnl >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                                                {p.est_pnl >= 0 ? '+' : ''}{p.est_pnl.toFixed(0)}
                                            </span>
                                        ) : (
                                            <span className="text-slate-400">—</span>
                                        )}
                                    </td>
                                    <td className="py-2 px-2">
                                        {isStopLoss ? (
                                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg bg-amber-50 text-amber-700 text-[10px] font-bold border border-amber-200">
                                                <Shield size={10} />
                                                风控·强制
                                            </span>
                                        ) : p.origin === 'signal' ? (
                                            <span className="text-slate-400">信号</span>
                                        ) : (
                                            <span className="text-slate-400">{p.origin}</span>
                                        )}
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>

            {/* Rejection reasons */}
            {rows.some(r => r.rejectReason) && (
                <div className="space-y-1">
                    {rows.map((r, i) => r.rejectReason && (
                        <div key={i} className="flex items-center gap-2 px-3.5 py-2 rounded-xl bg-red-50 border border-red-100 text-xs text-red-700">
                            <AlertTriangle size={13} />
                            <span className="font-mono font-semibold">{proposals[i].symbol}</span>
                            <span>{r.rejectReason}</span>
                        </div>
                    ))}
                </div>
            )}

            {/* Actions */}
            <div className="flex items-center justify-between pt-2 border-t border-slate-100">
                <span className="text-xs text-slate-500 font-medium">
                    已选 {checkedCount} / {proposals.length} 笔 · 预计动用 <b className="text-slate-800">{totalEstAmount.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}</b>
                </span>
                <div className="flex items-center gap-2">
                    <button
                        onClick={onSkip}
                        disabled={loading}
                        className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl border border-slate-200 text-slate-600 text-xs font-semibold hover:bg-slate-50 disabled:opacity-40 transition-all"
                    >
                        <SkipForward size={14} />
                        跳过今日
                    </button>
                    <button
                        onClick={handleConfirm}
                        disabled={loading || checkedCount === 0}
                        className="inline-flex items-center gap-1.5 px-5 py-2 rounded-xl bg-gradient-to-r from-blue-600 to-indigo-600 text-white text-xs font-semibold hover:from-blue-700 hover:to-indigo-700 disabled:opacity-40 shadow-xs transition-all"
                    >
                        {loading ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                        确认执行 {checkedCount} 笔
                    </button>
                </div>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Session card
// ---------------------------------------------------------------------------

function SessionCard({
    session: initialSession,
    onStep,
    onDelete,
    onRefresh,
    onViewReport,
}: {
    session: ReplaySession;
    onStep: (id: string, params?: { confirmed?: ConfirmedOrder[]; skip?: boolean }) => Promise<StepResult | null>;
    onDelete: (id: string) => void;
    onRefresh: () => void;
    onViewReport: (sessionId: string) => void;
}) {
    const [session, setSession] = useState(initialSession);
    const [stepping, setStepping] = useState(false);
    const [proposing, setProposing] = useState(false);
    const [proposal, setProposal] = useState<ProposalResponse | null>(null);
    const [lastResult, setLastResult] = useState<StepResult | null>(null);
    const [stepError, setStepError] = useState<string | null>(null);
    const [showTrades, setShowTrades] = useState(false);
    // 自动推演逐日快照（每步即时更新，完成后保留最后一日即终值）
    const [liveSnap, setLiveSnap] = useState<StepResult['snapshot'] | null>(null);

    // Auto-advance hook (R5)
    const autoAdvance = useAutoAdvance({
        onDay: (record) => {
            if (record.snapshot) setLiveSnap(record.snapshot);
            onRefresh();
        },
        onDone: () => {
            onRefresh();
        },
    });

    // Sync from parent
    useEffect(() => {
        setSession(initialSession);
    }, [initialSession]);

    // Auto mode step
    const handleAutoStep = async () => {
        setStepping(true);
        setStepError(null);
        try {
            const result = await onStep(session.session_id);
            if (result) {
                setLastResult(result);
                onRefresh();
            }
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '推演失败';
            setStepError(msg);
        } finally {
            setStepping(false);
        }
    };

    // Manual mode: propose
    const handlePropose = async () => {
        setProposing(true);
        setStepError(null);
        try {
            const resp = await proposeSession(session.session_id);
            setProposal(resp);
            onRefresh();
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '生成提案失败';
            setStepError(msg);
        } finally {
            setProposing(false);
        }
    };

    // Manual mode: confirm
    const handleConfirm = async (confirmed: ConfirmedOrder[]) => {
        setStepping(true);
        setStepError(null);
        try {
            const result = await onStep(session.session_id, { confirmed });
            if (result) {
                setLastResult(result);
                setProposal(null);
                onRefresh();
            }
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '执行失败';
            setStepError(msg);
        } finally {
            setStepping(false);
        }
    };

    // Manual mode: skip
    const handleSkip = async () => {
        setStepping(true);
        setStepError(null);
        try {
            const result = await onStep(session.session_id, { skip: true });
            if (result) {
                setLastResult(result);
                setProposal(null);
                onRefresh();
            }
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '跳过失败';
            setStepError(msg);
        } finally {
            setStepping(false);
        }
    };

    const isManual = !session.auto_trade;
    const canStep = session.status === 'ready' && session.next_date !== null;
    const canPropose = isManual && (session.status === 'ready' || session.status === 'awaiting_confirm') && session.next_date !== null;
    // 资产数据：自动推演逐日快照 → 手动单步结果 → 后端最新快照（完成后固定为终值）
    const snap = (liveSnap ?? lastResult?.snapshot ?? session.latest_snapshot ?? null) as {
        trade_date?: string;
        total_asset?: number;
        cum_pnl?: number;
        day_pnl?: number;
        cash?: number;
        market_value?: number;
    } | null;
    const pnl = snap?.cum_pnl ?? 0;
    const dayPnl = snap?.day_pnl ?? 0;
    const lotSize = Number((session.strategy_params as Record<string, unknown>)?.lot_size) || 100;

    return (
        <div className="border border-slate-200/80 bg-white/95 rounded-2xl shadow-xs overflow-hidden">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 bg-slate-50/60 border-b border-slate-100">
                <div className="flex items-center gap-2.5">
                    <Clock size={16} className="text-slate-400" />
                    <span className="text-sm font-bold text-slate-800">{session.name || '回放会话'}</span>
                    <StatusBadge status={session.status} />
                    {isManual && (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-lg bg-purple-50 text-purple-700 text-[10px] font-bold border border-purple-200">
                            手动确认
                        </span>
                    )}
                </div>
                <div className="flex items-center gap-2">
                    {/* Report button (when finished or has data) */}
                    {(session.status === 'finished' || session.sessions_done > 0) && (
                        <button
                            onClick={() => onViewReport(session.session_id)}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-gray-200 text-gray-600 text-xs font-medium hover:bg-gray-50 transition-colors"
                        >
                            <BarChart3 size={14} />
                            报告
                        </button>
                    )}
                    {/* Auto mode: step button */}
                    {!isManual && autoAdvance.state === 'idle' && (
                        <button
                            onClick={handleAutoStep}
                            disabled={!canStep || stepping}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-blue-500 text-white text-xs font-medium hover:bg-blue-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        >
                            {stepping ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                            {session.next_date ? `推演 ${session.next_date}` : '已完成'}
                        </button>
                    )}
                    {/* Auto mode: auto-advance controls */}
                    {!isManual && canStep && autoAdvance.state !== 'running' && autoAdvance.state !== 'paused' && (
                        <button
                            onClick={() => autoAdvance.start(session)}
                            disabled={!canStep}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-green-500 text-white text-xs font-medium hover:bg-green-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        >
                            <FastForward size={14} />
                            自动推进
                        </button>
                    )}
                    {!isManual && autoAdvance.state === 'running' && (
                        <button
                            onClick={autoAdvance.pause}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-amber-500 text-white text-xs font-medium hover:bg-amber-600 transition-colors"
                        >
                            <Pause size={14} />
                            暂停
                        </button>
                    )}
                    {!isManual && autoAdvance.state === 'paused' && (
                        <button
                            onClick={autoAdvance.resume}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-green-500 text-white text-xs font-medium hover:bg-green-600 transition-colors"
                        >
                            <Play size={14} />
                            继续
                        </button>
                    )}
                    {!isManual && (autoAdvance.state === 'running' || autoAdvance.state === 'paused') && (
                        <button
                            onClick={autoAdvance.stop}
                            className="p-1.5 rounded-lg text-gray-400 hover:text-red-500 hover:bg-red-50 transition-colors"
                        >
                            <RotateCcw size={14} />
                        </button>
                    )}
                    {/* Speed selector */}
                    {!isManual && (autoAdvance.state === 'running' || autoAdvance.state === 'paused') && (
                        <select
                            value={autoAdvance.speed}
                            onChange={e => autoAdvance.setSpeed(e.target.value as AutoAdvanceSpeed)}
                            className="px-2 py-1 rounded border border-gray-200 text-xs"
                        >
                            <option value="slow">慢 (2s)</option>
                            <option value="medium">中 (1s)</option>
                            <option value="fast">快 (0.3s)</option>
                            <option value="instant">极速</option>
                        </select>
                    )}
                    {/* Manual mode: propose button */}
                    {isManual && !proposal && (
                        <button
                            onClick={handlePropose}
                            disabled={!canPropose || proposing}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-purple-500 text-white text-xs font-medium hover:bg-purple-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        >
                            {proposing ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                            {session.status === 'awaiting_confirm' ? '查看提案' : `生成提案 ${session.next_date ?? ''}`}
                        </button>
                    )}
                    <button
                        onClick={() => onDelete(session.session_id)}
                        className="p-1.5 rounded-lg text-gray-400 hover:text-red-500 hover:bg-red-50 transition-colors"
                    >
                        <Trash2 size={14} />
                    </button>
                </div>
            </div>

            {/* Body */}
            <div className="px-4 py-3 space-y-3">
                {/* Auto-advance progress bar */}
                {autoAdvance.state !== 'idle' && (
                    <div className="space-y-1">
                        <div className="flex justify-between text-xs text-gray-500">
                            <span>
                                {autoAdvance.state === 'running' && '自动推进中…'}
                                {autoAdvance.state === 'paused' && '已暂停'}
                                {autoAdvance.state === 'done' && '已完成'}
                                {autoAdvance.state === 'error' && '推进出错'}
                            </span>
                            <span>{autoAdvance.progress.done} / {autoAdvance.progress.total} 天</span>
                        </div>
                        <div className="w-full h-1.5 bg-gray-100 rounded-full overflow-hidden">
                            <div
                                className={`h-full rounded-full transition-all duration-300 ${
                                    autoAdvance.state === 'error' ? 'bg-red-400' :
                                    autoAdvance.state === 'done' ? 'bg-green-400' : 'bg-blue-400'
                                }`}
                                style={{ width: `${autoAdvance.progress.total ? (autoAdvance.progress.done / autoAdvance.progress.total) * 100 : 0}%` }}
                            />
                        </div>
                        {/* Auto-advance error */}
                        {autoAdvance.errorMessage && (
                            <p className="text-xs text-red-500">{autoAdvance.errorMessage}</p>
                        )}
                        {/* Daily results */}
                        {autoAdvance.records.length > 0 && (
                            <div className="max-h-32 overflow-y-auto space-y-0.5">
                                {autoAdvance.records.map((r, i) => (
                                    <div
                                        key={i}
                                        className={`grid grid-cols-[86px_1fr_auto] items-center gap-2 px-2 py-1 rounded text-xs ${
                                            i === autoAdvance.records.length - 1 && autoAdvance.state === 'running'
                                                ? 'bg-blue-50 animate-pulse'
                                                : ''
                                        }`}
                                    >
                                        <span className="font-mono text-gray-600">{r.trade_date}</span>
                                        <span className="text-gray-400 font-mono">
                                            {r.sell_count || r.buy_count
                                                ? `轮换 ${Math.min(r.sell_count, r.buy_count)} 只 (卖${r.sell_count}/买${r.buy_count})`
                                                : '无调仓'}
                                        </span>
                                        <span className={`font-mono text-right ${r.day_pnl >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                                            {r.day_pnl >= 0 ? '+' : ''}{r.day_pnl.toFixed(0)}
                                        </span>
                                    </div>
                                ))}
                            </div>
                        )}
                        {/* Done: link to report */}
                        {autoAdvance.state === 'done' && (
                            <button
                                onClick={() => onViewReport(session.session_id)}
                                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-blue-500 text-white text-xs font-medium hover:bg-blue-600 transition-colors"
                            >
                                <BarChart3 size={14} />
                                查看报告
                            </button>
                        )}
                    </div>
                )}

                {/* Core Metrics 4-Grid Cards */}
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                    {/* 1. Progress & Cursor */}
                    <div className="bg-slate-50/80 rounded-xl p-3.5 border border-slate-200/70 flex flex-col justify-between">
                        <div className="flex items-center justify-between text-xs text-slate-500 mb-1">
                            <span className="font-semibold">推演进度</span>
                            <span className="font-mono font-bold text-slate-700">{session.sessions_done} / {session.sessions_total} 天</span>
                        </div>
                        <div className="w-full h-1.5 bg-slate-200/80 rounded-full overflow-hidden my-1.5">
                            <div
                                className="h-full bg-blue-600 rounded-full transition-all duration-300"
                                style={{ width: `${session.sessions_total ? (session.sessions_done / session.sessions_total) * 100 : 0}%` }}
                            />
                        </div>
                        <div className="flex items-center justify-between text-[11px] text-slate-400 mt-1">
                            <span>游标: <b className="font-mono text-slate-600">{session.cursor_date ?? '—'}</b></span>
                            <span className="font-mono">{session.start_date} ~ {session.end_date}</span>
                        </div>
                    </div>

                    {/* 2. Total Assets */}
                    <div className="bg-slate-50/80 rounded-xl p-3.5 border border-slate-200/70 flex flex-col justify-between">
                        <div className="text-xs font-semibold text-slate-500 mb-0.5">当前总资产</div>
                        <div className="text-xl font-black text-slate-900 font-mono tracking-tight my-0.5 text-center">
                            ¥ {(snap?.total_asset ?? session.initial_cash ?? 1000000).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}
                        </div>
                        <div className="text-[11px] text-slate-400">
                            {snap?.trade_date
                                ? <>估值日: <b className="font-mono text-slate-600">{snap.trade_date}</b></>
                                : <>初始本金: ¥ {(session.initial_cash ?? 1000000).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}</>}
                        </div>
                    </div>

                    {/* 3. Cumulative PnL */}
                    <div className={`rounded-xl p-3.5 border flex flex-col justify-between ${
                        pnl >= 0 ? 'bg-red-50/50 border-red-100' : 'bg-emerald-50/50 border-emerald-100'
                    }`}>
                        <div className="flex items-center justify-between text-xs mb-0.5">
                            <span className={`font-semibold ${pnl >= 0 ? 'text-red-700' : 'text-emerald-700'}`}>累计盈亏</span>
                            <span className={`text-[10px] font-bold px-1.5 py-0.2 rounded ${
                                pnl >= 0 ? 'bg-red-100/80 text-red-700' : 'bg-emerald-100/80 text-emerald-700'
                            }`}>
                                {pnl >= 0 ? '+' : ''}{((pnl / (session.initial_cash || 1000000)) * 100).toFixed(2)}%
                            </span>
                        </div>
                        <div className={`text-xl font-black font-mono tracking-tight my-0.5 text-center ${pnl >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                            {pnl >= 0 ? '+' : ''}{pnl.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </div>
                        <div className="text-[11px] text-slate-400">
                            日盈亏: <span className={`font-mono font-semibold ${dayPnl >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                                {dayPnl >= 0 ? '+' : ''}{dayPnl.toFixed(2)}
                            </span>
                        </div>
                    </div>

                    {/* 4. Cash & Market Value */}
                    <div className="bg-slate-50/80 rounded-xl p-3.5 border border-slate-200/70 flex flex-col justify-between">
                        <div className="text-xs font-semibold text-slate-500 mb-0.5">资产分布</div>
                        <div className="space-y-1 my-0.5">
                            <div className="flex items-center justify-between text-xs">
                                <span className="text-slate-400">可用现金:</span>
                                <span className="font-mono font-bold text-slate-700">
                                    ¥ {(snap?.cash ?? session.initial_cash ?? 1000000).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}
                                </span>
                            </div>
                            <div className="flex items-center justify-between text-xs">
                                <span className="text-slate-400">持仓市值:</span>
                                <span className="font-mono font-bold text-slate-700">
                                    ¥ {(snap?.market_value ?? 0).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}
                                </span>
                            </div>
                        </div>
                    </div>
                </div>

                {/* Error */}
                {(session.error_message || stepError) && (
                    <div className="flex items-start gap-2 px-3.5 py-2.5 rounded-xl bg-red-50 border border-red-100">
                        <AlertTriangle size={15} className="text-red-500 shrink-0 mt-0.5" />
                        <p className="text-xs font-medium text-red-700">{session.error_message || stepError}</p>
                    </div>
                )}

                {/* Proposal table (manual mode) */}
                {isManual && proposal && (
                    <ProposalTable
                        proposals={proposal.proposals}
                        lotSize={lotSize}
                        onConfirm={handleConfirm}
                        onSkip={handleSkip}
                        loading={stepping}
                    />
                )}

                {/* Last step result */}
                {lastResult && (
                    <div className="space-y-2.5 pt-1">
                        <div className="flex items-center justify-between bg-slate-50/80 px-3.5 py-2 rounded-xl border border-slate-200/60">
                            <h4 className="text-xs font-bold text-slate-700 flex items-center gap-2">
                                <span className="w-2 h-2 rounded-full bg-blue-500"></span>
                                {lastResult.trade_date} 最近成交快照 ({lastResult.filled.length} 笔成交)
                            </h4>
                            <button
                                onClick={() => setShowTrades(!showTrades)}
                                className="text-xs font-medium text-slate-500 hover:text-slate-800 flex items-center gap-1"
                            >
                                <span>{showTrades ? '收起明细' : '展开明细'}</span>
                                {showTrades ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                            </button>
                        </div>

                        {showTrades && (
                            <div className="grid gap-1.5 max-h-48 overflow-y-auto pr-1">
                                {lastResult.filled.map((f, i) => (
                                    <div key={i} className="flex items-center justify-between px-3.5 py-2 rounded-xl bg-slate-50/90 border border-slate-100 text-xs">
                                        <div className="flex items-center gap-2">
                                            <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                                                f.side === 'BUY' ? 'bg-red-50 text-red-600 border border-red-200' : 'bg-emerald-50 text-emerald-600 border border-emerald-200'
                                            }`}>
                                                {f.side === 'BUY' ? '买入' : '卖出'}
                                            </span>
                                            <span className="font-mono font-semibold text-slate-800">{f.symbol}</span>
                                        </div>
                                        <span className="font-mono font-medium text-slate-700">
                                            {f.quantity} 股 @ {f.price.toFixed(2)} 元
                                        </span>
                                        <span className="text-slate-400 font-mono">手续费 ¥{f.total_fee.toFixed(2)}</span>
                                    </div>
                                ))}
                                {lastResult.rejected.length > 0 && lastResult.rejected.map((r, i) => (
                                    <div key={`r-${i}`} className="flex items-center justify-between px-3.5 py-2 rounded-xl bg-red-50/80 border border-red-100 text-xs text-red-600">
                                        <div className="flex items-center gap-2">
                                            <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-red-100 text-red-700">已拒绝</span>
                                            <span className="font-mono font-semibold">{r.symbol}</span>
                                        </div>
                                        <span>{r.side === 'BUY' ? '买入' : '卖出'}</span>
                                        <span className="font-medium">{r.reason}</span>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

const ReplayPage: React.FC = () => {
    const [sessions, setSessions] = useState<ReplaySession[]>([]);
    const [loading, setLoading] = useState(true);
    const [showCreate, setShowCreate] = useState(false);
    const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
    const [reportSessionId, setReportSessionId] = useState<string | null>(null);

    const loadSessions = useCallback(async () => {
        try {
            const list = await listSessions();
            setSessions(list);
            // If no selected session and sessions exist, select the first
            if (list.length > 0) {
                setSelectedSessionId(selectedSessionId && list.some(s => s.session_id === selectedSessionId) ? selectedSessionId : list[0].session_id);
            } else {
                setShowCreate(true);
            }
        } catch {
            // ignore
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { loadSessions(); }, [loadSessions]);

    // If viewing report, show ReplayReportPage
    if (reportSessionId) {
        return (
            <ReplayReportPage
                sessionId={reportSessionId}
                onBack={() => setReportSessionId(null)}
            />
        );
    }

    const handleCreate = (newSession: ReplaySession) => {
        setShowCreate(false);
        loadSessions();
        if (newSession?.session_id) {
            setSelectedSessionId(newSession.session_id);
        }
    };

    const handleStep = async (
        sessionId: string,
        params?: { confirmed?: ConfirmedOrder[]; skip?: boolean },
    ): Promise<StepResult | null> => {
        try {
            const result = await stepSession(sessionId, params);
            await loadSessions();
            return result;
        } catch {
            return null;
        }
    };

    const handleDelete = async (sessionId: string) => {
        try {
            await deleteSession(sessionId);
            const remaining = sessions.filter(s => s.session_id !== sessionId);
            setSessions(remaining);
            if (selectedSessionId === sessionId) {
                setSelectedSessionId(remaining.length > 0 ? remaining[0].session_id : null);
            }
        } catch {
            // ignore
        }
    };

    const activeSession = sessions.find(s => s.session_id === selectedSessionId) ?? (sessions.length > 0 ? sessions[0] : null);

    return (
        <div className="h-full flex flex-col overflow-hidden bg-slate-50/30">
            {/* Top Workspace Header */}
            <div className="shrink-0 px-5 py-3.5 border-b border-slate-100 bg-white/90 backdrop-blur-md flex items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                    <div className="w-9 h-9 rounded-xl bg-blue-50 text-blue-600 flex items-center justify-center border border-blue-100 shadow-2xs">
                        <Clock size={18} />
                    </div>
                    <div>
                        <div className="flex items-center gap-2">
                            <h2 className="text-base font-bold text-slate-800 tracking-tight">时光回放推演</h2>
                            <span className="px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 text-[11px] font-semibold">
                                {sessions.length} 个推演任务
                            </span>
                        </div>
                        <p className="text-xs text-slate-400">A 股历史行情逐日仿真推演与策略决策执行</p>
                    </div>
                </div>

                {/* Right Action Bar */}
                <div className="flex items-center gap-2.5">
                    <button
                        onClick={() => setShowCreate(!showCreate)}
                        className={`inline-flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs font-bold transition-all shadow-xs ${
                            showCreate
                                ? 'bg-slate-100 hover:bg-slate-200 text-slate-700'
                                : 'bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white'
                        }`}
                    >
                        <Plus size={15} />
                        {showCreate ? '返回推演面板' : '新建回放任务'}
                    </button>
                </div>
            </div>

            {/* Main Content Body */}
            <div className="flex-1 overflow-y-auto p-5 space-y-4 custom-scrollbar">
                {/* 1. Create Wizard Mode */}
                {showCreate ? (
                    <div className="max-w-6xl mx-auto bg-white/95 rounded-2xl border border-slate-200/80 shadow-xs overflow-hidden">
                        {/* Header（与 SessionCard 头部条一致） */}
                        <div className="flex items-center justify-between px-4 py-3 bg-slate-50/60 border-b border-slate-100">
                            <div className="flex items-center gap-2.5">
                                <Plus size={15} className="text-slate-400" />
                                <span className="text-sm font-bold text-slate-800">新建回放推演任务</span>
                            </div>
                            {sessions.length > 0 && (
                                <button
                                    onClick={() => setShowCreate(false)}
                                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-200 text-slate-600 text-xs font-medium hover:bg-slate-50 transition-colors"
                                >
                                    取消
                                </button>
                            )}
                        </div>
                        <div className="p-4 bg-slate-50/30">
                            <CreateSessionForm onCreate={handleCreate} />
                        </div>
                    </div>
                ) : (
                    /* 2. Session Workspace Mode */
                    <div className="space-y-4">
                        {loading ? (
                            <div className="flex flex-col items-center justify-center py-20 gap-3">
                                <Loader2 size={28} className="animate-spin text-blue-500" />
                                <p className="text-xs font-medium text-slate-400">正在加载回放会话列表…</p>
                            </div>
                        ) : sessions.length === 0 ? (
                            <div className="max-w-md mx-auto my-12 p-8 text-center bg-white rounded-2xl border border-slate-100 shadow-xs space-y-3">
                                <div className="w-12 h-12 mx-auto rounded-2xl bg-blue-50 text-blue-600 flex items-center justify-center">
                                    <Clock size={24} />
                                </div>
                                <h4 className="text-sm font-bold text-slate-800">暂无回放会话</h4>
                                <p className="text-xs text-slate-400 leading-relaxed">
                                    创建你的第一个历史行情回放，以仿真方式逐日推演模型策略决策。
                                </p>
                                <button
                                    onClick={() => setShowCreate(true)}
                                    className="mt-2 inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-blue-600 text-white text-xs font-bold hover:bg-blue-700 transition-all shadow-xs"
                                >
                                    <Plus size={14} />
                                    立即新建回放
                                </button>
                            </div>
                        ) : (
                            <>
                                {/* Multiple Sessions Switcher Bar */}
                                {sessions.length > 1 && (
                                    <div className="flex items-center gap-2 overflow-x-auto pb-1">
                                        <span className="text-xs font-bold text-slate-400 shrink-0 mr-1">会话切换:</span>
                                        {sessions.map(s => {
                                            const isSelected = s.session_id === selectedSessionId;
                                            return (
                                                <button
                                                    key={s.session_id}
                                                    onClick={() => setSelectedSessionId(s.session_id)}
                                                    className={`shrink-0 flex items-center gap-2 px-3.5 py-2 rounded-xl text-xs font-bold transition-all ${
                                                        isSelected
                                                            ? 'bg-blue-600 text-white shadow-xs'
                                                            : 'bg-white text-slate-600 border border-slate-200/80 hover:bg-slate-50'
                                                    }`}
                                                >
                                                    <span className="font-mono">{s.start_date} ~ {s.end_date}</span>
                                                    <span className={`px-1.5 py-0.2 rounded text-[10px] ${
                                                        isSelected ? 'bg-white/20 text-white' : 'bg-slate-100 text-slate-500'
                                                    }`}>
                                                        {s.sessions_done}/{s.sessions_total}天
                                                    </span>
                                                </button>
                                            );
                                        })}
                                    </div>
                                )}

                                {/* Active Session Cockpit */}
                                {activeSession && (
                                    <SessionCard
                                        key={activeSession.session_id}
                                        session={activeSession}
                                        onStep={handleStep}
                                        onDelete={handleDelete}
                                        onRefresh={loadSessions}
                                        onViewReport={setReportSessionId}
                                    />
                                )}
                            </>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
};

export default ReplayPage;
