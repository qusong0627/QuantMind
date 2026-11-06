import React, { useState, useRef, useEffect } from 'react';
import { Send, Square, Compass, WandSparkles, BookOpen, CalendarDays, ChevronDown, Loader2 } from 'lucide-react';
import { useAppSelector } from '../../../store';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { getSessionScopedStorageKey } from '../../../store/sessionScopedStorage';
import { TaskConfig, UniverseId, UniverseInfo, type StatisticalMiningOptions } from '../types-v2';
import { StatisticalOptionsPanel, StatisticalOptionsSummary, effectiveStatisticalOptions, statisticalOptionsError } from './StatisticalOptionsPanel';
import { getUniverses } from '../services-v2/api';
import type { MiningPreview } from '../services-v2/api';
import {
  getMiningAlgorithms,
  type MiningAlgorithmOption,
} from '../services-v2/algorithmApi';

interface ChatInputProps {
  onSubmit: (config: TaskConfig) => void;
  onPreview: (config: TaskConfig) => Promise<MiningPreview>;
  onResearchDirections?: (config: TaskConfig) => void;
  onSampleDirections?: (config: TaskConfig, quotaSupported: boolean) => void;
  onResearchExperience?: (config: TaskConfig) => void;
  onVariantLedger?: (config: TaskConfig) => void;
  onStop?: () => void;
  isRunning?: boolean;
  inline?: boolean;
  initialPrompt?: string;
  onSelectPrompt?: (prompt: string) => void;
}

function preferredUniverse(): UniverseId {
  try {
    const key = getSessionScopedStorageKey('autoalpha_config');
    const saved = key ? localStorage.getItem(key) : null;
    const value = saved ? JSON.parse(saved)?.defaultUniverse : null;
    return typeof value === 'string' && value.trim() ? value : 'all_a';
  } catch {
    return 'all_a';
  }
}

