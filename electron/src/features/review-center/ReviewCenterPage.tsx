import { useEffect, useMemo, useState } from 'react';
import { useSelector } from 'react-redux';
import ReactECharts from 'echarts-for-react';
import { Activity, ArrowUpRight, BarChart3, CircleHelp, Clock3, Layers3, ListFilter, Search, Star, TrendingUp } from 'lucide-react';
import { modelTrainingService, type UserModelRecord, type ReviewRun, type ReviewResult, type ReviewStock } from '../../services/modelTrainingService';
import { marketService, type MarketIndex } from '../../services/marketService';
import { StockMiniTerminalModal } from '../inference-center/components/StockMiniTerminalModal';
import { getLiveBenchmarkReturn } from './benchmarkQuote';
import './ReviewCenterPage.css';

const pct = (v: number | null | undefined) => v == null || !Number.isFinite(v) ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
const returnClass = (v: number | null | undefined) => v == null || !Number.isFinite(v) || v === 0 ? '' : v > 0 ? 'positive' : 'negative';
const price = (v: number | null | undefined) => v == null ? '—' : `¥${v.toFixed(2)}`;
const names: Record<string, string> = { '000300.SH': '沪深300', '000905.SH': '中证500', '000001.SH': '上证指数' };
const label = (model: UserModelRecord) => String(model.metadata_json?.display_name || model.model_id);
const dateLabel = (s: string) => s ? s.slice(5).replace('-', '月') + '日' : '—';

// 以北京时间的自然日计算，包含今天在内共 30 天。
const recentDateRange = () => {
  const parts = new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
  const get = (name: string) => Number(parts.find(part => part.type === name)?.value);
  const today = new Date(Date.UTC(get('year'), get('month') - 1, get('day')));
  const start = new Date(today.getTime() - 29 * 86400000);
  return { start: start.toISOString().slice(0, 10), end: today.toISOString().slice(0, 10) };
};
const recentRuns = (items: ReviewRun[]) => {
  const { start, end } = recentDateRange();
  return items.filter(item => item.data_trade_date >= start && item.data_trade_date <= end);
};
const hasRecentPage = (items: ReviewRun[], hasMore: boolean) => hasMore && (!items.length || items[items.length - 1].data_trade_date >= recentDateRange().start);

