/** 行情回测工作台的控制器：左栏策略选择 / 中栏标的与K线 / 右栏运行与源码，
 *  三栏共用这一份状态，避免像旧版那样两个 tab 各存一套、结果对不上。
 *
 * 两条回测链路（内置模板同步返回 / Pine 库异步 job）在这里收敛成同一个 `LabResult`。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import {
  applyChatCandidate,
  createPineItem,
  deletePineItem,
  fetchChatJob,
  fetchChatThread,
  fetchLabKlines,
  fetchLabStrategies,
  fetchMarketMeta,
  fetchNote,
  fetchNoteIds,
  fetchPineJob,
  fetchPineList,
  fetchPineSource,
  fetchPineTranspile,
  fetchPineVersions,
  resetPineSource,
  revertPineSource,
  runLabBacktest,
  runPineBacktest,
  saveNote as putNote,
  savePineSource,
  searchLabSymbols,
  sendChat as postChat,
  type AdjMode,
  type BtResult,
  type ChatJob,
  type ChatMessage,
  type ChatMode,
  type Freq,
  type Kline,
  type LabMarket,
  type LabStrategy,
  type LabSymbol,
  type MarketInfo,
  type NoteDoc,
  type NoteInput,
  type PineJob,
  type PineItem,
  type PineList,
  type PineListItem,
  type PineReport,
  type PineTranspile,
  type VersionRow,
} from './api';
import { fromPineReport, fromTemplate, type LabResult } from './labResult';
import { errText } from './format';
import { FREQ_NAME, MARKET_NAME } from './markets';
import { asUpdater } from './reactCompat';

const PAGE = 200;
const POLL_MS = 2500;

export type Selection = { kind: 'template' | 'pine'; id: string };

export interface Workbench {
  // 市场
  market: LabMarket;
  marketInfo: MarketInfo | null;
  /** 该市场真实可用的复权档 */
  adjs: AdjMode[];
  /** 界面可选的周期（三档都列；无数据的档点击后回退日线） */
  freqs: Freq[];
  /** 该市场真实有落盘的周期 */
  availFreqs: Freq[];
  /** 当前 K 线周期（默认日线） */
  freq: Freq;
  setFreq: (f: Freq) => void;
  /** 选到无数据周期时的回退提示 */
  freqNote: string;
  /** 本次回测实际使用的复权口径（分钟线恒为不复权） */
  runAdj: AdjMode;
  /** 本次回测口径（有后复权用后复权，否则该市场第一档） */
  backtestAdj: AdjMode;

  // 标的与图
  symbol: string;
  adj: AdjMode;
  setAdj: (a: AdjMode) => void;
  stockName: string;
  bars: Kline[];
  barsBusy: boolean;
  fullSpan: boolean;
  toggleSpan: () => void;
  showMarkers: boolean;
  toggleMarkers: () => void;
  /** 回测捕获到的指标是否画在 K 线上（叠加线 + 副图） */
  showIndicators: boolean;
  toggleIndicators: () => void;
  query: string;
  hits: LabSymbol[];
  showHits: boolean;
  setShowHits: (v: boolean) => void;
  onQuery: (v: string) => void;
  pickSymbol: (s: LabSymbol) => void;
  /** 把标的/复权切回结果所属的那份（成交点与K线不同标的时用） */
  syncToResult: () => void;

  // 内置模板
  strategies: LabStrategy[];
  params: Record<string, number>;
  setParams: Dispatch<SetStateAction<Record<string, number>>>;

  // Pine 策略库
  category: string;
  setCategory: (c: string) => void;
  q: string;
  setQ: (v: string) => void;
  list: PineList | null;
  listBusy: boolean;
  listErr: string;
  loadMore: () => void;
  transpile: Record<string, PineTranspile>;
  cur: PineListItem | null;
  /** 新增自定义策略（成功后刷新列表并选中） */
  createPine: (payload: { title: string; category?: string; source: string }) => Promise<boolean>;
  createBusy: boolean;
  createErr: string;
  /** 删除当前自定义策略（仅 source_kind=custom） */
  deletePine: () => Promise<boolean>;
  deleteBusy: boolean;
  deleteErr: string;

  // 选择与运行
  sel: Selection | null;
  selectTemplate: (id: string) => void;
  selectPine: (id: string) => void;
  run: () => void;
  runBusy: boolean;
  running: boolean;
  job: PineJob | null;
  err: string;

  // 结果
  result: LabResult | null;

  // 备注
  note: NoteDoc | null;
  noteBusy: boolean;
  noteMsg: string;
  noteErr: string;
  saveNote: (patch: NoteInput) => void;
  noteIds: Set<string>;

  // 对话
  chat: ChatMessage[];
  /** 已发出、worker 还没落进线程的那条（乐观显示，避免点完没反应） */
  chatPending: ChatMessage | null;
  /** 队列/模型阶段标签；空闲时为空串 */
  chatStage: string;
  chatBusy: boolean;
  chatErr: string;
  sendChat: (message: string) => void;
  /** ask = 问答；edit = 让 agent 产出改过的候选 */
  chatMode: ChatMode;
  setChatMode: (m: ChatMode) => void;
  /** 当前任务（含 edit 模式的候选与前后对比） */
  chatJob: ChatJob | null;
  /** 把一条回答追加进备注（by=agent） */
  appendNote: (text: string) => void;

  // 候选采用 / 回滚
  applyBusy: boolean;
  applyMsg: string;
  applyErr: string;
  applyCandidate: () => void;
  revert: () => void;
  /** 可回滚的版本快照（每次采用/回滚各留一份） */
  versions: VersionRow[];

  // 源码编辑
  draft: string;
  setDraft: (s: string) => void;
  dirty: boolean;
  saving: boolean;
  saveMsg: string;
  saveErr: string;
  save: () => void;
  reset: () => void;
}

