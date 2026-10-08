import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import { usePolling } from '../hooks/usePolling';
import './DataPlatform.css';
import { asUpdater } from '../reactCompat';

/* ============================================================
   数据平台 — 多市场 parquet 仓库浏览 / 预览（借鉴 quantmind 数据管理台）
   A股 QuantDB / 港股 QuantHK / 美股 QuantUS / 期货 QuantFutures
   数据直接复用本机 quantmind 仓库（同一份 parquet，格式一致），无同步能力。
   ============================================================ */

export interface DpMarket {
  id: string;
  label: string;
  code: string;
  flag: string;
  beta: boolean;
  default_root: string;
}

export interface DpDataset {
  dataset: string;
  name: string;
  group: string;
  category_id: string;
  layout: 'partition' | 'symbol' | 'single';
  rel_dir: string;
  note: string;
  synced: boolean;
  files: number;
  size_mb: number;
  start_date?: string;
  end_date?: string;
  partitions?: number;
  updated_at?: string;
}

export interface DpGroup {
  id: string;
  name: string;
  category_id: string;
  dataset_count: number;
  synced_count: number;
  files: number;
  size_mb: number;
}

export interface DpCatalog {
  market: string;
  data_dir: string;
  exists: boolean;
  groups: DpGroup[];
  datasets: DpDataset[];
}

export interface DpPreview {
  dataset: string;
  name: string;
  source: string;
  file: string | null;
  rows_total: number;
  column_count: number;
  columns: { name: string; dtype: string }[];
  data: Record<string, unknown>[];
  symbol_total?: number;
  symbol_choices?: string[];
}

export interface DpScanItem {
  dataset?: string;
  name?: string;
  layout?: string;
  rel_dir: string;
  files: number;
  bytes: number;
}

export interface DpScan {
  root: string;
  exists: boolean;
  total_files: number;
  total_bytes: number;
  datasets: DpScanItem[];
  unknown: DpScanItem[];
}

const unwrap = async <T,>(p: Promise<{ data: { success: boolean; data: T } }>): Promise<T> =>
  (await p).data.data;

export const fetchDpMarkets = () => unwrap<DpMarket[]>(api.get('/data-platform/markets'));
export const fetchDpCatalog = (market: string) =>
  unwrap<DpCatalog>(api.get(`/data-platform/${market}/catalog`));
export const fetchDpRoot = (market: string) =>
  unwrap<{ market: string; root: string }>(api.get(`/data-platform/${market}/root`));
export const fetchDpPreview = (market: string, dataset: string, symbol?: string, limit = 50) =>
  unwrap<DpPreview>(
    api.get(`/data-platform/${market}/preview`, { params: { dataset, symbol: symbol || undefined, limit } }),
  );

/** QuantDB SDK 远端预览（消耗流量，仅 A股）。经 quantmind 代理转发。 */
export const fetchQdbRemotePreview = (dataset: string, symbol?: string, limit = 50) =>
  unwrap<DpPreview>(
    api.get('/quantmind/admin/data-platform/quantdb/preview', {
      params: { dataset, symbol: symbol || undefined, limit, remote: true },
      timeout: 120000,
    }),
  );
export const fetchDpScan = (market: string, root?: string) =>
  unwrap<DpScan>(api.get(`/data-platform/${market}/scan`, { params: { root: root || undefined } }));
export const setDpRoot = (market: string, root: string) =>
  unwrap<{ market: string; root: string }>(api.post(`/data-platform/${market}/root`, { root }));

// ---------- 数据获取方式 + 同步（经 quantmind 代理，后端零改动） ----------

export interface DpSource {
  source: string;
  label: string;
  enabled: boolean;
}

export interface DpSyncJob {
  job_id: string;
  status: string;
  stage?: string;
  datasets: string[];
  days?: number;
  total: number;
  done: number;
  error?: string;
  cancel_requested?: boolean;
  started_at: string;
  finished_at?: string;
  started_by?: string;
}

export const fetchDpSources = (market: string) =>
  unwrap<{ sources: DpSource[] }>(
    api.get(`/quantmind/admin/data-platform/${market}/data-sources`),
  );
export const setDpSources = (market: string, sources: Record<string, boolean>) =>
  unwrap<{ sources: Record<string, boolean> }>(
    api.post(`/quantmind/admin/data-platform/${market}/data-sources`, { sources }),
  );