export const ChatInput: React.FC<ChatInputProps> = ({
  onSubmit,
  onPreview,
  onResearchDirections,
  onSampleDirections,
  onResearchExperience,
  onVariantLedger,
  onStop,
  isRunning = false,
  inline = false,
  initialPrompt = '',
}) => {
  const isAuthenticated = useAppSelector((state) => state.auth.isAuthenticated);
  const isAuthenticating = useAppSelector((state) => state.auth.isLoading);
  const [sessionEpoch, setSessionEpoch] = useState(() => getSessionGeneration());
  const sessionGenerationRef = useRef(getSessionGeneration());
  const [input, setInput] = useState(initialPrompt);
  const [useCustomMiningDirection, setUseCustomMiningDirection] = useState(false);
  const [universe, setUniverse] = useState<UniverseId>(preferredUniverse);
  const [universes, setUniverses] = useState<UniverseInfo[]>([]);
  const [algorithms, setAlgorithms] = useState<MiningAlgorithmOption[]>([]);
  const [algorithmsReady, setAlgorithmsReady] = useState(false);
  const [algorithm, setAlgorithm] = useState<'rdagent' | 'statistical'>('rdagent');
  const [windowExpanded, setWindowExpanded] = useState(false);
  const algorithmRef = useRef<'rdagent' | 'statistical'>('rdagent');
  const selectAlgorithm = (value: 'rdagent' | 'statistical') => {
    algorithmRef.current = value;
    setAlgorithm(value);
    setWindowExpanded(value === 'statistical');
  };
  const [miningStart, setMiningStart] = useState('');
  const [miningEnd, setMiningEnd] = useState('');
  const [validStart, setValidStart] = useState('');
  const [validEnd, setValidEnd] = useState('');
  const [statisticalConfig, setStatisticalConfig] = useState<StatisticalMiningOptions | undefined>();
  const [preview, setPreview] = useState<MiningPreview | null>(null);
  const [pendingConfig, setPendingConfig] = useState<TaskConfig | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const reset = () => {
      sessionGenerationRef.current = getSessionGeneration();
      setUniverses([]);
      setAlgorithms([]);
      setAlgorithmsReady(false);
      setInput('');
      setUniverse(preferredUniverse());
      selectAlgorithm('rdagent');
      setUseCustomMiningDirection(false);
      setMiningStart('');
      setMiningEnd('');
      setValidStart('');
      setValidEnd('');
      setStatisticalConfig(undefined);
      setPreview(null);
      setPendingConfig(null);
      setPreviewError('');
      setPreviewing(false);
      setSessionEpoch(getSessionGeneration());
    };
    sessionGenerationRef.current = getSessionGeneration();
    return registerSessionReset(reset);
  }, []);

  useEffect(() => {
    if (initialPrompt) {
      setInput(initialPrompt);
    }
  }, [initialPrompt]);

  useEffect(() => {
    if (!isAuthenticated || isAuthenticating) {
      setUniverses([]);
      return;
    }
    const generation = sessionGenerationRef.current;
    getUniverses()
      .then((res) => {
        if (generation === sessionGenerationRef.current) {
          setUniverses(res.data?.universes ?? []);
        }
      })
      .catch(() => {});
  }, [isAuthenticated, isAuthenticating, sessionEpoch]);

  useEffect(() => {
    if (!isAuthenticated || isAuthenticating) {
      setAlgorithms([]);
      setAlgorithmsReady(false);
      return;
    }
    const generation = sessionGenerationRef.current;
    getMiningAlgorithms().then((res) => {
      if (generation !== sessionGenerationRef.current) return;
      const available = res.data?.algorithms ?? [];
      setAlgorithms(available);
      setAlgorithmsReady(res.data?.available === true && available.length > 0);
      if (!available.some((item) => item.key === algorithmRef.current) && available[0]) {
        selectAlgorithm(available[0].key);
      }
    });
  }, [isAuthenticated, isAuthenticating, sessionEpoch]);

  const invalidDateWindow = (
    (algorithm === 'statistical' && (!miningStart || !miningEnd))
    || Boolean(miningStart) !== Boolean(miningEnd)
    || (Boolean(miningStart && miningEnd) && miningStart > miningEnd)
    || Boolean(validStart) !== Boolean(validEnd)
    || (Boolean(validStart && validEnd) && validStart > validEnd)
    || (Boolean(validStart && miningEnd) && validStart <= miningEnd)
    || (Boolean(validStart) && !miningStart)
  );

  const statisticalDefaults = algorithms.find((item) => item.key === 'statistical')?.statistical_defaults;
  const effectiveStatisticalConfig = effectiveStatisticalOptions(statisticalDefaults, statisticalConfig);
  const invalidStatisticalOptions = algorithm === 'statistical' && effectiveStatisticalConfig
    ? statisticalOptionsError(effectiveStatisticalConfig)
    : null;

  const buildTaskConfig = (): TaskConfig => ({
    userInput: input.trim(),
    algorithm,
    statisticalConfig: algorithm === 'statistical' ? effectiveStatisticalConfig : undefined,
    autoMaterialize: false,
    miningStart: miningStart || undefined,
    miningEnd: miningEnd || undefined,
    validStart: validStart || undefined,
    validEnd: validEnd || undefined,
    useCustomMiningDirection,
    miningMarket: 'a_share',
    universe,
    dataSource: 'qlib_bin',
    // Omit datasets: the server resolves the full registered QuantDB set.
    freq: 'day',
  });

  const handleSubmit = async () => {
    if (isRunning || previewing || invalidDateWindow || invalidStatisticalOptions
      || !isAuthenticated || isAuthenticating || !algorithmsReady) return;
    const generation = getSessionGeneration();
    const taskConfig = buildTaskConfig();
    setPreviewing(true);
    setPreviewError('');
    try {
      const result = await onPreview(taskConfig);
      if (generation !== getSessionGeneration()) return;
      setPendingConfig(taskConfig.algorithm === 'statistical' && result.statistical_config
        ? { ...taskConfig, statisticalConfig: result.statistical_config }
        : taskConfig);
      setPreview(result);
    } catch (error) {
      if (generation !== getSessionGeneration()) return;
      setPreviewError(error instanceof Error ? error.message : '参数预览失败');
    } finally {
      if (generation === getSessionGeneration()) setPreviewing(false);
    }
  };

  const confirmSubmit = () => {
    if (!pendingConfig || !preview) return;
    onSubmit(pendingConfig);
    setPendingConfig(null);
    setPreview(null);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSubmit();
    }
  };

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = Math.max(112, Math.min(textareaRef.current.scrollHeight, 240)) + 'px';
    }
  }, [input, algorithm]);

  const content = (
    <div className={`relative w-full ${inline ? 'max-w-4xl mx-auto' : 'container mx-auto px-6 max-w-3xl'}`}>
      <div className="overflow-hidden rounded-3xl border border-slate-200/90 bg-white/95 shadow-xl shadow-slate-900/5 backdrop-blur-xl">
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-b border-slate-100 bg-slate-50/60 px-5 py-4 sm:px-6">
          <div className="flex min-w-0 flex-wrap items-center gap-3" aria-label="挖掘算法">
            <span className="text-xs font-medium text-slate-500">挖掘方式</span>
            <div className="flex flex-wrap gap-1 rounded-xl bg-slate-100 p-1">
              {algorithms.map((option) => (
                <button
                  key={option.key}
                  type="button"
                  onClick={() => selectAlgorithm(option.key)}
                  disabled={isRunning || !algorithmsReady}
                  aria-pressed={algorithm === option.key}
                  className={`rounded-lg px-3 py-2 text-xs font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 disabled:opacity-40 ${
                    algorithm === option.key
                      ? 'bg-white text-blue-600 shadow-xs'
                      : 'text-slate-500 hover:text-slate-800'
                  }`}
                >
                  {option.name}
                </button>
              ))}
            </div>
          </div>
          <label className="flex min-w-0 max-w-full items-center gap-2 text-xs text-slate-500">
            <span className="shrink-0">股票池</span>
            <select
              aria-label="选择因子挖掘的股票池"
              value={universe}
              onChange={(e) => setUniverse(e.target.value as UniverseId)}
              disabled={isRunning}
              className="min-w-0 max-w-52 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-200 disabled:opacity-40"
              title="选择因子挖掘的股票池"
            >
              {universes.length > 0
                ? universes.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.isSystem === false ? '★ ' : ''}{u.name}{u.stockCount > 0 ? ` (${u.stockCount})` : ''}
                    </option>
                  ))
                : <option value={universe}>{universe === 'all_a' ? '全部A股' : universe === 'csi500' ? '中证500' : universe === 'csi300' ? '沪深300' : universe}</option>}
            </select>
            <span className="shrink-0 rounded-md bg-blue-50 px-2 py-1 text-[11px] font-medium text-blue-600">A股</span>
          </label>
          {!algorithmsReady && isAuthenticated && (
            <p role="status" className="w-full text-xs text-amber-700">算法能力暂不可用，暂不能提交</p>
          )}
        </div>

        <div className="px-5 pt-5 sm:px-6">
          {algorithm === 'statistical'
            ? <p className="text-xs font-semibold text-slate-800">统计因子搜索</p>
            : <label htmlFor="mining-research-prompt" className="text-xs font-semibold text-slate-800">研究假设</label>}
          <p className="mt-1 text-xs leading-5 text-slate-400">
            {algorithm === 'statistical' ? '选择日期窗口后，按固定候选空间搜索日频量价因子。' : '描述你想验证的市场规律、信号或因子组合。'}
          </p>
          {algorithm === 'rdagent' && (
            <textarea
              id="mining-research-prompt"
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                isRunning
                  ? '实验运行中...可切换页面，任务不会中断'
                  : useCustomMiningDirection
                    ? '已开启自选挖掘方向，将使用「设置 → 挖掘方向」中的选项'
                    : '例如：寻找短期动量反转与成交量变化之间的关系，挖掘具有稳定超额收益的因子…'
              }
              disabled={isRunning}
              className="mt-3 block w-full resize-none rounded-xl border border-slate-100 bg-slate-50/50 px-4 py-3 text-xs leading-7 text-slate-800 placeholder:text-slate-400 focus:border-blue-200 focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-50 disabled:opacity-60"
              rows={4}
              style={{ minHeight: '112px', maxHeight: '240px' }}
            />
          )}
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-3">
            <div className="flex flex-wrap items-center gap-1">
              {algorithm === 'rdagent' && (
                <button
                  type="button"
                  onClick={() => setUseCustomMiningDirection(!useCustomMiningDirection)}
                  disabled={isRunning || previewing}
                  aria-pressed={useCustomMiningDirection}
                  title="使用「设置 → 挖掘方向」中的选项"
                  className={`inline-flex items-center gap-1.5 rounded-lg px-2 py-2 text-xs transition-colors disabled:opacity-40 ${useCustomMiningDirection ? 'bg-purple-50 text-purple-700' : 'text-slate-500 hover:bg-slate-50 hover:text-slate-700'}`}
                >
                  <Compass className="h-3.5 w-3.5" />预设方向{useCustomMiningDirection ? ' · 已启用' : ''}
                </button>
              )}
              {onResearchExperience && algorithms.find((item) => item.key === algorithm)?.supports_experience_memory === true && (
                <button type="button" onClick={() => onResearchExperience(buildTaskConfig())}
                  disabled={!isAuthenticated || isAuthenticating || !algorithmsReady}
                  className="inline-flex items-center gap-1.5 rounded-lg px-2 py-2 text-xs text-slate-500 hover:bg-slate-50 hover:text-slate-700 disabled:opacity-40">
                  <BookOpen className="h-3.5 w-3.5" />本人经验
                </button>
              )}
              {onVariantLedger && algorithms.find((item) => item.key === algorithm)?.supports_variant_ledger === true && (
                <button type="button" onClick={() => onVariantLedger(buildTaskConfig())}
                  disabled={!isAuthenticated || isAuthenticating || !algorithmsReady}
                  className="rounded-lg px-2 py-2 text-xs text-slate-500 hover:bg-slate-50 hover:text-slate-700 disabled:opacity-40">变体台账</button>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {algorithm === 'rdagent' && algorithms.find((item) => item.key === algorithm)?.supports_direction_batches === true && onResearchDirections && (
                <button
                  type="button"
                  onClick={() => onResearchDirections(buildTaskConfig())}
                  disabled={isRunning || previewing || !isAuthenticated || isAuthenticating || !algorithmsReady || invalidDateWindow || !input.trim() || useCustomMiningDirection}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-600 hover:border-blue-200 hover:text-blue-600 disabled:cursor-not-allowed disabled:opacity-40"
                  title={useCustomMiningDirection ? '请关闭自选方向并填写研究需求' : '拆解研究方向并批量挖掘'}
                >
                  <WandSparkles className="h-4 w-4" />
                  拆解方向
                </button>
              )}

              {algorithm === 'rdagent' && algorithms.find((item) => item.key === algorithm)?.supports_sampled_direction_batches === true && onSampleDirections && (
                <button type="button" onClick={() => onSampleDirections(buildTaskConfig(), algorithms.find((item) => item.key === algorithm)?.supports_direction_quota === true)}
                  disabled={isRunning || previewing || !isAuthenticated || isAuthenticating || !algorithmsReady || invalidDateWindow}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-600 hover:border-blue-200 hover:text-blue-600 disabled:opacity-40">
                  多方向选择
                </button>
              )}
            </div>
          </div>
        </div>

        <div className="border-t border-slate-100 px-5 sm:px-6">
          <button
            type="button"
            onClick={() => setWindowExpanded(!windowExpanded)}
            aria-expanded={windowExpanded}
            aria-controls="mining-date-window"
            className="flex w-full items-center gap-2 py-3 text-left text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-300"
          >
            <CalendarDays className="h-4 w-4 shrink-0 text-slate-400" />
            <span className="shrink-0 font-medium text-slate-600">{algorithm === 'statistical' ? '统计窗口' : '实验窗口'}</span>
            <span className={`min-w-0 flex-1 truncate ${invalidDateWindow ? 'text-amber-700' : 'text-slate-400'}`}>
              {invalidDateWindow ? '请完善日期区间' : miningStart ? `${miningStart} 至 ${miningEnd}${validStart ? ` · OOS ${validStart} 至 ${validEnd}` : ''}` : '使用系统默认窗口 · 可选'}
            </span>
            <ChevronDown className={`h-4 w-4 shrink-0 text-slate-400 transition-transform ${windowExpanded ? 'rotate-180' : ''}`} />
          </button>
          <div id="mining-date-window" hidden={!windowExpanded} className="pb-4">
            <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,20rem),1fr))] gap-4">
              <fieldset className="min-w-0">
                <legend className="mb-2 text-xs font-medium text-slate-600">挖掘区间{algorithm === 'statistical' ? '（必填）' : '（可选）'}</legend>
                <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2 text-xs text-slate-400">
                  <input aria-label={`${algorithm === 'statistical' ? '统计' : 'RD-Agent'}挖掘开始日期`} type="date" value={miningStart} onChange={(event) => setMiningStart(event.target.value)} disabled={isRunning} className="w-full min-w-0 rounded-lg border border-slate-200 bg-white px-2 py-2 text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-100 disabled:opacity-50" />
                  <span>至</span>
                  <input aria-label={`${algorithm === 'statistical' ? '统计' : 'RD-Agent'}挖掘结束日期`} type="date" value={miningEnd} onChange={(event) => setMiningEnd(event.target.value)} disabled={isRunning} className="w-full min-w-0 rounded-lg border border-slate-200 bg-white px-2 py-2 text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-100 disabled:opacity-50" />
                </div>
              </fieldset>
              <fieldset className="min-w-0">
                <legend className="mb-2 text-xs font-medium text-slate-600">样本外检验 OOS（可选）</legend>
                <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2 text-xs text-slate-400">
                  <input aria-label={`${algorithm === 'statistical' ? '统计' : 'RD-Agent'}验证开始日期`} type="date" value={validStart} onChange={(event) => setValidStart(event.target.value)} disabled={isRunning} className="w-full min-w-0 rounded-lg border border-slate-200 bg-white px-2 py-2 text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-100 disabled:opacity-50" />
                  <span>至</span>
                  <input aria-label={`${algorithm === 'statistical' ? '统计' : 'RD-Agent'}验证结束日期`} type="date" value={validEnd} onChange={(event) => setValidEnd(event.target.value)} disabled={isRunning} className="w-full min-w-0 rounded-lg border border-slate-200 bg-white px-2 py-2 text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-100 disabled:opacity-50" />
                </div>
              </fieldset>
            </div>
            <p className={`mt-3 text-xs leading-5 ${invalidDateWindow ? 'text-amber-700' : 'text-slate-400'}`}>
              {algorithm === 'statistical' ? '请填写完整挖掘区间。' : '留空使用服务器默认训练窗口。'}日期须从早到晚，OOS 必须位于挖掘区间之后。
            </p>
          </div>
        </div>

        {algorithm === 'statistical' && effectiveStatisticalConfig && (
          <>
            <StatisticalOptionsPanel key={sessionEpoch} value={effectiveStatisticalConfig} disabled={isRunning || previewing} onChange={setStatisticalConfig} />
          </>
        )}

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 bg-slate-50/60 px-5 py-4 sm:px-6">
          <span className="text-xs text-slate-400">{algorithm === 'statistical' ? '确认参数后开始搜索' : 'Enter 预览 · Shift + Enter 换行'}</span>
          {isRunning && onStop ? (
            <button
              type="button"
              onClick={onStop}
              className="ml-auto inline-flex items-center gap-2 rounded-xl bg-red-500 px-5 py-2.5 text-xs font-semibold text-white transition-colors hover:bg-red-600"
              title="中断实验"
            >
              <Square className="h-4 w-4" />
              停止实验
            </button>
          ) : (
            <button
              type="button"
              onClick={handleSubmit}
              disabled={isRunning || previewing || !isAuthenticated || isAuthenticating || !algorithmsReady || invalidDateWindow || Boolean(invalidStatisticalOptions)}
              className="ml-auto inline-flex items-center gap-2 rounded-xl bg-blue-600 px-5 py-2.5 text-xs font-semibold text-white shadow-sm shadow-blue-500/20 transition-colors hover:bg-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:bg-slate-300 disabled:shadow-none"
              title={previewing ? '正在预览参数' : '发送 (Enter)'}
            >
              {previewing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              {previewing ? '正在预览' : '预览并开始'}
            </button>
          )}
        </div>
      </div>
    </div>
  );

  if (inline) {
    return (
      <div className="w-full relative">
        {content}
        {preview && pendingConfig && <PreviewDialog preview={preview} onConfirm={confirmSubmit} onCancel={() => { setPreview(null); setPendingConfig(null); }} />}
        {previewError && <p role="alert" className="mx-auto mt-2 max-w-4xl text-xs text-red-600">参数预览失败：{previewError}</p>}
      </div>
    );
  }

  return (
    <div
      className="fixed left-0 right-0 z-40 flex flex-col items-center"
      style={{ bottom: 0, paddingBottom: '88px' }}
    >
      {/* Gradient scrim */}
      <div
        className="pointer-events-none absolute inset-x-0"
        style={{
          bottom: 0,
          height: '160px',
          background: 'linear-gradient(to bottom, hsl(var(--background) / 0) 0%, hsl(var(--background) / 0.6) 40%, hsl(var(--background) / 0.95) 70%, hsl(var(--background)) 100%)',
        }}
      />
      {content}
      {preview && pendingConfig && <PreviewDialog preview={preview} onConfirm={confirmSubmit} onCancel={() => { setPreview(null); setPendingConfig(null); }} />}
      {previewError && <p role="alert" className="mx-auto mt-2 max-w-4xl text-xs text-red-600">参数预览失败：{previewError}</p>}
    </div>
  );
};

