import React, { useCallback, useEffect, useState } from 'react';
import { Button, Card, Tag, Typography, Spin, Empty, Collapse, Progress, Modal, message } from 'antd';
import { Database, Calendar, RefreshCw, Zap, CheckCircle2, AlertTriangle, Clock } from 'lucide-react';
import { modelTrainingService } from '../services/modelTrainingService';

const { Text } = Typography;

const STORAGE_KEY = (modelId: string) => `qm:backfill:${modelId}`;

export const InferenceCoveragePanel: React.FC<{ modelId: string }> = ({ modelId }) => {
  const [loading, setLoading] = useState(true);
  const [coverage, setCoverage] = useState<any>(null);
  const [backfilling, setBackfilling] = useState(false);
  const [task, setTask] = useState<any>(null);
  const [tradingMap, setTradingMap] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    if (!modelId) return;
    setLoading(true);
    try {
      const data = await modelTrainingService.getInferenceCoverage(modelId);
      setCoverage(data);
    } catch (e: any) {
      // 后端未部署时友好空态
      setCoverage({ min_date: null, max_date: null, count: 0, gap_dates: [], latest_trade_date: null, is_up_to_date: false, reason: e?.message });
    } finally {
      setLoading(false);
    }
  }, [modelId]);

  useEffect(() => { void load(); }, [load]);

  // 切页恢复：若存在进行中的后台任务，自动恢复轮询
  useEffect(() => {
    if (!modelId) return;
    const stored = localStorage.getItem(STORAGE_KEY(modelId));
    if (!stored) return;
    const taskId = stored.trim();
    if (!taskId) return;
    let cancelled = false;
    const resume = async () => {
      try {
        const st = await modelTrainingService.getBackfillStatus(modelId, taskId);
        if (cancelled) return;
        if (st?.status === 'running') {
          setTask(st);
          setBackfilling(true);
          // 继续轮询直至完成
          for (let i = 0; i < 120; i++) {
            await new Promise((r) => setTimeout(r, 2000));
            if (cancelled) return;
            try {
              const cur = await modelTrainingService.getBackfillStatus(modelId, taskId);
              if (cancelled) return;
              setTask(cur);
              if (cur?.status === 'completed' || cur?.status === 'failed' || cur?.status === 'partial') {
                if (cur?.status === 'completed') message.success(`补全完成，追加 ${cur?.appended ?? 0} 日`);
                else if (cur?.status === 'partial') message.warning(cur?.error || `部分成功：追加 ${cur?.appended ?? 0} 日，${cur?.failed ?? 0} 日失败`);
                else message.error(cur?.error || '补全失败');
                localStorage.removeItem(STORAGE_KEY(modelId));
                setBackfilling(false);
                void load();
                return;
              }
            } catch {
              // 忽略轮询错误
            }
          }
          setBackfilling(false);
        } else {
          // 已结束的任务清理存储，失败/完成时展示一下进度
          if (st?.status === 'completed' || st?.status === 'failed') {
            setTask(st);
            // 若已完成可短期展示后清理，避免切回仍显示旧进度时误以为进行中
            if (st?.status === 'completed') localStorage.removeItem(STORAGE_KEY(modelId));
          } else {
            localStorage.removeItem(STORAGE_KEY(modelId));
          }
        }
      } catch {
        localStorage.removeItem(STORAGE_KEY(modelId));
      }
    };
    void resume();
    return () => { cancelled = true; };
  }, [modelId, load]);

  // 量化交易日历：批量判断 3 个月所有日期是否为交易日，替代周末判断
  useEffect(() => {
    const latest = coverage?.latest_trade_date ? String(coverage.latest_trade_date).slice(0, 10) : null;
    const baseDate = latest ? new Date(latest) : new Date();
    const months = [-2, -1, 0].map((off) => {
      const d = new Date(baseDate.getFullYear(), baseDate.getMonth() + off, 1);
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    });
    const allDates: string[] = [];
    for (const ym of months) {
      const [y, m] = ym.split('-').map(Number);
      const days = new Date(y, m, 0).getDate();
      for (let d = 1; d <= days; d++) allDates.push(`${ym}-${String(d).padStart(2, '0')}`);
    }
    void modelTrainingService.batchCheckTradingDays('SSE', allDates).then(setTradingMap).catch(() => setTradingMap({}));
  }, [coverage?.latest_trade_date]);

  const gapCount = coverage?.gap_dates?.length ?? 0;
  const isUpToDate = coverage?.is_up_to_date ?? (gapCount === 0 && coverage?.max_date);
  // estimated: pred.parquet 缺失时后端用 QuantDB 因子可支持范围兜底，
  // 区间是"可补全范围"而非真实推理覆盖
  const estimated = Boolean((coverage as any)?.estimated);

  const handleBackfill = () => {
    Modal.confirm({
      title: '一键补全推理历史',
      content: `将补齐检测到的 ${gapCount} 个缺口交易日（含历史中间空洞），并逐日推理追加到 pred.parquet。是否继续？`,
      okText: '开始补全',
      cancelText: '取消',
      centered: false,
      style: { top: 200 },
      wrapClassName: 'inference-backfill-modal-wrap',
      styles: { mask: { backdropFilter: 'blur(8px)', WebkitBackdropFilter: 'blur(8px)', backgroundColor: 'rgba(15,23,42,0.2)' } } as any,
      onOk: async () => {
        setBackfilling(true);
        setTask({ status: 'running', progress: 0, logs: '' });
        try {
          const res = await modelTrainingService.triggerInferenceBackfill(modelId);
          const taskId = res?.task_id || res?.taskId;
          if (!taskId) {
            if (res?.status === 'completed' && res?.gap === 0) message.info(res?.message || '已是最新，无需补全');
            else if (res?.status === 'failed') message.error(res?.error || res?.message || '触发失败');
            else message.success(res?.message || '已触发补全');
            setBackfilling(false);
            void load();
            return;
          }
          localStorage.setItem(STORAGE_KEY(modelId), taskId);
          // 轮询
          const poll = async () => {
            for (let i = 0; i < 120; i++) {
              await new Promise((r) => setTimeout(r, 2000));
              try {
                const st = await modelTrainingService.getBackfillStatus(modelId, taskId);
                setTask(st);
                if (st?.status === 'completed' || st?.status === 'failed' || st?.status === 'partial') {
                  if (st?.status === 'completed') message.success(`补全完成，追加 ${st?.appended ?? gapCount} 日`);
                  else if (st?.status === 'partial') message.warning(st?.error || `部分成功：追加 ${st?.appended ?? gapCount} 日，${st?.failed ?? 0} 日失败`);
                  else message.error(st?.error || '补全失败');
                  localStorage.removeItem(STORAGE_KEY(modelId));
                  setBackfilling(false);
                  void load();
                  return;
                }
              } catch {
                // 忽略轮询错误
              }
            }
            // 超时仍保留 taskId 供下次切回继续轮询
            setBackfilling(false);
          };
          void poll();
        } catch (e: any) {
          message.error(e?.message ?? '触发失败');
          setBackfilling(false);
        }
      },
    });
  };

  if (loading) return <div className="flex justify-center py-12"><Spin /></div>;

  return (
    <div className="space-y-4">
      <style>{`.inference-backfill-modal-wrap .ant-modal{top:200px !important;} .inference-backfill-modal-wrap.ant-modal-wrap{padding-top:100px;}`}</style>
      <Card size="small" className="rounded-2xl">
        <Collapse
          ghost
          items={[{
            key: 'explain',
            label: <span className="text-xs font-black text-slate-700 flex items-center gap-1.5"><Database size={12} className="text-blue-500"/>口径说明</span>,
            children: <div className="text-xs text-slate-600 leading-relaxed space-y-1">
              <div>覆盖基于 `pred.parquet`（`storage_path/pred.parquet`）的 `trade_date` 去重，与个股终端历史推理同源；`engine_signal_scores` 为 `completed` 落库。</div>
              <div>补全按交易日逐日跑 `inference.py` 并追加到 `pred.parquet`，目标至 `latest_trade_date`（`SSE` 日历）。</div>
              <div>`pred.parquet` 缺失时区间显示为 QuantDB 因子可支持范围（预估口径，蓝色标记），补全后才生成真实覆盖。</div>
            </div>,
          }]}
        />
      </Card>

      <div className="grid grid-cols-3 gap-3">
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">{estimated ? '可支持区间（预估）' : '覆盖区间'}</div>
          <div className="text-xs font-mono font-black text-slate-800 mt-1">{coverage?.min_date ?? '—'} ~ {coverage?.max_date ?? '—'}</div>
          <div className={`text-[11px] ${estimated ? 'text-amber-600 font-bold' : 'text-slate-400'}`}>
            {estimated ? '尚无真实推理记录，补全后生成' : `共 ${coverage?.count ?? 0} 个交易日`}
          </div>
        </Card>
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">最新交易日</div>
          <div className="text-lg font-mono font-black text-slate-800">{coverage?.latest_trade_date ?? '—'}</div>
          <div className="text-[11px] mt-1 flex items-center justify-center gap-1">
            {isUpToDate ? <><CheckCircle2 size={12} className="text-emerald-500"/> <span className="text-emerald-600 font-bold">已是最新</span></> : <><AlertTriangle size={12} className="text-amber-500"/> <span className="text-amber-600 font-bold">缺口 {gapCount} 日</span></>}
          </div>
        </Card>
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">操作</div>
          <div className="mt-2 flex flex-col gap-1.5 items-center">
            <Button type="primary" icon={<Zap size={14} />} onClick={handleBackfill} loading={backfilling} disabled={isUpToDate || backfilling} className="rounded-xl font-bold">
              一键补全至最新
            </Button>
            <Button icon={<RefreshCw size={12} />} onClick={() => void load()} size="small" className="rounded-full">刷新</Button>
          </div>
        </Card>
      </div>

      {backfilling && task && (
        <Card size="small" className="rounded-2xl">
          <div className="flex items-center gap-2 mb-2"><Clock size={12} className="text-blue-500"/><Text className="text-xs font-bold">补全进度</Text></div>
          <Progress percent={task.progress ?? 0} size="small" />
          {task.logs && <pre className="mt-2 p-2 bg-slate-900 text-slate-100 rounded-xl text-[10px] max-h-32 overflow-auto">{String(task.logs).slice(-2000)}</pre>}
        </Card>
      )}

      <Card size="small" className="rounded-2xl">
        <div className="flex items-center justify-between mb-3">
          <Text className="text-xs font-black text-slate-700 flex items-center gap-1.5"><Calendar size={12} className="text-slate-400"/>覆盖日历（6-8月）</Text>
          <Tag className="rounded-full">{gapCount} 个缺口日</Tag>
        </div>
        {(() => {
          const latest = coverage?.latest_trade_date ? String(coverage.latest_trade_date).slice(0, 10) : null;
          const maxDate = coverage?.max_date ? String(coverage.max_date).slice(0, 10) : null;
          const gapSet = new Set((coverage?.gap_dates || []).map((d: string) => String(d).slice(0, 10)));
          // 动态展示最近 3 个月（含最新交易月），如 8.30 则 6-8 月
          const baseDate = latest ? new Date(latest) : new Date();
          const months = [-2, -1, 0].map((off) => {
            const d = new Date(baseDate.getFullYear(), baseDate.getMonth() + off, 1);
            return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
          });
          const weekDays = ['一', '二', '三', '四', '五', '六', '日'];
          const isTradingDay = (d: string) => tradingMap[d] ?? false;
          return (
            <div className="space-y-4">
              <div className="flex gap-2 text-[10px] flex-wrap">
                <span className="flex items-center gap-1"><span className={`w-3 h-3 rounded ${estimated ? 'bg-sky-500/30 border border-sky-200' : 'bg-emerald-500/30 border border-emerald-200'}`} /> {estimated ? '可补全范围' : '已覆盖'}</span>
                <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-amber-400/30 border border-amber-200" /> 待补缺口</span>
                <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-slate-100 border border-slate-200" /> 未来</span>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                {months.map((ym) => {
                  const [y, m] = ym.split('-').map(Number);
                  const first = new Date(y, m - 1, 1);
                  const daysInMonth = new Date(y, m, 0).getDate();
                  const startWeek = (first.getDay() + 6) % 7; // 周一为 0
                  const cells: (string | null)[] = Array(startWeek).fill(null);
                  for (let d = 1; d <= daysInMonth; d++) cells.push(`${ym}-${String(d).padStart(2, '0')}`);
                  while (cells.length % 7 !== 0) cells.push(null);
                  return (
                    <div key={ym} className="border border-slate-100 rounded-xl p-2 bg-white">
                      <div className="text-xs font-black text-slate-700 text-center mb-1">{m}月</div>
                      <div className="grid grid-cols-7 gap-1 text-[10px] text-center text-slate-400 mb-1">
                        {weekDays.map((w) => <div key={w}>{w}</div>)}
                      </div>
                      <div className="grid grid-cols-7 gap-1">
                        {cells.map((d, i) => {
                          if (!d) return <div key={i} className="h-7" />;
                          const isFuture = latest ? d > latest : false;
                          const isGap = gapSet.has(d);
                          const isTrading = isTradingDay(d);
                          const isCovered = maxDate ? d <= maxDate && !isGap && !isFuture && isTrading : false;
                          const bg = isFuture
                            ? 'bg-slate-100 text-slate-400 border-slate-200'
                            : isGap
                              ? 'bg-amber-400/30 text-amber-700 border-amber-200 font-bold'
                              : isCovered
                                ? (estimated
                                    ? 'bg-sky-500/30 text-sky-700 border-sky-200'
                                    : 'bg-emerald-500/30 text-emerald-700 border-emerald-200')
                                : !isTrading
                                  ? 'bg-slate-50 text-slate-300 border-slate-100'
                                  : 'bg-white text-slate-500 border-slate-100';
                          return (
                            <div key={d} className={`h-7 flex items-center justify-center rounded-lg border text-[11px] ${bg}`}>
                              {Number(d.slice(8, 10))}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </div>
              {gapCount === 0 && <Empty description="已覆盖至最新交易日" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            </div>
          );
        })()}
      </Card>
    </div>
  );
};

export default InferenceCoveragePanel;