export const startDpSync = (
  market: string,
  payload: { datasets: string[]; days?: number; with_pg?: boolean; with_qlib?: boolean },
) =>
  unwrap<{ job: DpSyncJob }>(
    api.post(`/quantmind/admin/data-platform/${market}/sync-datasets`, payload),
  );
export const fetchDpSyncJobs = (market: string) =>
  unwrap<{ jobs: DpSyncJob[] }>(api.get(`/quantmind/admin/data-platform/${market}/sync-jobs`));
export const cancelDpSync = (market: string, jobId: string) =>
  unwrap<{ job_id: string; status: string }>(
    api.post(`/quantmind/admin/data-platform/${market}/sync-jobs/${jobId}/cancel`),
  );

// ---------- 格式化 ----------

const LAYOUT_LABEL: Record<string, string> = {
  partition: '按日分区',
  symbol: '按标的',
  single: '单文件',
};

const fmtBytes = (bytes: number): string => {
  if (!bytes || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
};

const fmtMb = (mb: number): string =>
  mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;

const fmtDate = (d: string | undefined): string => {
  if (!d) return '—';
  if (d.length === 8) return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}`;
  return d;
};

const fmtTime = (iso: string | undefined): string =>
  iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '—';

const fmtCell = (value: unknown): string => {
  if (value === null || value === undefined) return '';
  if (typeof value === 'number') return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(4);
  if (typeof value === 'boolean') return String(value);
  return String(value);
};

/* ============================================================
   页面主体
   ============================================================ */

export default function DataPlatform() {
  const [markets, setMarkets] = useState<DpMarket[]>([]);
  const [market, setMarket] = useState('quantdb');
  const catalog = usePolling(() => fetchDpCatalog(market), [market], 30000);
  const [rootInfo, setRootInfo] = useState<{ root: string } | null>(null);
  const [folderOpen, setFolderOpen] = useState(false);
  const [previewDataset, setPreviewDataset] = useState<DpDataset | null>(null);

  useEffect(() => {
    fetchDpMarkets().then(setMarkets).catch(() => undefined);
  }, []);

  useEffect(() => {
    fetchDpRoot(market).then(setRootInfo).catch(() => undefined);
  }, [market]);

  return (
    <div className="page">
      <div className="dp-header">
        <div>
          <div className="dp-title">数据平台</div>
          <div className="dp-sub">
            本地 parquet 仓库浏览 · 数据直接复用本机 quantmind 仓库（同一份数据，格式一致）· 无同步
          </div>
        </div>
        <span className="dp-refresh">30 秒自动刷新 · 目录 {catalog.data ? `${catalog.data.datasets.filter((d) => d.synced).length}/${catalog.data.datasets.length} 有数据` : '加载中'}</span>
      </div>

      {/* 市场 tab */}
      <div className="dp-tabs">
        {markets.length === 0 &&
          ['quantdb', 'quanthk', 'quantus', 'quantfutures'].map((m) => (
            <button key={m} className={`dp-tab ${m === market ? 'active' : ''}`} onClick={() => setMarket(m)}>
              {m === 'quantdb' ? '🇨🇳 A股市场 (QuantDB)' : m === 'quanthk' ? '🇭🇰 港股市场 (QuantHK)' : m === 'quantus' ? '🇺🇸 美股市场 (QuantUS)' : '⚡ 国内期货 (QuantFutures)'}
              {m !== 'quantdb' && <span className="dp-beta">Beta</span>}
            </button>
          ))}
        {markets.map((m) => (
          <button key={m.id} className={`dp-tab ${m.id === market ? 'active' : ''}`} onClick={() => setMarket(m.id)}>
            {m.flag} {m.label} ({m.code})
            {m.beta && <span className="dp-beta">Beta</span>}
          </button>
        ))}
      </div>

      {catalog.error && <div className="error-box">加载失败：{catalog.error}</div>}

      {/* QuantDB SDK 云端直供（仅 A股）：输入 key 获取流量 */}
      {market === 'quantdb' && <QuantDbSdkCard />}

      {/* 数据获取方式（勾选启用的数据源，同步时生效） */}
      <SourcesCard market={market} />

      {/* 目录卡 */}
      <div className="dp-card">
        <div className="dp-card-head">
          <div>
            <div className="dp-card-title">数据集目录</div>
            <div className="dp-card-desc">
              本地目录 <code className="dp-code">{catalog.data?.data_dir ?? rootInfo?.root ?? '…'}</code>
              {!catalog.data?.exists && <span className="dp-warn">（目录不存在，请选择文件夹）</span>}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="dp-btn" onClick={() => setFolderOpen(true)}>📁 选择文件夹</button>
            <button className="dp-btn" onClick={() => { catalog.refresh(); }}>刷新</button>
          </div>
        </div>

        {catalog.data && catalog.data.groups.length === 0 && (
          <div className="dp-empty">该目录下未识别到数据集</div>
        )}

        {catalog.data?.groups.map((g) => {
          const members = (catalog.data?.datasets ?? []).filter((d) => d.group === g.id);
          if (members.length === 0) return null;
          return (
            <GroupSection key={g.id} group={g} datasets={members} onPreview={setPreviewDataset} />
          );
        })}
      </div>

      {/* 数据同步（复刻 quantmind：按数据集触发 + 任务列表/进度/取消） */}
      <SyncCard market={market} datasets={catalog.data?.datasets ?? []} />

      {/* 预览弹层 */}
      {previewDataset && (
        <PreviewModal market={market} dataset={previewDataset} onClose={() => setPreviewDataset(null)} />
      )}

      {/* 文件夹选择弹层 */}
      {folderOpen && (
        <FolderModal market={market} onClose={() => setFolderOpen(false)} onApplied={() => { catalog.refresh(); }} />
      )}
    </div>
  );
}

/* ============================================================
   QuantDB SDK 云端直供卡（A股专属）：API key 输入 + 流量配额
   ============================================================ */

interface QdbUsage {
  used_gb: number;
  limit_gb: number;
  remaining_gb: number;
  credit_gb?: number;
  purchased_gb?: number;
  subscription?: { status: string; plan_id?: string; ended_at?: string };
}

interface QdbInfo {
  installed: boolean;
  api_key_configured: boolean;
  connected: boolean;
  version?: string;
  account?: { username: string; email: string };
  usage?: QdbUsage;
  error?: string;
}

function QuantDbSdkCard() {
  const [info, setInfo] = useState<QdbInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [key, setKey] = useState('');
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.get('/quantmind/admin/data-platform/quantdb/info');
      setInfo(r.data?.data?.quantdb ?? null);
    } catch (err) {
      setMsg({ ok: false, text: `获取 QuantDB 状态失败：${err instanceof Error ? err.message : String(err)}` });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const saveKey = async () => {
    if (!key.trim()) return;
    setSaving(true);
    setMsg(null);
    try {
      const r = await api.post('/quantmind/admin/data-platform/quantdb/config', { api_key: key.trim() });
      const d = r.data?.data ?? {};
      if (d.verified) {
        setMsg({ ok: true, text: `已保存并验证：${d.api_key_masked}` });
        setKey('');
        load();
      } else {
        setMsg({ ok: false, text: `Key 验证失败：${d.error ?? '未知错误'}` });
      }
    } catch (err) {
      setMsg({ ok: false, text: `保存失败：${err instanceof Error ? err.message : String(err)}` });
    } finally {
      setSaving(false);
    }
  };

  const usage = info?.usage;
  const usagePct = usage && usage.limit_gb > 0 ? Math.min(100, Math.round((usage.used_gb / usage.limit_gb) * 100)) : 0;
  const connected = info?.connected;
  const sub = usage?.subscription;

  return (
    <div className="dp-card dp-sdk">
      <div className="dp-card-head">
        <div>
          <div className="dp-card-title">QuantDB 云端直供（A股）</div>
          <div className="dp-card-desc">
            SDK {info?.installed ? `v${info.version} 已安装` : '未安装'} · 远端预览消耗流量 · 本地浏览零流量
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <span className={`dp-sdk-state ${connected ? 'dp-on' : 'dp-off'}`}>
            {connected === undefined ? '…' : connected ? '已连接' : '未连接'}
          </span>
          <button className="dp-btn" onClick={load} disabled={loading}>刷新</button>
        </div>
      </div>

      {info?.error && <div className="dp-error">{info.error}</div>}
      {msg && <div className={msg.ok ? 'dp-ok' : 'dp-error'}>{msg.text}</div>}

      <div className="dp-sdk-body">
        {/* 左：API Key */}
        <div className="dp-sdk-col">
          <div className="dp-sdk-row">
            <span className="dp-field-label">API Key</span>
            <span className={`dp-tag ${info?.api_key_configured ? 'dp-tag-green' : ''}`}>
              {info?.api_key_configured ? '已配置' : '未配置'}
            </span>
          </div>
          <div className="dp-toolbar" style={{ marginTop: 6 }}>
            <input
              type="password"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder={info?.api_key_configured ? '输入新 key 覆盖（只写不回显）' : '粘贴 QuantDB API Key 以获取流量'}
              className="dp-input"
              style={{ minWidth: 260 }}
            />
            <button className="dp-btn dp-btn-primary" onClick={saveKey} disabled={saving || !key.trim()}>
              {saving ? '验证中…' : '保存并验证'}
            </button>
          </div>
          {info?.account?.username && (
            <div className="dp-card-desc" style={{ marginTop: 6 }}>
              账户：{info.account.username}
              {sub?.plan_id && <> · 订阅：<span className="dp-tag dp-tag-green">{sub.plan_id}</span></>}
              {sub && <span className="dp-tag">{sub.status === 'active' ? '订阅中' : sub.status}</span>}
            </div>
          )}
        </div>

        {/* 右：流量配额 */}
        <div className="dp-sdk-col dp-sdk-traffic">
          <div className="dp-sdk-row">
            <span className="dp-field-label">流量配额</span>
            <span className="dp-sdk-big">{usage ? usage.remaining_gb.toFixed(1) : '—'} GB</span>
            <span className="dp-dim">剩余</span>
            <a className="dp-btn dp-btn-mini" href="https://www.quantdb.cn/pricing.html" target="_blank" rel="noopener noreferrer">购买流量 ↗</a>
          </div>
          <div className="dp-meter">
            <div className="dp-meter-fill" style={{ width: `${usagePct}%` }} />
          </div>
          <div className="dp-card-desc">
            已用 {usage ? usage.used_gb.toFixed(1) : '—'} / {usage ? usage.limit_gb.toFixed(0) : '—'} GB
            {usage?.credit_gb ? ` · 赠送 ${usage.credit_gb} GB` : ''}
            {sub?.ended_at && ` · 到期 ${sub.ended_at.slice(0, 10)}`}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ============================================================
   数据获取方式（复刻 quantmind：勾选启用的数据源，同步时生效）
   AKShare / 雅虎 / 北向 / 南向 / CCASS 等，经 quantmind 数据平台
   ============================================================ */

function SourcesCard({ market }: { market: string }) {
  const [sources, setSources] = useState<DpSource[]>([]);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetchDpSources(market);
      setSources(r.sources);
    } catch (err) {
      setMsg({ ok: false, text: `获取数据源失败：${err instanceof Error ? err.message : String(err)}` });
    }
  }, [market]);

  useEffect(() => {
    setSources([]);
    setMsg(null);
    load();
  }, [load]);

  const toggle = async (s: DpSource) => {
    setBusy(s.source);
    setMsg(null);
    const next = sources.map((x) => (x.source === s.source ? { ...x, enabled: !x.enabled } : x));
    try {
      await setDpSources(
        market,
        Object.fromEntries(next.map((x) => [x.source, x.enabled])),
      );
      setSources(next);
      setMsg({ ok: true, text: `已保存：${s.label} ${s.enabled ? '停用' : '启用'}` });
    } catch (err) {
      setMsg({ ok: false, text: `保存失败：${err instanceof Error ? err.message : String(err)}` });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="dp-card">
      <div className="dp-card-head">
        <div>
          <div className="dp-card-title">数据获取方式</div>
          <div className="dp-card-desc">
            勾选启用的数据源（quantmind 数据平台，同步时生效）· AKShare / 雅虎 / 北向 / 南向 / CCASS 等
          </div>
        </div>
        <button className="dp-btn" onClick={load} disabled={busy !== null}>刷新</button>
      </div>
      {msg && <div className={msg.ok ? 'dp-ok' : 'dp-error'}>{msg.text}</div>}
      <div className="dp-sdk-body">
        {sources.length === 0 && <div className="dp-loading">加载数据源…</div>}
        {sources.map((s) => (
          <button
            key={s.source}
            className={`dp-chip ${s.enabled ? 'dp-chip-on' : ''}`}
            disabled={busy !== null}
            onClick={() => toggle(s)}
            title={s.enabled ? '点击停用' : '点击启用'}
          >
            <span className="dp-chip-dot">{s.enabled ? '●' : '○'}</span>
            {s.label}
            <span className="dp-dim">{s.source}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ============================================================
   数据同步（复刻 quantmind：按数据集触发 + 任务列表/进度/取消）
   quantdb：with_pg / with_qlib；其余市场：days + with_qlib
   ============================================================ */

const SYNC_STATUS_LABEL: Record<string, string> = {
  running: '同步中',
  completed: '完成',
  failed: '失败',
  cancelled: '已取消',
};

function SyncCard({ market, datasets }: { market: string; datasets: DpDataset[] }) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [days, setDays] = useState(5);
  const [withPg, setWithPg] = useState(false);
  const [withQlib, setWithQlib] = useState(false);
  const [starting, setStarting] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const jobs = usePolling(() => fetchDpSyncJobs(market), [market], 8000);

  const jobList = jobs.data?.jobs ?? [];
  const runningCount = jobList.filter((j) => j.status === 'running').length;

  const toggleSelect = (dataset: string) => {
    setSelected(asUpdater((prev) => {
      const next = new Set(prev);
      if (next.has(dataset)) next.delete(dataset);
      else next.add(dataset);
      return next;
    }));
  };

  const start = async () => {
    if (selected.size === 0) return;
    setStarting(true);
    setMsg(null);
    try {
      const payload =
        market === 'quantdb'
          ? { datasets: [...selected], with_pg: withPg, with_qlib: withQlib }
          : { datasets: [...selected], days, with_qlib: withQlib };
      const r = await startDpSync(market, payload);
      setMsg({ ok: true, text: `同步任务已触发：${r.job.job_id}` });
      setSelected(new Set());
      jobs.refresh();
    } catch (err) {
      setMsg({ ok: false, text: `触发失败：${err instanceof Error ? err.message : String(err)}` });
    } finally {
      setStarting(false);
    }
  };

  const cancel = async (jobId: string) => {
    try {
      const r = await cancelDpSync(market, jobId);
      setMsg({ ok: true, text: `${jobId} ${r.status}（当前数据集完成后停止）` });
      jobs.refresh();
    } catch (err) {
      setMsg({ ok: false, text: `取消失败：${err instanceof Error ? err.message : String(err)}` });
    }
  };

  return (
    <div className="dp-card">
      <div className="dp-card-head">
        <div>
          <div className="dp-card-title">数据同步</div>
          <div className="dp-card-desc">
            按数据集触发同步（后台任务，写入本机共享仓库）· 任务历史最多保留 20 条
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {runningCount > 0 && <span className="dp-warn">{runningCount} 个任务运行中</span>}
          <button className="dp-btn" onClick={() => jobs.refresh()} disabled={jobs.loading}>刷新</button>
        </div>
      </div>

      {msg && <div className={msg.ok ? 'dp-ok' : 'dp-error'}>{msg.text}</div>}

      {/* 数据集多选 */}
      <div className="dp-toolbar" style={{ alignItems: 'flex-start' }}>
        <div className="dp-field dp-field-wide">
          <span className="dp-field-label">选择数据集（可多选）</span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {datasets.map((d) => (
              <button
                key={d.dataset}
                className={`dp-chip ${selected.has(d.dataset) ? 'dp-chip-on' : ''}`}
                onClick={() => toggleSelect(d.dataset)}
                title={d.note}
              >
                {d.name}
                <span className="dp-dim">{d.dataset}</span>
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* 参数 + 触发 */}
      <div className="dp-toolbar" style={{ alignItems: 'flex-end' }}>
        {market !== 'quantdb' && (
          <label className="dp-field">
            <span className="dp-field-label">最近交易日（天）</span>
            <input
              type="number"
              min={1}
              max={365}
              value={days}
              onChange={(e) => setDays(Number(e.target.value) || 5)}
              className="dp-input dp-input-num"
            />
          </label>
        )}
        <label className="dp-field" style={{ flexDirection: 'row', alignItems: 'center', gap: 4 }}>
          <input type="checkbox" checked={withQlib} onChange={(e) => setWithQlib(e.target.checked)} />
          <span className="dp-field-label" style={{ fontSize: 10 }}>同步后重建 Qlib 缓存</span>
        </label>
        {market === 'quantdb' && (
          <label className="dp-field" style={{ flexDirection: 'row', alignItems: 'center', gap: 4 }}>
            <input type="checkbox" checked={withPg} onChange={(e) => setWithPg(e.target.checked)} />
            <span className="dp-field-label" style={{ fontSize: 10 }}>回填 PG stock_daily_latest</span>
          </label>
        )}
        <button
          className="dp-btn dp-btn-primary"
          onClick={start}
          disabled={starting || selected.size === 0}
        >
          {starting ? '触发中…' : `开始同步${selected.size > 0 ? `（${selected.size} 个数据集）` : ''}`}
        </button>
      </div>

      {/* 任务列表 */}
      <div className="table-wrap" style={{ marginTop: 12 }}>
        {jobList.length === 0 && !jobs.loading && <div className="dp-empty">暂无同步任务</div>}
        {jobList.length > 0 && (
          <table className="data">
            <thead>
              <tr>
                <th>任务</th>
                <th>数据集</th>
                <th>状态</th>
                <th>进度</th>
                <th>开始时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {jobList.map((j) => {
                const pct = j.total > 0 ? Math.round((j.done / j.total) * 100) : 0;
                return (
                  <tr key={j.job_id}>
                    <td>
                      <span style={{ fontWeight: 700 }}>{j.job_id}</span>
                      <div className="dp-dim">{j.started_by ? `发起：${j.started_by}` : ''}</div>
                    </td>
                    <td className="dp-dim">{j.datasets.join(', ')}</td>
                    <td>
                      <span className={`dp-dot ${j.status === 'completed' ? 'dp-on' : j.status === 'running' ? 'dp-on' : 'dp-off'}`}>
                        {SYNC_STATUS_LABEL[j.status] ?? j.status}
                      </span>
                      {j.cancel_requested && <span className="dp-warn"> 取消中</span>}
                    </td>
                    <td style={{ minWidth: 140 }}>
                      <div className="dp-meter" style={{ height: 7, margin: 0 }}>
                        <div className="dp-meter-fill" style={{ width: `${pct}%` }} />
                      </div>
                      <div className="dp-dim">
                        {j.done}/{j.total}
                        {j.stage ? ` · ${j.stage}` : ''}
                        {j.error ? ` · ${j.error}` : ''}
                      </div>
                    </td>
                    <td className="dp-dim">{fmtTime(j.started_at)}</td>
                    <td>
                      {j.status === 'running' && (
                        <button className="dp-btn dp-btn-mini" onClick={() => cancel(j.job_id)}>
                          取消
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

/* ============================================================
   分组折叠
   ============================================================ */

function GroupSection({
  group,
  datasets,
  onPreview,
}: {
  group: DpGroup;
  datasets: DpDataset[];
  onPreview: (d: DpDataset) => void;
}) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    setOpen(false);
  }, [group.id]);
  return (
    <div className="dp-group">
      <button className="dp-group-head" onClick={() => setOpen(!open)}>
        <span className="dp-group-arrow">{open ? '▼' : '▶'}</span>
        <span className="dp-group-name">{group.name}</span>
        <span className="dp-group-tag">
          {group.synced_count}/{group.dataset_count} 有数据
        </span>
        <span className="dp-group-size">
          {group.files.toLocaleString()} 文件 · {fmtMb(group.size_mb)}
        </span>
      </button>
      {open && (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>数据集</th>
                <th>形态</th>
                <th>本地</th>
                <th>文件数</th>
                <th>大小</th>
                <th>数据区间</th>
                <th>更新时间</th>
                <th>说明</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {datasets.map((d) => (
                <tr
                  key={d.dataset}
                  className="dp-row"
                  style={{ cursor: d.synced ? 'pointer' : 'default' }}
                  title={d.synced ? '点击直接预览' : undefined}
                  onClick={() => d.synced && onPreview(d)}
                >
                  <td>
                    <div style={{ fontWeight: 700 }}>{d.name}</div>
                    <div className="dp-dim">{d.dataset}</div>
                  </td>
                  <td><span className="dp-layout">{LAYOUT_LABEL[d.layout] ?? d.layout}</span></td>
                  <td>
                    <span className={`dp-dot ${d.synced ? 'dp-on' : 'dp-off'}`}>{d.synced ? '有' : '无'}</span>
                  </td>
                  <td>{d.files.toLocaleString()}</td>
                  <td>{d.files ? fmtMb(d.size_mb) : '—'}</td>
                  <td className="dp-dim">
                    {d.start_date ? `${fmtDate(d.start_date)} → ${fmtDate(d.end_date)}` : '—'}
                    {d.partitions ? ` (${d.partitions} 分区)` : ''}
                  </td>
                  <td className="dp-dim">{fmtTime(d.updated_at)}</td>
                  <td className="dp-dim" title={d.note}>{d.note || '—'}</td>
                  <td>
                    <button
                      className="dp-btn dp-btn-mini"
                      disabled={!d.synced}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (d.synced) onPreview(d);
                      }}
                    >
                      预览
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ============================================================
   预览弹层（复刻 quantmind QuantDBPreviewDrawer：symbol 检索 + 行数 + 列头 dtype + 表格）
   ============================================================ */

function PreviewModal({
  market,
  dataset,
  onClose,
}: {
  market: string;
  dataset: DpDataset;
  onClose: () => void;
}) {
  const [preview, setPreview] = useState<DpPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [symbol, setSymbol] = useState('');
  const [limit, setLimit] = useState(50);

  const load = useCallback(
    async (sym = symbol.trim(), lim = limit) => {
      setLoading(true);
      setError(null);
      try {
        setPreview(await fetchDpPreview(market, dataset.dataset, sym || undefined, lim));
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setPreview(null);
      } finally {
        setLoading(false);
      }
    },
    [market, dataset.dataset, symbol, limit],
  );

  const fetchRemote = async () => {
    if (market !== 'quantdb') return;
    setLoading(true);
    setError(null);
    try {
      setPreview(await fetchQdbRemotePreview(dataset.dataset, symbol.trim() || undefined, limit));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPreview(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setSymbol('');
    setLimit(50);
    setPreview(null);
    setError(null);
    load('', 50);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market, dataset.dataset]);

  const supportsSymbol = dataset.layout === 'symbol';
  const choices = preview?.symbol_choices ?? [];

  return (
    <div className="dp-overlay" onClick={onClose}>
      <div className="dp-modal" onClick={(e) => e.stopPropagation()}>
        <div className="dp-modal-head">
          <span className="dp-modal-title">
            {dataset.name} · <span className="dp-dim">{dataset.dataset}</span>
          </span>
          <button className="dp-btn" onClick={onClose}>✕ 关闭</button>
        </div>

        {/* 检索工具条 */}
        <div className="dp-toolbar">
          {supportsSymbol && (
            <label className="dp-field">
              <span className="dp-field-label">标的</span>
              <input
                list={`dp-symbols-${dataset.dataset}`}
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                placeholder="如 600519.SH"
                className="dp-input"
              />
              <datalist id={`dp-symbols-${dataset.dataset}`}>
                {choices.map((s) => <option key={s} value={s} />)}
              </datalist>
            </label>
          )}
          <label className="dp-field">
            <span className="dp-field-label">行数</span>
            <input
              type="number"
              min={1}
              max={200}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value) || 50)}
              className="dp-input dp-input-num"
            />
          </label>
          <button className="dp-btn dp-btn-primary" onClick={() => load()} disabled={loading}>
            查询
          </button>
          <button className="dp-btn" onClick={() => load()} disabled={loading}>刷新</button>
          {market === 'quantdb' && (
            <button className="dp-btn" onClick={fetchRemote} disabled={loading} title="经 QuantDB SDK 远端预览，消耗流量">
              远端预览
            </button>
          )}
        </div>

        {/* 元信息 */}
        {preview && (
          <div className="dp-meta">
            <span className={`dp-tag ${preview.source === 'local' ? 'dp-tag-green' : 'dp-tag-blue'}`}>
              {preview.source === 'local' ? '本地 parquet（零流量）' : 'SDK 远端预览'}
            </span>
            <span className="dp-tag">{preview.rows_total.toLocaleString()} 行</span>
            <span className="dp-tag">{preview.column_count} 列</span>
            {preview.symbol_total !== undefined && (
              <span className="dp-tag dp-tag-purple">{preview.symbol_total.toLocaleString()} 个标的</span>
            )}
            {preview.file && <span className="dp-dim dp-file">{preview.file}</span>}
          </div>
        )}

        {error && <div className="dp-error">预览失败：{error}</div>}
        {loading && <div className="dp-loading">LOADING…</div>}

        {preview && preview.data.length > 0 && (
          <div className="table-wrap">
            <table className="data dp-preview-table">
              <thead>
                <tr>
                  {preview.columns.map((c) => (
                    <th key={c.name}>
                      <div style={{ fontWeight: 700 }}>{c.name}</div>
                      <div className="dp-col-dtype">{c.dtype}</div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.data.map((row, i) => (
                  <tr key={i}>
                    {preview.columns.map((c) => (
                      <td key={c.name} className={row[c.name] == null ? 'dp-null' : ''}>
                        {fmtCell(row[c.name])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {preview && preview.data.length === 0 && !error && (
          <div className="dp-empty">
            {dataset.synced ? '该数据集本地无可预览样本' : '该数据集本地无数据'}
          </div>
        )}
      </div>
    </div>
  );
}

/* ============================================================
   文件夹选择弹层（预检 → 识别数据集 → 应用为新数据根）
   ============================================================ */

function FolderModal({
  market,
  onClose,
  onApplied,
}: {
  market: string;
  onClose: () => void;
  onApplied: () => void;
}) {
  const [root, setRoot] = useState('');
  const [scan, setScan] = useState<DpScan | null>(null);
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applied, setApplied] = useState(false);

  useEffect(() => {
    fetchDpRoot(market).then((r) => setRoot(r.root)).catch(() => undefined);
  }, [market]);

  const preflight = async () => {
    setLoading(true);
    setError(null);
    try {
      setScan(await fetchDpScan(market, root.trim() || undefined));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const apply = async () => {
    if (!scan?.exists) return;
    setApplying(true);
    try {
      const r = await setDpRoot(market, scan.root);
      setRoot(r.root);
      setApplied(true);
      onApplied();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setApplying(false);
    }
  };

  return (
    <div className="dp-overlay" onClick={onClose}>
      <div className="dp-modal dp-modal-folder" onClick={(e) => e.stopPropagation()}>
        <div className="dp-modal-head">
          <span className="dp-modal-title">📁 选择数据文件夹</span>
          <button className="dp-btn" onClick={onClose}>✕ 关闭</button>
        </div>

        <div className="dp-toolbar">
          <label className="dp-field dp-field-wide">
            <span className="dp-field-label">数据根目录</span>
            <input
              value={root}
              onChange={(e) => setRoot(e.target.value)}
              placeholder="服务器上的数据根目录，如 /data/quantdb"
              className="dp-input"
            />
          </label>
          <button className="dp-btn" onClick={preflight} disabled={loading}>
            {loading ? '预检中…' : '预检'}
          </button>
        </div>

        {error && <div className="dp-error">{error}</div>}

        {scan && !scan.exists && (
          <div className="dp-error">目录不存在：{scan.root}</div>
        )}

        {scan?.exists && (
          <>
            <div className="dp-stats">
              <div className="dp-stat">
                <div className="dp-stat-v">{scan.total_files.toLocaleString()}</div>
                <div className="dp-stat-k">parquet 文件</div>
              </div>
              <div className="dp-stat">
                <div className="dp-stat-v">{fmtBytes(scan.total_bytes)}</div>
                <div className="dp-stat-k">数据总量</div>
              </div>
              <div className="dp-stat">
                <div className="dp-stat-v">{scan.datasets.length}</div>
                <div className="dp-stat-k">识别数据集</div>
              </div>
              <div className="dp-stat">
                <div className="dp-stat-v">{scan.unknown.length}</div>
                <div className="dp-stat-k">未登记目录</div>
              </div>
            </div>

            {scan.datasets.length > 0 && (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>数据集</th>
                      <th>名称</th>
                      <th>形态</th>
                      <th>本地目录</th>
                      <th>文件数</th>
                      <th>大小</th>
                    </tr>
                  </thead>
                  <tbody>
                    {scan.datasets.map((d) => (
                      <tr key={d.rel_dir}>
                        <td style={{ fontWeight: 700 }}>{d.name}</td>
                        <td className="dp-dim">{d.dataset}</td>
                        <td><span className="dp-layout">{LAYOUT_LABEL[d.layout ?? ''] ?? d.layout}</span></td>
                        <td className="dp-dim">{d.rel_dir}</td>
                        <td>{d.files.toLocaleString()}</td>
                        <td>{fmtBytes(d.bytes)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {scan.unknown.length > 0 && (
              <div className="dp-card-desc" style={{ padding: '6px 2px 0' }}>
                未登记目录：
                {scan.unknown.map((u) => (
                  <span key={u.rel_dir} className="dp-tag">
                    {u.rel_dir} · {u.files.toLocaleString()} 文件 · {fmtBytes(u.bytes)}
                  </span>
                ))}
              </div>
            )}

            <div className="dp-modal-foot">
              {applied && <span className="dp-ok">✓ 已应用，目录已切换</span>}
              <button className="dp-btn dp-btn-primary" onClick={apply} disabled={applying}>
                {applying ? '应用…' : '使用此目录'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