/** 标的由页面持有：选股页点一行也能直接跳到工作台看这只票。 */
export function useWorkbench(
  market: LabMarket,
  symbol: string,
  setSymbol: (code: string) => void,
): Workbench {
  // ---- 市场 ----
  const [marketInfo, setMarketInfo] = useState<MarketInfo | null>(null);
  const adjs = useMemo<AdjMode[]>(
    () => marketInfo?.adjs ?? (market === 'cn'
      ? ['unadjusted', 'forward', 'backward']
      : market === 'hk' ? ['unadjusted', 'backward'] : ['unadjusted']),
    [marketInfo, market],
  );
  /** 回测口径：有后复权用后复权，否则用该市场第一档（美股/期货/加密只有原始价）。 */
  const backtestAdj: AdjMode = adjs.includes('backward') ? 'backward' : (adjs[0] ?? 'unadjusted');
  /** 该市场可选的 K 线周期（三档都列；无数据的档由 chooseFreq 回退） */
  const freqs = useMemo<Freq[]>(
    () => marketInfo?.freqs ?? ['1d', '5m', '1m'],
    [marketInfo],
  );
  const availFreqs = useMemo<Freq[]>(
    () => marketInfo?.avail_freqs ?? ['1d'],
    [marketInfo],
  );
  const [freq, setFreq] = useState<Freq>('1d');
  const [freqNote, setFreqNote] = useState('');
  /** 回测复权：分钟线只有不复权；日线按市场能力选后复权/第一档 */
  const runAdj: AdjMode = freq === '1d' ? backtestAdj : 'unadjusted';

  // ---- 标的与K线 ----
  const [adj, setAdj] = useState<AdjMode>('unadjusted');
  const [stockName, setStockName] = useState('');
  const [bars, setBars] = useState<Kline[]>([]);
  const [barsBusy, setBarsBusy] = useState(false);
  /** 看盘默认近 600 根；跑完回测自动切全历史，否则 10 年回测的进出场箭头大多落在图外 */
  const [fullSpan, setFullSpan] = useState(false);
  const [showMarkers, setShowMarkers] = useState(true);
  const [showIndicators, setShowIndicators] = useState(true);

  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<LabSymbol[]>([]);
  const [showHits, setShowHits] = useState(false);
  const timer = useRef<number | null>(null);
  /** K线请求序号：快速切标的/复权时，旧请求晚到不许覆盖新数据 */
  const barsSeq = useRef(0);

  // 市场元数据（可用复权档）。失败时退回前端静态表，不阻塞页面。
  useEffect(() => {
    let alive = true;
    fetchMarketMeta()
      .then((list) => {
        if (alive) setMarketInfo(list.find((m) => m.id === market) ?? null);
      })
      .catch(() => {
        if (alive) setMarketInfo(null);
      });
    return () => { alive = false; };
  }, [market]);

  // 切市场后若当前复权档不存在（如 A股后复权 → 美股只有原始价），落到第一档
  useEffect(() => {
    if (!adjs.includes(adj)) setAdj(adjs[0] ?? 'unadjusted');
  }, [adjs, adj]);

  // 切市场后周期在該市场没有数据 → 自动回日线（不把按钮藏起来）
  useEffect(() => {
    if (!availFreqs.includes(freq)) {
      setFreq('1d');
      setFreqNote(`${MARKET_NAME[market]}暂无「${FREQ_NAME[freq]}」数据，已按日线显示`);
    }
  }, [availFreqs, freq, market]);

  /** 界面点击周期：无数据档自动回退日线并给出提示 */
  const chooseFreq = (f: Freq) => {
    if (f !== '1d' && !availFreqs.includes(f)) {
      setFreqNote(`该市场暂无「${FREQ_NAME[f]}」数据，已自动按日线显示`);
      setFreq('1d');
      return;
    }
    setFreqNote('');
    setFreq(f);
  };

  // ---- 选择与结果 ----
  const [sel, setSel] = useState<Selection | null>(null);
  const [err, setErr] = useState('');

  // ---- 内置模板 ----
  const [strategies, setStrategies] = useState<LabStrategy[]>([]);
  const [params, setParams] = useState<Record<string, number>>({});
  const [bt, setBt] = useState<BtResult | null>(null);
  const [btBusy, setBtBusy] = useState(false);

  // ---- Pine 策略库 ----
  const [category, setCategory] = useState('');
  const [q, setQ] = useState('');
  const [list, setList] = useState<PineList | null>(null);
  const [listBusy, setListBusy] = useState(false);
  const [listErr, setListErr] = useState('');
  const [transpile, setTranspile] = useState<Record<string, PineTranspile>>({});
  const seq = useRef(0);

  const [cur, setCur] = useState<PineListItem | null>(null);
  const [draft, setDraft] = useState('');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState('');
  const [saveErr, setSaveErr] = useState('');

  const [job, setJob] = useState<PineJob | null>(null);
  const [report, setReport] = useState<PineReport | null>(null);
  const [queuing, setQueuing] = useState(false);

  // ---- 备注 ----
  const [note, setNote] = useState<NoteDoc | null>(null);
  const [noteBusy, setNoteBusy] = useState(false);
  const [noteMsg, setNoteMsg] = useState('');
  const [noteErr, setNoteErr] = useState('');
  const [noteIds, setNoteIds] = useState<Set<string>>(new Set());

  const refreshNoteIds = useCallback(() => {
    fetchNoteIds()
      .then((d) => setNoteIds(new Set(d.ids)))
      .catch(() => setNoteIds(new Set()));
  }, []);

  useEffect(() => {
    refreshNoteIds();
  }, [refreshNoteIds]);

  useEffect(() => {
    if (!sel) return;
    setNoteMsg('');
    setNoteErr('');
    setNote(null);
    fetchNote(sel.id)
      .then(setNote)
      .catch(() => setNote(null));
  }, [sel]);

  const saveNote = (patch: NoteInput) => {
    if (!sel) return;
    setNoteBusy(true);
    setNoteMsg('');
    setNoteErr('');
    putNote(sel.id, patch)
      .then((doc) => {
        setNote(doc);
        setNoteMsg('已保存');
        refreshNoteIds();
      })
      .catch((e) => setNoteErr(errText(e) || '备注保存失败'))
      .finally(() => setNoteBusy(false));
  };

  // ---- 对话 ----
  const [chat, setChat] = useState<ChatMessage[]>([]);
  const [chatPending, setChatPending] = useState<ChatMessage | null>(null);
  const [chatJob, setChatJob] = useState<ChatJob | null>(null);
  const [chatSending, setChatSending] = useState(false);
  const [chatErr, setChatErr] = useState('');
  const [chatMode, setChatMode] = useState<ChatMode>('ask');
  const [versions, setVersions] = useState<VersionRow[]>([]);
  const [applyBusy, setApplyBusy] = useState(false);
  const [applyMsg, setApplyMsg] = useState('');
  const [applyErr, setApplyErr] = useState('');

  const loadChat = useCallback((id: string) => {
    fetchChatThread(id)
      .then((d) => setChat(d.messages))
      .catch(() => setChat([]));
  }, []);

  const refreshVersions = useCallback((id: string) => {
    fetchPineVersions(id)
      .then((d) => setVersions(d.versions))
      .catch(() => setVersions([]));
  }, []);

  useEffect(() => {
    setChat([]);
    setChatPending(null);
    setChatJob(null);
    setChatSending(false);
    setChatErr('');
    setVersions([]);
    setApplyMsg('');
    setApplyErr('');
    if (sel?.kind !== 'pine') return;      // 对话只针对策略库条目（模板不在库里）
    loadChat(sel.id);
    refreshVersions(sel.id);
  }, [sel, loadChat, refreshVersions]);

  // staged = 候选已过沙箱回测，但 worker 还要落对比 → 不是终态
  const chatRunning =
    chatJob?.status === 'queued' || chatJob?.status === 'running' || chatJob?.status === 'staged';
  const chatJobId = chatJob?.job_id ?? '';

  // 轮询任务：worker 由 cron 每分钟起一次，答完才写 done
  useEffect(() => {
    if (!sel || !chatJobId || !chatRunning) return;
    const itemId = sel.id;
    const tick = () => {
      fetchChatJob(itemId, chatJobId)
        .then((j) => {
          setChatJob(j);
          if (j.status === 'queued' || j.status === 'running' || j.status === 'staged') return;
          setChatPending(null);
          setChatSending(false);
          if (j.status === 'done') loadChat(itemId);
          else setChatErr(j.error || '回答失败');
        })
        .catch(() => undefined);           // 单次轮询失败不打断，下一拍再来
    };
    const t = window.setInterval(tick, POLL_MS);
    return () => window.clearInterval(t);
  }, [sel, chatJobId, chatRunning, loadChat]);

  const sendChat = (message: string) => {
    const text = message.trim();
    if (!sel || sel.kind !== 'pine' || !text || chatSending) return;
    setChatErr('');
    setApplyMsg('');
    setApplyErr('');
    setChatSending(true);
    setChatPending({ role: 'user', content: text, pending: true });
    postChat(sel.id, text, chatMode)
      .then((r) => setChatJob({ job_id: r.job_id, status: 'queued', stage: 'queued' }))
      .catch((e) => {
        setChatPending(null);
        setChatSending(false);
        setChatErr(errText(e) || '发送失败');
      });
  };

  /** 采用/回滚后：源码与任务都变了，重载这两样（不动 sel，免得清掉对话与对比卡） */
  const reloadSource = (id: string) => {
    fetchPineSource(id)
      .then((d) => {
        setCur(d);
        setDraft(d.source ?? '');
        setDirty(false);
      })
      .catch(() => setSaveErr('源码读取失败'));
    loadJob(id);
  };

  const applyCandidate = () => {
    if (!sel || sel.kind !== 'pine' || !chatJob?.job_id || !chatJob.candidate) return;
    setApplyBusy(true);
    setApplyMsg('');
    setApplyErr('');
    applyChatCandidate(sel.id, chatJob.job_id, symbol, adj, market, freq)
      .then((r) => {
        setApplyMsg(r.queued
          ? '已采用，正在重跑转写 + 回测…'
          : `已采用；未自动重跑（${r.queue_error}）`);
        reloadSource(sel.id);
        refreshVersions(sel.id);
        refreshTranspile();
      })
      .catch((e) => setApplyErr(errText(e) || '采用失败'))
      .finally(() => setApplyBusy(false));
  };

  const revert = () => {
    if (!sel || sel.kind !== 'pine' || !versions.length) return;
    setApplyBusy(true);
    setApplyMsg('');
    setApplyErr('');
    revertPineSource(sel.id)
      .then((r) => {
        setApplyMsg(r.queued
          ? `已回滚到 ${r.reverted}，正在重跑…`
          : `已回滚到 ${r.reverted}；未自动重跑（${r.queue_error}）`);
        reloadSource(sel.id);
        refreshVersions(sel.id);
        refreshTranspile();
      })
      .catch((e) => setApplyErr(errText(e) || '回滚失败'))
      .finally(() => setApplyBusy(false));
  };

  /** 把 agent 的回答沉淀进备注（人工写的部分不动，只往后追加） */
  const appendNote = (text: string) => {
    if (!sel || sel.kind !== 'pine' || !text.trim()) return;
    const base = (note?.note ?? '').trim();
    saveNote({ note: base ? `${base}\n\n${text.trim()}` : text.trim(), by: 'agent' });
  };

  // ---- 内置模板列表 ----
  useEffect(() => {
    fetchLabStrategies()
      .then((list2) => {
        setStrategies(list2);
        if (!list2.length) return;
        setSel(asUpdater((curSel) => curSel ?? { kind: 'template', id: list2[0].id }));
        setParams(asUpdater((prev) =>
          Object.keys(prev).length
            ? prev
            : Object.fromEntries(list2[0].params.map((p) => [p.k, p.default])),
        ));
      })
      .catch((e) => setErr(`策略列表加载失败（${errText(e)}）`));
  }, []);

  // ---- K线 ----
  const loadBars = useCallback(async (sym: string, mode: AdjMode, full: boolean) => {
    const mySeq = ++barsSeq.current;
    setBarsBusy(true);
    try {
      const r = await fetchLabKlines(sym, mode, full ? 3000 : 600, market, freq);
      if (mySeq !== barsSeq.current) return;   // 已有更新的请求在飞
      setBars(r.bars);
      setStockName(r.name);
      // 只清掉上一次的「K线加载失败」，别顺手抹掉回测/任务错误
      setErr(asUpdater((cur: string) => (cur.startsWith('K线加载失败') ? '' : cur)));
    } catch (e) {
      if (mySeq !== barsSeq.current) return;
      setBars([]);
      setErr(`K线加载失败：${sym}（${errText(e)}）`);
    } finally {
      if (mySeq === barsSeq.current) setBarsBusy(false);
    }
  }, [market, freq]);

  useEffect(() => {
    if (symbol) void loadBars(symbol, adj, fullSpan);
  }, [symbol, adj, fullSpan, market, freq, loadBars]);

  // 卸载时收掉防抖定时器（否则 setHits 会落在已卸载组件上）
  useEffect(() => () => {
    if (timer.current) window.clearTimeout(timer.current);
  }, []);

  // ---- 标的搜索（防抖） ----
  const onQuery = (v: string) => {
    setQuery(v);
    if (timer.current) window.clearTimeout(timer.current);
    if (!v.trim()) {
      setHits([]);
      setShowHits(false);
      return;
    }
    timer.current = window.setTimeout(() => {
      searchLabSymbols(v, 20, market)
        .then((rows) => {
          setHits(rows);
          setShowHits(true);
        })
        .catch(() => setHits([]));
    }, 250);
  };

  const pickSymbol = (s: LabSymbol) => {
    setSymbol(s.code);
    setQuery('');
    setHits([]);
    setShowHits(false);
    setBt(null);
  };

  // ---- Pine 列表 ----
  const load = useCallback((cat: string, query: string, offset = 0) => {
    setListBusy(true);
    setListErr('');
    const mySeq = ++seq.current;
    fetchPineList(cat, query, PAGE, offset)
      .then((d) => {
        if (mySeq !== seq.current) return;
        setList(asUpdater((prev) => (offset > 0 && prev ? { ...d, items: [...prev.items, ...d.items] } : d)));
      })
      .catch(() => {
        if (mySeq !== seq.current) return;
        setListErr('策略库读取失败（索引还没生成？跑 scripts/pine_library_index.py）');
      })
      .finally(() => {
        if (mySeq === seq.current) setListBusy(false);
      });
  }, []);

  useEffect(() => {
    const t = setTimeout(() => load(category, q), q ? 300 : 0);
    return () => clearTimeout(t);
  }, [category, q, load]);

  const refreshTranspile = useCallback(() => {
    fetchPineTranspile()
      .then(setTranspile)
      .catch(() => setTranspile({}));
  }, []);

  useEffect(() => {
    refreshTranspile();
  }, [refreshTranspile]);

  const loadJob = useCallback((id: string) => {
    // 先按当前市场/标的/复权/周期取那一份；没有该键的报告再回退「最新」，
    // 保证切市场/周期后能看到该市场跑过的那份，也不丢「该策略上次跑过」的线索。
    fetchPineJob(id, { market, symbol, adj, freq })
      .then((d) => {
        if (d.report) {
          setJob(d.job);
          setReport(d.report);
          return;
        }
        return fetchPineJob(id).then((latest) => {
          setJob(latest.job);
          setReport(latest.report);
        });
      })
      .catch(() => undefined);
  }, [market, symbol, adj, freq]);

  // ---- 选中 ----
  const selectTemplate = (id: string) => {
    setSel({ kind: 'template', id });
    setParams(
      Object.fromEntries(
        (strategies.find((s) => s.id === id)?.params ?? []).map((p) => [p.k, p.default]),
      ),
    );
    setBt(null);
    setCur(null);
    setJob(null);
    setReport(null);
    setErr('');
  };

  const selectPine = (id: string) => {
    setSel({ kind: 'pine', id });
    setBt(null);
    setJob(null);
    setReport(null);
    setErr('');
    setSaveMsg('');
    setSaveErr('');
    setDirty(false);
    reloadSource(id);
  };

  // ---- 新增 / 删除自定义策略 ----
  const [createBusy, setCreateBusy] = useState(false);
  const [createErr, setCreateErr] = useState('');
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteErr, setDeleteErr] = useState('');

  const createPine = async (payload: { title: string; category?: string; source: string }) => {
    setCreateBusy(true);
    setCreateErr('');
    try {
      const item: PineItem = await createPineItem(payload);
      // 乐观插到列表顶部（顺序与后端 custom-first 一致），不为一新增重拉 1001 条
      setList(asUpdater((prev) => prev ? {
        ...prev,
        items: [item, ...prev.items],
        total: prev.total + 1,
        filtered: prev.filtered + 1,
        categories: prev.categories.some((c) => c.name === item.category)
          ? prev.categories.map((c) => (c.name === item.category ? { ...c, count: c.count + 1 } : c))
          : [...prev.categories, { name: item.category, count: 1 }],
      } : prev));
      selectPine(item.id);
      return true;
    } catch (e) {
      setCreateErr(errText(e) || '新增失败');
      return false;
    } finally {
      setCreateBusy(false);
    }
  };

  const deletePine = async () => {
    if (!sel || sel.kind !== 'pine') return false;
    const id = sel.id;
    setDeleteBusy(true);
    setDeleteErr('');
    try {
      await deletePineItem(id);
      setList(asUpdater((prev) => prev ? {
        ...prev,
        items: prev.items.filter((it) => it.id !== id),
        total: Math.max(0, prev.total - 1),
        filtered: Math.max(0, prev.filtered - 1),
      } : prev));
      setSel(null);
      setCur(null);
      setDraft('');
      setDirty(false);
      setJob(null);
      setReport(null);
      setNote(null);
      refreshNoteIds();
      refreshTranspile();
      return true;
    } catch (e) {
      setDeleteErr(errText(e) || '删除失败');
      return false;
    } finally {
      setDeleteBusy(false);
    }
  };

  // ---- 轮询：任务在跑就每 2.5s 拉一次；跑完刷新列表上的 AI/▶ 角标 ----
  const running = job?.status === 'queued' || job?.status === 'running';
  useEffect(() => {
    if (!cur || !running) return;
    const t = setInterval(() => loadJob(cur.id), POLL_MS);
    return () => clearInterval(t);
  }, [cur, running, loadJob]);

  useEffect(() => {
    if (job?.status === 'done' || job?.status === 'failed') refreshTranspile();
  }, [job?.status, refreshTranspile]);

  // ---- 结果与K线口径对齐 ----
  // 报告自带 symbol/adj，K线必须用报告自己的口径拉，否则成交点会落在错误的K线上。
  // 只在「换了一份报告」时对齐一次，之后用户仍可自由切标的看别的票。
  const syncedRef = useRef('');
  const marketRef = useRef(market);
  // 切市场：旧市场的结果/任务/错误一律作废；周期回日线（A股默认口径）
  useEffect(() => {
    if (marketRef.current === market) return;
    marketRef.current = market;
    setBt(null);
    setReport(null);
    setJob(null);
    setErr('');
    setFullSpan(false);
    setFreq('1d');
    syncedRef.current = '';
  }, [market]);

  const freqRef = useRef(freq);
  // 切周期：旧周期的回测结果不能叠到新周期 K 线上（成交点/指标会错位）
  useEffect(() => {
    if (freqRef.current === freq) return;
    freqRef.current = freq;
    if (freq !== '1d' && adj !== 'unadjusted') setAdj('unadjusted');
    setBt(null);
    setReport(null);
    setJob(null);
    setErr('');
    setFullSpan(false);
    syncedRef.current = '';
  }, [freq, adj]);

  useEffect(() => {
    if (!report || !cur) return;
    // 不在加载旧报告时强行改标的/复权——会被用户「换只票看看」的意图反拽。
    // 只把图切到全历史；成交点不属于当前 K 线时由 ResultView 的提示按钮显式同步。
    if (syncedRef.current === cur.id) return;
    syncedRef.current = cur.id;
    setFullSpan(true);
  }, [report, cur]);

  // 市场/周期/标的/复权变化 → 重新取该四元组的报告（没有则回退最新）
  useEffect(() => {
    if (sel?.kind !== 'pine') return;
    loadJob(sel.id);
  }, [sel, loadJob]);

  const result = useMemo<LabResult | null>(() => {
    if (sel?.kind === 'template') return bt ? fromTemplate(bt) : null;
    if (sel?.kind === 'pine') return report ? fromPineReport(report, cur?.title ?? '') : null;
    return null;
  }, [sel, bt, report, cur]);

  const syncToResult = () => {
    if (!result || result.market !== market || result.freq !== freq) return;
    if (result.symbol) setSymbol(result.symbol);
    if (result.adj) setAdj(result.adj as AdjMode);
  };

  // ---- 运行 ----
  const run = () => {
    if (!sel) return;
    setErr('');
    if (sel.kind === 'template') {
      setBtBusy(true);
      runLabBacktest({ strategy: sel.id, symbol, adj: runAdj, params, market, freq })
        .then((r) => {
          setBt(r);
          // 成交点标注要落在同一价格序列上 → 切到回测口径 + 全历史
          if (adj !== runAdj) setAdj(runAdj);
          setFullSpan(true);
        })
        .catch((e) => {
          setErr(errText(e) || '回测失败（后端 PyneCore 运行时或数据不可用）');
          setBt(null);
        })
        .finally(() => setBtBusy(false));
      return;
    }
    setQueuing(true);
    runPineBacktest(sel.id, symbol, runAdj, false, market, freq)
      .then(() => {
        setJob({ status: 'queued', stage: 'queued' });
        setReport(null);
      })
      .catch((e) => setErr(errText(e) || '入队失败'))
      .finally(() => setQueuing(false));
  };

  // ---- 源码编辑 ----
  const save = () => {
    if (!cur) return;
    setSaving(true);
    setSaveErr('');
    setSaveMsg('');
    savePineSource(cur.id, draft)
      .then((r) => {
        setDirty(false);
        setSaveMsg(`已保存 · ${r.lines} 行 · ${r.indent_ok ? '缩进完整' : '仍无缩进'}`);
        setCur({ ...cur, edited: true, source_kind: 'edited', source: draft });
      })
      .catch((e) => setSaveErr(errText(e) || '保存失败'))
      .finally(() => setSaving(false));
  };

  const reset = () => {
    if (!cur) return;
    setSaveErr('');
    resetPineSource(cur.id)
      .then(() => {
        setSaveMsg('已丢弃编辑，重新载入索引版本');
        selectPine(cur.id);
      })
      .catch(() => setSaveErr('重置失败'));
  };

  return {
    market,
    marketInfo,
    adjs,
    freqs,
    availFreqs,
    freq,
    setFreq: chooseFreq,
    freqNote,
    runAdj,
    backtestAdj,
    symbol,
    adj,
    setAdj,
    stockName,
    bars,
    barsBusy,
    fullSpan,
    toggleSpan: () => setFullSpan(asUpdater((v) => !v)),
    showMarkers,
    toggleMarkers: () => setShowMarkers(asUpdater((v) => !v)),
    showIndicators,
    toggleIndicators: () => setShowIndicators(asUpdater((v) => !v)),
    query,
    hits,
    showHits,
    setShowHits,
    onQuery,
    pickSymbol,
    syncToResult,

    strategies,
    params,
    setParams,

    category,
    setCategory,
    q,
    setQ,
    list,
    listBusy,
    listErr,
    loadMore: () => load(category, q, list?.items.length ?? 0),
    transpile,
    cur,
    createPine,
    createBusy,
    createErr,
    deletePine,
    deleteBusy,
    deleteErr,

    sel,
    selectTemplate,
    selectPine,
    run,
    runBusy: sel?.kind === 'template' ? btBusy : queuing,
    running,
    job,
    err,

    result,

    note,
    noteBusy,
    noteMsg,
    noteErr,
    saveNote,
    noteIds,

    chat,
    chatPending,
    chatStage: chatRunning
      ? chatJob?.status === 'staged'
        ? 'staged'
        : chatJob?.stage || 'queued'
      : '',
    chatBusy: chatSending,
    chatErr,
    sendChat,
    chatMode,
    setChatMode,
    chatJob,
    appendNote,

    applyBusy,
    applyMsg,
    applyErr,
    applyCandidate,
    revert,
    versions,

    draft,
    setDraft: (v: string) => {
      setDraft(v);
      setDirty(true);
      setSaveMsg('');
    },
    dirty,
    saving,
    saveMsg,
    saveErr,
    save,
    reset,
  };
}
