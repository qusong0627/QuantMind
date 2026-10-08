/** 行情回测工作台的控制器：左栏策略选择 / 中栏标的与K线 / 右栏运行与源码，
 *  三栏共用这一份状态，避免像旧版那样两个 tab 各存一套、结果对不上。
 *
 * 两条回测链路（内置模板同步返回 / Pine 库异步 job）在这里收敛成同一个 `LabResult`。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import {
  applyChatCandidate,
  fetchChatJob,
  fetchChatThread,
  fetchLabKlines,
  fetchLabStrategies,
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
  type Kline,
  type LabStrategy,
  type LabSymbol,
  type NoteDoc,
  type NoteInput,
  type PineJob,
  type PineList,
  type PineListItem,
  type PineReport,
  type PineTranspile,
  type VersionRow,
} from '../../api/client';
import { fromPineReport, fromTemplate, type LabResult } from './labResult';
import { errText } from './format';
import { asUpdater } from '../../reactCompat';

const PAGE = 200;
const POLL_MS = 2500;

export type Selection = { kind: 'template' | 'pine'; id: string };

export interface Workbench {
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
export function useWorkbench(symbol: string, setSymbol: (code: string) => void): Workbench {
  // ---- 标的与K线 ----
  const [adj, setAdj] = useState<AdjMode>('unadjusted');
  const [stockName, setStockName] = useState('');
  const [bars, setBars] = useState<Kline[]>([]);
  const [barsBusy, setBarsBusy] = useState(false);
  /** 看盘默认近 600 根；跑完回测自动切全历史，否则 10 年回测的买卖点大多落在图外 */
  const [fullSpan, setFullSpan] = useState(false);
  const [showMarkers, setShowMarkers] = useState(true);
  const [showIndicators, setShowIndicators] = useState(true);

  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<LabSymbol[]>([]);
  const [showHits, setShowHits] = useState(false);
  const timer = useRef<number | null>(null);

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
    applyChatCandidate(sel.id, chatJob.job_id, symbol, adj)
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
      .catch(() => setErr('策略列表加载失败（后端 /api/market-lab 未就绪？）'));
  }, []);

  // ---- K线 ----
  const loadBars = useCallback(async (sym: string, mode: AdjMode, full: boolean) => {
    setBarsBusy(true);
    try {
      const r = await fetchLabKlines(sym, mode, full ? 3000 : 600);
      setBars(r.bars);
      setStockName(r.name);
    } catch {
      setBars([]);
      setErr(`K线加载失败：${sym}`);
    } finally {
      setBarsBusy(false);
    }
  }, []);

  useEffect(() => {
    if (symbol) void loadBars(symbol, adj, fullSpan);
  }, [symbol, adj, fullSpan, loadBars]);

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
      searchLabSymbols(v, 20)
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
    fetchPineJob(id)
      .then((d) => {
        setJob(d.job);
        setReport(d.report);
      })
      .catch(() => undefined);
  }, []);

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
  useEffect(() => {
    if (!report || !cur) return;
    const key = `${cur.id}|${report.symbol}|${report.adj}`;
    if (syncedRef.current === key) return;
    syncedRef.current = key;
    if (report.symbol && report.symbol !== symbol) setSymbol(report.symbol);
    if (report.adj && report.adj !== adj) setAdj(report.adj as AdjMode);
    setFullSpan(true);
  }, [report, cur, symbol, adj]);

  const result = useMemo<LabResult | null>(() => {
    if (sel?.kind === 'template') return bt ? fromTemplate(bt) : null;
    if (sel?.kind === 'pine') return report ? fromPineReport(report, cur?.title ?? '') : null;
    return null;
  }, [sel, bt, report, cur]);

  const syncToResult = () => {
    if (!result) return;
    if (result.symbol) setSymbol(result.symbol);
    if (result.adj) setAdj(result.adj as AdjMode);
  };

  // ---- 运行 ----
  const run = () => {
    if (!sel) return;
    setErr('');
    if (sel.kind === 'template') {
      setBtBusy(true);
      runLabBacktest({ strategy: sel.id, symbol, adj: 'backward', params })
        .then((r) => {
          setBt(r);
          // 成交点标注要落在同一价格序列上 → 切到后复权（回测口径）+ 全历史
          if (adj !== 'backward') setAdj('backward');
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
    runPineBacktest(sel.id, symbol)
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
