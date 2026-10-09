import React, { useState, useRef, useEffect } from 'react';
import { Send, Compass } from 'lucide-react';
import { TaskConfig, UniverseId, UniverseInfo } from '../types-v2';
import { alphaAgentService, MarketInfo } from '../services/alphaAgentService';
import { getUniverses } from '../services-v2/api';

const MARKET_LABELS: Record<string, string> = {
  a_share: 'A股',
  crypto: '加密货币',
  hong_kong: '港股',
  us_stock: '美股',
  futures: '期货',
};

interface ChatInputProps {
  onSubmit: (config: TaskConfig) => void;
  /**
   * 提交在途（POST /evolve 未返回）——只锁提交动作本身。
   * 多任务下「已有任务运行」不再是提交障碍（任务相互独立），运行数走 runningCount。
   */
  isSubmitting?: boolean;
  /** 运行中任务数：只影响占位提示文案，不禁用任何控件 */
  runningCount?: number;
  inline?: boolean;
  initialPrompt?: string;
  /** 「重跑」回填：市场/池/数据源。只在有值时覆盖，空值不动用户当前选择。 */
  initialConfig?: Partial<Pick<TaskConfig, 'miningMarket' | 'universe' | 'dataSource'>>;
  /** 回填代次（`MiningRetryDraft.key`）：同内容连点两次「重跑」也要重新应用 */
  initialConfigKey?: number;
  onSelectPrompt?: (prompt: string) => void;
}