const PreviewDialog: React.FC<{
  preview: MiningPreview;
  onConfirm: () => void;
  onCancel: () => void;
}> = ({ preview, onConfirm, onCancel }) => (
  <div role="dialog" aria-modal="true" aria-label="挖掘参数预览" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4">
    <div className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-2xl bg-white p-5 shadow-2xl">
      <h3 className="text-sm font-bold text-slate-900">确认挖掘参数</h3>
      <dl className="mt-3 grid grid-cols-[7rem_1fr] gap-x-3 gap-y-2 text-xs">
        <dt className="text-slate-500">算法</dt><dd>{preview.algorithm.name}（{preview.algorithm.version}）</dd>
        <dt className="text-slate-500">市场 / 频率</dt><dd>{preview.market} / {preview.freq}</dd>
        <dt className="text-slate-500">股票池</dt><dd>{preview.universe}</dd>
        {preview.algorithm.key === 'rdagent' && <><dt className="text-slate-500">挖掘方向</dt><dd className="whitespace-pre-wrap break-words">{preview.direction || '未填写'}{preview.directions.length > 0 ? `；类别：${preview.directions.join('、')}（${preview.direction_mode === 'random' ? '随机选择' : preview.direction_mode === 'quota' ? '配额加权选择' : '按首项选择'}）` : ''}</dd></>}
        {preview.direction_weighting === 'blankness' && <><dt className="text-slate-500">随机口径</dt><dd>按本人在该股票池的历史提交次数使用 1/(1+n) 加权，提交时抽取；历史不可用时明确记录均匀兜底。种子及候选权重保存在任务详情。</dd></>}
        {preview.direction_quota && <><dt className="text-slate-500">配额预演</dt><dd>{preview.direction_quota.available ? `三档名额 ${preview.direction_quota.slots.explore}/${preview.direction_quota.slots.exploit}/${preview.direction_quota.slots.mutate}（探索/优选/变异），已打分 ${preview.direction_quota.directions.length} 条候选` : `暂不可用：${preview.direction_quota.error ?? '方向史或饱和数据读取出错'}，提交时将退回历史空白度加权`}</dd></>}
        <dt className="text-slate-500">日期窗口</dt><dd>{preview.window.mode === 'custom' ? preview.algorithm.key === 'statistical'
          ? `${preview.window.mining?.join(' 至 ')}${preview.window.oos ? `；OOS ${preview.window.oos.join(' 至 ')}` : '；未设置 OOS'}`
          : `${preview.window.mining?.join(' 至 ')}；训练 ${preview.window.train?.join(' 至 ')}；验证 ${preview.window.valid?.join(' 至 ')}；测试 ${preview.window.test?.join(' 至 ')}${preview.window.oos ? `；OOS ${preview.window.oos.join(' 至 ')}` : ''}` : '使用服务器默认窗口'}</dd>
        {preview.algorithm.key === 'rdagent' && <><dt className="text-slate-500">演化轮数</dt><dd>{preview.loop_n}</dd></>}
        <dt className="text-slate-500">LLM</dt><dd>{preview.requires_llm ? '需要' : '不需要'}</dd>
      </dl>
      {preview.algorithm.key === 'statistical' && preview.statistical_config && <StatisticalOptionsSummary value={preview.statistical_config} />}
      <div className="mt-5 flex justify-end gap-2">
        <button type="button" onClick={onCancel} className="rounded-lg border border-slate-200 px-3 py-2 text-xs text-slate-600">返回修改</button>
        <button type="button" onClick={onConfirm} className="rounded-lg bg-blue-600 px-3 py-2 text-xs font-semibold text-white">确认并开始</button>
      </div>
    </div>
  </div>
);