export default function ReviewCenterPage() {
  const user = useSelector((state: any) => state.auth.user);
  const identity = `${user?.tenant_id || ''}:${user?.user_id || user?.id || ''}`;
  const [models, setModels] = useState<UserModelRecord[]>([]);
  const [model, setModel] = useState('');
  const [batches, setBatches] = useState<ReviewRun[]>([]);
  const [batch, setBatch] = useState('');
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [topK, setTopK] = useState(30);
  const [excludeST, setExcludeST] = useState(true);
  const [period, setPeriod] = useState(0);
  const [benchmark, setBenchmark] = useState('000300.SH');
  const [tab, setTab] = useState<'stocks' | 'daily'>('stocks');
  const [batchTab, setBatchTab] = useState<'all' | 'favorites'>('all');
  const [favorites, setFavorites] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<ReviewResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [refreshError, setRefreshError] = useState(false);
  const [indexQuote, setIndexQuote] = useState<MarketIndex | null>(null);
  /** 成分股明细行 → K线 + 模型分数弹窗（与推理中心右侧股票池同一个模块） */
  const [miniStock, setMiniStock] = useState<ReviewStock | null>(null);

  useEffect(() => {
    let alive = true;
    setModels([]); setModel(''); setBatches([]); setBatch(''); setFavorites([]); setResult(null); setError('');
    modelTrainingService.listUserModels().then(r => { if (alive) { setModels(r.items); setModel(r.items[0]?.model_id || ''); } }).catch(() => { if (alive) setError('模型加载失败，请检查网络或登录状态'); });
    return () => { alive = false; };
  }, [identity]);
  useEffect(() => {
    let alive = true;
    setBatches([]); setBatch(''); setPage(1); setHasMore(false); setResult(null);
    if (model) modelTrainingService.listReviewRuns(model).then(r => { if (alive) { const recent = recentRuns(r.items); setBatches(recent); setBatch(recent[0]?.run_id || ''); setHasMore(hasRecentPage(r.items, r.has_more)); } }).catch(() => { if (alive) setError('推理批次加载失败'); });
    return () => { alive = false; };
  }, [model, identity]);
  useEffect(() => {
    let alive = true;
    let inFlight = false;
    setResult(null); setError(''); setRefreshError(false);
    if (!batch) { setLoading(false); return () => { alive = false; }; }
    const load = async (background: boolean) => {
      if (inFlight) return;
      inFlight = true;
      if (!background) setLoading(true);
      try {
        const data = await modelTrainingService.getInferenceReview(batch, topK, excludeST, benchmark);
        if (alive) { setResult(data); setError(''); setRefreshError(false); }
      } catch {
        if (alive) {
          if (background) setRefreshError(true);
          else setError('复盘数据加载失败，请稍后重试');
        }
      } finally {
        inFlight = false;
        if (alive && !background) setLoading(false);
      }
    };
    void load(false);
    const timer = window.setInterval(() => {
      if (!document.hidden) void load(true);
    }, 60000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [batch, topK, excludeST, benchmark, refresh, identity]);
  useEffect(() => {
    let alive = true;
    let pending = false;
    setIndexQuote(null);
    if (!batch) return () => { alive = false; };
    const loadIndex = async () => {
      if (pending) return;
      pending = true;
      try {
        const response = await marketService.getTencentMarketData();
        if (alive) setIndexQuote(response.success ? response.data?.indices.find(q => q.symbol.toUpperCase() === `${benchmark.slice(-2)}${benchmark.slice(0, 6)}`) || null : null);
      } catch { if (alive) setIndexQuote(null); } finally { pending = false; }
    };
    void loadIndex();
    const timer = window.setInterval(() => { if (!document.hidden) void loadIndex(); }, 60000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [benchmark, batch, identity]);
  const loadMore = async () => { try { const r = await modelTrainingService.listReviewRuns(model, page + 1); setBatches([...batches, ...recentRuns(r.items)]); setPage(page + 1); setHasMore(hasRecentPage(r.items, r.has_more)); } catch { setError('加载更多批次失败'); } };
  const visibleBatches = recentRuns(batches).filter(item => batchTab === 'all' || favorites.includes(item.run_id));
  const selectedBatch = batches.find(item => item.run_id === batch);
  const currentModel = models.find(item => item.model_id === model);
  const stocks = result?.stocks || [];
  const visibleStocks = stocks.filter(s => `${s.name}${s.code}${s.industry}`.toLowerCase().includes(query.toLowerCase()));
  const originalDays = result?.days || [];
  const lastDay = originalDays.at(-1);
  const liveBenchmark = lastDay && lastDay.source !== '日线' && lastDay.benchmark == null
    ? getLiveBenchmarkReturn(indexQuote, benchmark, selectedBatch?.prediction_trade_date || '', lastDay.date, result?.benchmark_start)
    : null;
  const days = liveBenchmark == null ? originalDays : originalDays.map((day, index) => index === originalDays.length - 1 ? { ...day, benchmark: liveBenchmark } : day);
  const chartDays = period ? days.slice(-period) : days;
  const latest = days.at(-1);
  const previous = days.at(-2);
  const dailyReturn = latest?.cumulative != null ? (previous?.cumulative != null ? ((1 + latest.cumulative / 100) / (1 + previous.cumulative / 100) - 1) * 100 : days.length === 1 ? latest.cumulative : null) : null;
  const rising = stocks.filter(s => s.daily != null && s.daily > 0).length;
  const falling = stocks.filter(s => s.daily != null && s.daily < 0).length;
  const flat = stocks.filter(s => s.daily === 0).length;
  const dailyCoverage = rising + falling + flat;
  const missingDaily = stocks.length - dailyCoverage;
  const excess = latest?.cumulative != null && latest?.benchmark != null ? latest.cumulative - latest.benchmark : null;
  const chart = useMemo(() => ({
    color: ['#426cf5', '#b2bdd2'], tooltip: {trigger:'axis', formatter: (items: Array<{axisValue: string; seriesName: string; marker: string; value: number | null}>) => { const day = String(items[0]?.axisValue || '').replace(/[^0-9-]/g, ''); return [day, ...items.map(item => { const name = item.seriesName === '组合收益' ? '组合收益' : names[benchmark]; const value = typeof item.value === 'number' && Number.isFinite(item.value) ? item.value : null; const color = value == null || value === 0 ? '#64748b' : value > 0 ? '#eb5265' : '#16a576'; return `${item.marker}${name}　<span style="color:${color};font-weight:600">${pct(value)}</span>`; })].join('<br/>'); }}, legend: {top:0,right:0,textStyle:{color:'#8190a9',fontSize:11}},
    grid:{left:50,right:20,top:40,bottom:28}, xAxis:{type:'category',boundaryGap:false,data:chartDays.map(d => d.date.slice(5)),axisLabel:{color:'#9aa8bd',fontSize:10}},
    yAxis:{type:'value',axisLabel:{color:'#9aa8bd',fontSize:10,formatter:'{value}%'},splitLine:{lineStyle:{color:'#edf1f7',type:'dashed'}}},
    series:[{name:'组合收益',type:'line',smooth:false,data:chartDays.map(d => d.cumulative),connectNulls:false,lineStyle:{width:3},areaStyle:{color:'rgba(77,114,245,.10)'}},{name:names[benchmark],type:'line',smooth:false,data:chartDays.map(d => d.benchmark),connectNulls:false,lineStyle:{width:2,type:'dashed'}}],
  }), [chartDays, benchmark]);
  return <div className="review-page"><div className="review-shell"><header className="review-header"><div className="review-brand"><span className="review-brand-icon"><BarChart3 size={20}/></span><div><h1>模型复盘 <span>REVIEW CENTER</span></h1><p>追踪每一次推理的事后表现</p></div></div><div className="review-demo"><span>事后观察 · 非实际交易收益</span></div></header>
    <div className="review-layout"><aside className="review-sidebar"><div className="review-side-heading"><div><h2>推理组合</h2><p>最近 30 天的推理批次</p></div><span>{batches.length}</span></div><label className="review-label" htmlFor="review-model">选择模型</label><select id="review-model" value={model} onChange={e => setModel(e.target.value)}><option value="" disabled>选择模型</option>{models.map(m => <option key={m.model_id} value={m.model_id}>{label(m)}</option>)}</select><div className="review-list-heading review-batch-tabs" role="tablist" aria-label="推理批次列表"><button role="tab" aria-selected={batchTab === 'all'} className={batchTab === 'all' ? 'active' : ''} onClick={() => setBatchTab('all')}>推理批次</button><button role="tab" aria-selected={batchTab === 'favorites'} className={batchTab === 'favorites' ? 'active' : ''} onClick={() => setBatchTab('favorites')}>个人收藏 <span>{batches.filter(b => favorites.includes(b.run_id)).length}</span></button></div><div className="review-batches">{visibleBatches.length === 0 && <div className="review-favorites-empty"><Star size={20}/><span>{batchTab === 'favorites' ? '暂无收藏批次' : '最近 30 天暂无已完成的推理批次'}</span></div>}{visibleBatches.map(item => { const favorite = favorites.includes(item.run_id); return <div className="review-batch-wrap" key={item.run_id}><button className={`review-batch ${batch === item.run_id ? 'selected' : ''}`} onClick={() => setBatch(item.run_id)}><div><strong>{dateLabel(item.data_trade_date)}</strong><span className="review-batch-tag">推理基准日</span></div><p>目标日 {item.prediction_trade_date}</p><div className="review-batch-bottom"><small title={item.run_id}>{item.run_id.slice(0, 16)}</small></div></button><button className={`review-favorite ${favorite ? 'is-favorite' : ''}`} onClick={() => setFavorites(favorite ? favorites.filter(id => id !== item.run_id) : [...favorites, item.run_id])} aria-label={`${favorite ? '取消收藏' : '收藏'} ${dateLabel(item.data_trade_date)}推理批次`} aria-pressed={favorite}><Star size={15} fill={favorite ? 'currentColor' : 'none'}/></button></div>})}{hasMore && batchTab === 'all' && <button className="review-more" onClick={() => void loadMore()}>加载更多</button>}</div><div className="review-side-note"><CircleHelp size={15}/><span>收藏暂保留在当前页面会话；收益以目标日开盘为观察起点。</span></div></aside>
    <main className="review-main"><div className="review-title"><div><div className="review-eyebrow"><span/> MODEL PERFORMANCE</div><h2>{selectedBatch ? `${dateLabel(selectedBatch.data_trade_date)}推理复盘` : '选择推理批次'}</h2><p>{currentModel ? label(currentModel) : '暂无模型'} <i/> 基准日 {selectedBatch?.data_trade_date || '—'} <i/> 目标日 {selectedBatch?.prediction_trade_date || '—'}</p></div><div className="review-time"><Clock3 size={14}/> {result?.quote_time ? `行情时间 ${new Date(result.quote_time).toLocaleString('zh-CN')}` : '暂无当日行情'}{refreshError ? ' · 更新失败，保留上次数据' : ''}</div></div>
    <section className="review-controls"><div><span className="review-control-label">组合规模</span><div className="review-segment">{[10,30,50].map(n => <button key={n} className={topK === n ? 'active' : ''} onClick={() => setTopK(n)}>Top {n}</button>)}</div></div><div className="review-control-divider"/><div><span className="review-control-label">股票过滤</span><button className={`review-toggle ${excludeST ? 'on' : ''}`} onClick={() => setExcludeST(!excludeST)} role="switch" aria-checked={excludeST}><span/>排除 ST</button></div><div className="review-control-divider"/><div><span className="review-control-label">对比基准</span><select value={benchmark} onChange={e => setBenchmark(e.target.value)} aria-label="对比基准">{Object.entries(names).map(([id,name]) => <option value={id} key={id}>{name}</option>)}</select></div><div className="review-control-hint"><ListFilter size={14}/> 等权持有 · 事后观察</div></section>
    {error && <div className="review-empty" role="alert">{error} <button onClick={() => setRefresh(refresh + 1)}>重试</button></div>}{loading && <div className="review-empty">正在加载复盘数据…</div>}{result && <><div className="review-metrics"><div className="review-metric primary"><span>组合累计收益 <TrendingUp size={15}/></span><strong className={returnClass(latest?.cumulative)}>{pct(latest?.cumulative)}</strong><small>目标日开盘起算</small><Activity className="review-metric-art" size={80}/></div><div className="review-metric"><span>最新日收益 <ArrowUpRight size={14}/></span><strong className={returnClass(dailyReturn)}>{pct(dailyReturn)}</strong><small>{latest?.date || '暂无数据'} · {latest?.source || '—'}</small></div><div className="review-metric"><span>相对基准 <ArrowUpRight size={14}/></span><strong className={returnClass(excess)}>{pct(excess)}</strong><small>对比 {names[benchmark]}</small></div><div className="review-metric"><span>上涨 / 下跌 <Layers3 size={14}/></span><strong><span className="positive">{dailyCoverage ? rising : '—'}</span> <em>/</em> <span className="negative">{dailyCoverage ? falling : '—'}</span></strong><small>平盘 {flat} · 可统计 {dailyCoverage}/{stocks.length}{missingDaily > 0 ? ` · 缺失 ${missingDaily}` : ''}</small></div></div>
    <section className="review-card review-chart-card"><div className="review-card-title"><div><h3>收益走势 <span>PERFORMANCE</span></h3><p>目标日开盘起算{liveBenchmark != null ? ` · 腾讯财经 ${indexQuote?.timestamp?.replace('T', ' ') || ''}` : ' · 当日指数行情不可用时留空'}</p></div><div className="review-period">{[{n:5,label:'近5日'},{n:0,label:'全部'}].map(x => <button className={period === x.n ? 'active' : ''} key={x.n} onClick={() => setPeriod(x.n)}>{x.label}</button>)}</div></div>{days.length ? <ReactECharts option={chart} notMerge style={{height:225,width:'100%'}}/> : <div className="review-empty">暂无可用行情</div>}</section>
    <section className="review-card review-table-card"><div className="review-table-header"><div className="review-tabs"><button className={tab === 'stocks' ? 'active' : ''} onClick={() => setTab('stocks')}>成分股明细 <span>{stocks.length}</span></button><button className={tab === 'daily' ? 'active' : ''} onClick={() => setTab('daily')}>每日表现</button></div>{tab === 'stocks' && <label className="review-search"><Search size={14}/><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索股票或行业"/></label>}</div><div className="review-table-scroll">{tab === 'stocks' ? <table><thead><tr><th>排名</th><th>股票</th><th>所属行业</th><th>模型分数</th><th>起始价（前复权）</th><th>当日涨跌</th><th>累计收益</th><th>行情状态</th></tr></thead><tbody>{visibleStocks.map(s => <tr key={s.code} className="review-stock-row" tabIndex={0} title="点击查看 K 线与模型分数" aria-label={`查看 ${s.name} ${s.code} 的 K线与模型分数`} onClick={() => setMiniStock(s)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setMiniStock(s); } }}><td><span className="review-rank">{s.rank}</span></td><td><strong>{s.name}</strong><small>{s.code}</small></td><td>{s.industry}</td><td className="review-mono">{s.score == null ? '—' : Number(s.score).toFixed(2)}</td><td>{price(s.start)}</td><td className={returnClass(s.daily)}>{pct(s.daily)}</td><td className={returnClass(s.cumulative)}>{pct(s.cumulative)}</td><td><span className="review-status">{s.source}</span></td></tr>)}</tbody></table> : <table><thead><tr><th>交易日期</th><th>组合日收益</th><th>累计收益</th><th>基准累计收益</th><th>超额收益</th><th>覆盖</th></tr></thead><tbody>{days.map((d,i) => {const prev = days[i-1]?.cumulative; const change = d.cumulative != null ? (prev != null ? ((1+d.cumulative/100)/(1+prev/100)-1)*100 : i === 0 ? d.cumulative : null) : null; return <tr key={d.date}><td>{d.date} · {d.source}</td><td className={returnClass(change)}>{pct(change)}</td><td className={returnClass(d.cumulative)}>{pct(d.cumulative)}</td><td className={returnClass(d.benchmark)}>{pct(d.benchmark)}</td><td className={returnClass(d.cumulative != null && d.benchmark != null ? d.cumulative-d.benchmark : null)}>{pct(d.cumulative != null && d.benchmark != null ? d.cumulative-d.benchmark : null)}</td><td>{d.coverage}/{stocks.length}</td></tr>}).reverse()}</tbody></table>}</div><div className="review-table-footer"><span><CircleHelp size={13}/> {result.message} {result.unknown_st > 0 && `ST 状态未知，已跳过 ${result.unknown_st} 只。`}</span></div></section></>}</main></div><StockMiniTerminalModal open={!!miniStock} onClose={() => setMiniStock(null)} symbol={miniStock?.code ?? ''} name={miniStock?.name} modelId={model || undefined} asOfDate={selectedBatch?.data_trade_date} /></div></div>;
}