export const ChatInput: React.FC<ChatInputProps> = ({
  onSubmit,
  isSubmitting = false,
  runningCount = 0,
  inline = false,
  initialPrompt = '',
  initialConfig,
  initialConfigKey,
}) => {
  const [input, setInput] = useState(initialPrompt);
  const [useCustomMiningDirection, setUseCustomMiningDirection] = useState(false);
  const [miningMarket, setMiningMarket] = useState<string>('a_share');
  const [universe, setUniverse] = useState<UniverseId>('csi300');
  const [universes, setUniverses] = useState<UniverseInfo[]>([]);
  const [dataSource, setDataSource] = useState<string>('qlib_bin');
  const [markets, setMarkets] = useState<MarketInfo[]>([]);
  const [config] = useState<Partial<TaskConfig>>({ librarySuffix: '' });
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    // 空串不清空：legacy 历史行方向为空是常态，不能把用户打了一半的字擦掉；
    // initialConfigKey 一起看：key 前进（又点了一次「重跑」）时同文案也要重放。
    if (initialPrompt) {
      setInput(initialPrompt);
    }
  }, [initialPrompt, initialConfigKey]);

  useEffect(() => {
    if (!initialConfig) return;
    if (initialConfig.miningMarket) setMiningMarket(initialConfig.miningMarket);
    if (initialConfig.universe) setUniverse(initialConfig.universe);
    if (initialConfig.dataSource) setDataSource(initialConfig.dataSource);
    // 依赖只认代次 key，不认对象身份/内容——同内容连点两次「重跑」
    // 也必须把用户手改过的选项复原（对象比内容会被 React 判成无变化而跳过）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialConfigKey]);

  useEffect(() => {
    alphaAgentService.listMarkets().then(setMarkets).catch(() => {});
    getUniverses()
      .then((res) => setUniverses(res.data?.universes ?? []))
      .catch(() => {});
  }, []);

  const handleSubmit = () => {
    if (isSubmitting) return;
    const suffix = config.librarySuffix?.trim() || undefined;
    onSubmit({
      userInput: input.trim(),
      useCustomMiningDirection,
      miningMarket: miningMarket as TaskConfig['miningMarket'],
      universe,
      dataSource: dataSource as TaskConfig['dataSource'],
      ...config,
      librarySuffix: suffix,
    } as TaskConfig);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = Math.max(52, Math.min(textareaRef.current.scrollHeight, 120)) + 'px';
    }
  }, [input]);

  const marketList = markets.length > 0
    ? markets.map((m) => ({ id: m.market_id, name: m.market_name, ready: m.data_ready }))
    : [
        { id: 'a_share', name: 'A股', ready: true },
        { id: 'crypto', name: '加密货币', ready: true },
        { id: 'hong_kong', name: '港股', ready: false },
        { id: 'us_stock', name: '美股', ready: false },
        { id: 'futures', name: '期货', ready: false },
      ];

  const selectedNotReady = marketList.find(m => m.id === miningMarket)?.ready === false;

  const content = (
    <div className={`relative w-full ${inline ? 'max-w-4xl mx-auto' : 'container mx-auto px-6 max-w-3xl'}`}>
      {/* Animated gradient border ring */}
      <div className="relative rounded-2xl bg-gradient-to-r from-blue-500/30 via-purple-500/30 to-pink-500/30 p-[1px] shadow-xl shadow-blue-500/5 transition-all">
        <div className="rounded-2xl bg-white/95 backdrop-blur-xl overflow-hidden shadow-xs">
          {/* Options row — compact inline chips */}
          <div className="px-4 pt-3.5 pb-1 flex items-center gap-1.5 flex-wrap">
            <span className="text-[10px] font-semibold uppercase tracking-widest text-slate-400 mr-0.5 select-none">市场</span>
            {marketList.map((m) => (
              <button
                key={m.id}
                onClick={() => setMiningMarket(m.id)}
                disabled={isSubmitting || !m.ready}
                className={`rounded-full px-2.5 py-[3px] text-[11px] font-semibold transition-all duration-200 flex items-center gap-1 ${
                  miningMarket === m.id
                    ? 'bg-blue-50 text-blue-600 ring-1 ring-blue-200 shadow-xs'
                    : m.ready
                      ? 'text-slate-500 hover:text-slate-700 hover:bg-slate-100'
                      : 'text-slate-300 cursor-not-allowed'
                }`}
                title={!m.ready ? '数据未就绪，请先在管理后台同步数据' : `${MARKET_LABELS[m.id]}因子挖掘`}
              >
                <span className={`inline-block w-1.5 h-1.5 rounded-full ring-1 ${m.ready ? 'bg-emerald-400 ring-emerald-300' : 'bg-slate-300 ring-slate-200'}`} />
                {m.name}
              </button>
            ))}

            {/* Universe selector */}
            {miningMarket === 'a_share' && (
              <div className="flex items-center gap-1 ml-2 pl-2 border-l border-slate-200">
                <span className="text-[10px] font-semibold uppercase tracking-widest text-slate-400 select-none">池</span>
                <select
                  value={universe}
                  onChange={(e) => setUniverse(e.target.value as UniverseId)}
                  disabled={isSubmitting}
                  className="rounded-full bg-slate-50 px-2.5 py-[3px] text-[11px] font-medium text-slate-600 border-0 focus:outline-none focus:ring-1 focus:ring-blue-200 disabled:opacity-40 appearance-none cursor-pointer"
                  title="选择因子挖掘的股票池"
                >
                  {universes.length > 0
                    ? universes.map((u) => (
                        <option key={u.id} value={u.id}>
                          {u.isSystem === false ? '★ ' : ''}{u.name}{u.stockCount > 0 ? ` (${u.stockCount})` : ''}
                        </option>
                      ))
                    : (
                      <option value="csi300">沪深300</option>
                    )}
                </select>
              </div>
            )}

            {/* Data source */}
            <div className="flex items-center gap-1 ml-1 pl-1 border-l border-slate-200">
              <span className="text-[10px] font-semibold uppercase tracking-widest text-slate-400 select-none">数据</span>
              {[
                { id: 'qlib_bin', name: 'Qlib' },
                { id: 'parquet', name: 'Parquet' },
              ].map((ds) => (
                <button
                  key={ds.id}
                  onClick={() => setDataSource(ds.id)}
                  disabled={isSubmitting}
                  className={`rounded-full px-2.5 py-[3px] text-[11px] font-semibold transition-all duration-200 ${
                    dataSource === ds.id
                      ? 'bg-blue-50 text-blue-600 ring-1 ring-blue-200 shadow-xs'
                      : 'text-slate-500 hover:text-slate-700 hover:bg-slate-100'
                  }`}
                >
                  {ds.name}
                </button>
              ))}
            </div>

            {/* Direction toggle */}
            <button
              type="button"
              onClick={() => setUseCustomMiningDirection(!useCustomMiningDirection)}
              title={useCustomMiningDirection ? '使用设置中的挖掘方向（已开）' : '使用设置中的挖掘方向（点击开启）'}
              className={`ml-1 pl-1 border-l border-slate-200 flex items-center gap-1 rounded-full px-2.5 py-[3px] text-[11px] font-semibold transition-all duration-200 ${
                useCustomMiningDirection
                  ? 'bg-purple-50 text-purple-600 ring-1 ring-purple-200 shadow-xs'
                  : 'text-slate-500 hover:text-slate-700 hover:bg-slate-100'
              }`}
            >
              <Compass className="h-3 w-3" />
              <span>方向</span>
            </button>
          </div>

          {/* Divider */}
          <div className="mx-4 border-t border-slate-100" />

          {/* Textarea + send row */}
          <div className="flex items-end gap-2.5 px-4 py-3">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                isSubmitting
                  ? '任务提交中...'
                  : selectedNotReady
                    ? '该市场数据未就绪，请先在管理后台同步数据'
                    : runningCount > 0
                      ? `已有 ${runningCount} 个任务运行中，可继续提交新想法（任务相互独立）`
                      : useCustomMiningDirection
                        ? '已开启自选挖掘方向，将使用「设置 → 挖掘方向」中的选项'
                        : miningMarket === 'crypto'
                          ? '描述加密货币因子需求，如：短期动量反转、量价背离...'
                          : '描述因子挖掘需求 (如：挖掘基于5日动量反转与成交量偏度组合的Alpha因子)，按 Enter 发送'
              }
              disabled={isSubmitting}
              className="flex-1 bg-transparent text-sm placeholder:text-slate-400 focus:outline-none focus:ring-0 resize-none leading-relaxed font-sans rounded-xl border border-transparent focus:border-blue-200"
              rows={2}
              style={{ minHeight: '44px', maxHeight: '100px' }}
            />

            {/* Send button：多任务下「停止」不属于输入框（每行任务各有自己的停止），
                这里只负责提交；仅在提交在途或市场未就绪时禁用 */}
            <button
              onClick={handleSubmit}
              disabled={isSubmitting || !!selectedNotReady}
              className="flex-shrink-0 p-2.5 rounded-xl bg-gradient-to-br from-blue-500 to-indigo-600 text-white hover:from-blue-600 hover:to-indigo-700 disabled:from-slate-300 disabled:to-slate-400 disabled:cursor-not-allowed transition-all duration-200 hover:scale-105 active:scale-95 shadow-lg shadow-blue-500/25 disabled:shadow-none cursor-pointer"
              title={selectedNotReady ? '市场数据未就绪' : '发送 (Enter)'}
            >
              <Send className="h-4 w-4" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );

  if (inline) {
    return <div className="w-full relative">{content}</div>;
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
    </div>
  );
};
