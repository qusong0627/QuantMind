/**
 * 交易黑名单维护表（个人中心）。
 *
 * 这一页维护的是**排除名单的用户层**：手工加入 / 例外放行 / 撤销。机器基线
 * （隔壁 quant-Trader 每日导入的那 1800+ 只）在这里只读——可以放行其中某一只，
 * 但不能删，因为下一次导入会把它带回来。两层的关系在表头写清楚，不然用户
 * 会花时间找「删除」按钮然后以为功能坏了。
 *
 * 三个「说清楚」的地方，缺一个用户就会误判：
 * - 名单**未导入**与名单为空必须分开说（前者是配置事故，要去跑导入器）；
 * - **拦买只数**（合并放行后的真实值）与名单总条数分开显示；
 * - **放行未命中**（放行了本来就不在名单里的票）要主动报出来。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  Check,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  ShieldAlert,
  Trash2,
  Undo2,
} from 'lucide-react';
import { Input, Modal, Select, message } from 'antd';
import { tradingBlacklistService } from './service';
import {
  allowMissNote,
  manualActive,
  reasonView,
  rowActions,
  rowBadge,
  sourceLines,
  staleNote,
  summarize,
  type RowAction,
} from './blacklistModel';
import type { ActionFilter, BlacklistListMeta, BlacklistRow } from './types';

const PAGE_SIZE = 50;

const ACTION_OPTIONS: { value: ActionFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'manual', label: '我排除的' },
  { value: 'allow', label: '我放行的' },
  { value: 'machine', label: '机器名单' },
];

export function TradingBlacklistPanel() {
  const [rows, setRows] = useState<BlacklistRow[]>([]);
  const [meta, setMeta] = useState<BlacklistListMeta | null>(null);
  const [imported, setImported] = useState(true);
  const [importReason, setImportReason] = useState<string | undefined>();
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [actionFilter, setActionFilter] = useState<ActionFilter>('all');
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [query]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await tradingBlacklistService.list({
        q: debounced,
        action: actionFilter,
        page,
        pageSize: PAGE_SIZE,
      });
      setRows(data.items ?? []);
      setMeta(data.meta ?? null);
      setImported(data.imported);
      setImportReason(data.reason);
      setTotal(data.total ?? 0);
    } catch (err) {
      message.error(`名单读取失败：${(err as Error).message}`);
      setRows([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [debounced, actionFilter, page]);

  useEffect(() => {
    load();
  }, [load]);

  // 过滤条件变化时回到第一页——否则「在第 30 页搜一个词」会得到「无结果」，
  // 而实际只是那一条在后排
  useEffect(() => {
    setPage(1);
  }, [debounced, actionFilter]);

  const summary = useMemo(() => summarize(meta, imported, importReason), [meta, imported, importReason]);
  const stale = staleNote(summary);
  const miss = allowMissNote(summary);
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // 「放行」与「改理由」都要填理由，所以走模态而不是 Modal.confirm：
  // confirm 只能确认一段既定文案，没有输入的地方。
  const [entry, setEntry] = useState<EntryTarget | null>(null);

  /**
   * 整行点击 → 打开这一行的详情/编辑弹窗。
   *
   * 表格那一列再宽也放不下 200 字的机器理由（实测合并前最长的一条就是那么长），
   * 而「这只票为什么被排除」正是这一页存在的全部意义 —— 所以点开看全文，
   * 并且顺手就能改自己那条：有生效中的本人条目走「改理由」，没有就走「加入」。
   */
  const openRow = useCallback((row: BlacklistRow) => {
    setEntry(manualActive(row) ? { mode: 'edit', row } : { mode: 'add', row });
  }, []);

  const runAction = useCallback(
    (row: BlacklistRow, action: RowAction) => {
      if (action.kind === 'allow') {
        setEntry({ mode: 'allow', row });
        return;
      }
      if (action.kind === 'edit') {
        setEntry({ mode: 'edit', row });
        return;
      }
      const label = row.name ? `${row.symbol} ${row.name}` : row.symbol;
      const confirmText: Record<'delete' | 'unallow', string> = {
        delete: `撤销对 ${label} 的手工排除？该股票将回到「由系统名单决定」的状态。`,
        unallow: `取消对 ${label} 的放行？该股票将重新按系统名单被排除。`,
      };
      Modal.confirm({
        title: action.label,
        content: confirmText[action.kind],
        okText: '确认',
        cancelText: '取消',
        onOk: async () => {
          setBusy(row.symbol);
          try {
            // `delete`（撤销我的手排）与 `unallow`（撤销我的放行）都是「删掉我那条改动」：
            // 删掉之后这一行自动回到系统名单的判定，这正是用户期望的结果。
            // 取消放行**绝不能**写成 upsert(action='block')——那会把「解除放行」
            // 变成「亲手拉黑」，方向完全相反。
            await tradingBlacklistService.remove(row.symbol);
            message.success(`${action.label}成功`);
            await load();
          } catch (err) {
            message.error(`${action.label}失败：${(err as Error).message}`);
          } finally {
            setBusy(null);
          }
        },
      });
    },
    [load],
  );

  return (
    <section className="bg-white rounded-2xl border border-gray-200 shadow-sm flex flex-col min-h-0">
      <header className="px-4 pt-3 pb-2.5 border-b border-gray-100 shrink-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-rose-50 text-rose-600 shrink-0">
            <ShieldAlert size={15} />
          </span>
          <h3 className="text-[15px] font-bold text-gray-800">交易黑名单</h3>
          <span className="text-[11px] text-gray-500">
            候选信号默认排除这些股票；放行需要你逐只确认
          </span>
          <button
            onClick={load}
            disabled={loading}
            className="ml-auto shrink-0 flex items-center gap-1 text-[11px] font-bold text-gray-400 hover:text-indigo-600 transition-colors disabled:opacity-50"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} /> 刷新
          </button>
        </div>

        {/* 统计条：只数与基准日。名单没导入是一等状态，不渲染成「0 条」 */}
        <div className="flex items-center gap-2 flex-wrap mt-2">
          {summary.imported ? (
            <>
              <Stat label="名单总条数" value={summary.total} tone="slate" />
              <Stat label="当前拦买" value={summary.blocking} tone="rose" />
              <Stat label="我排除的" value={summary.manual} tone="amber" />
              <Stat label="我放行的" value={summary.allow} tone="emerald" />
              <span className="text-[10px] text-gray-400 font-mono">
                基准日 {summary.asof || '未知'}
              </span>
            </>
          ) : (
            <span className="flex items-center gap-1.5 text-[11px] font-bold text-rose-600 bg-rose-50 border border-rose-100 rounded-lg px-2 py-1">
              <AlertTriangle size={12} />
              {summary.reason || '名单未导入'}
            </span>
          )}
        </div>

        {stale && (
          <div className="mt-1.5 flex items-center gap-1.5 text-[11px] font-bold text-amber-700 bg-amber-50 border border-amber-100 rounded-lg px-2 py-1">
            <AlertTriangle size={12} /> {stale} —— 请运行 <code className="font-mono">backend/scripts/import_exclusion_list.py</code> 刷新
          </div>
        )}
        {miss && (
          <div className="mt-1.5 flex items-center gap-1.5 text-[11px] font-bold text-amber-700 bg-amber-50 border border-amber-100 rounded-lg px-2 py-1">
            <AlertTriangle size={12} /> {miss}
          </div>
        )}
      </header>

      {/* 工具条 */}
      <div className="px-4 py-2 flex items-center gap-2 shrink-0 border-b border-gray-100">
        <Input
          size="small"
          allowClear
          prefix={<Search size={12} className="text-gray-400" />}
          placeholder="搜索代码或名称"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="max-w-[200px]"
        />
        <Select
          size="small"
          value={actionFilter}
          onChange={(v) => setActionFilter(v)}
          options={ACTION_OPTIONS}
          className="w-[110px]"
        />
        <span className="text-[11px] text-gray-400">共 {total} 条</span>
        <button
          onClick={() => setAddOpen(true)}
          className="ml-auto flex items-center gap-1 px-2.5 py-1 rounded-lg bg-rose-600 text-white text-[11px] font-bold hover:bg-rose-700 transition-colors"
        >
          <Plus size={12} /> 加入黑名单
        </button>
      </div>

      {/* 表体 */}
      <div className="flex-1 min-h-0 overflow-auto custom-scrollbar">
        <table className="w-full text-[12px] border-collapse">
          <thead className="bg-gray-50 text-gray-500 text-[11px] sticky top-0 z-10">
            <tr>
              <th className="px-3 py-1.5 text-left font-bold w-[130px]">代码 / 名称</th>
              <th className="px-3 py-1.5 text-left font-bold">命中来源与理由</th>
              <th className="px-3 py-1.5 text-left font-bold w-[80px]">到期</th>
              <th className="px-3 py-1.5 text-left font-bold w-[92px]">状态</th>
              <th className="px-3 py-1.5 text-right font-bold w-[110px]">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map((row) => {
              const badge = rowBadge(row);
              const reason = reasonView(row);
              return (
                <tr
                  key={row.symbol}
                  onClick={() => openRow(row)}
                  title="点击查看全部命中来源与理由（我的条目可在此修改）"
                  className="hover:bg-gray-50/70 transition-colors cursor-pointer"
                >
                  <td className="px-3 py-1.5">
                    <div className="font-mono font-bold text-gray-800">{row.symbol}</div>
                    <div className="text-[10px] text-gray-400 truncate max-w-[120px]">
                      {row.name || '—'}
                    </div>
                  </td>
                  <td className="px-3 py-2 align-top">
                    {/* 理由**不截断**：这一列存在的意义就是让人读完再决定买不买，
                        截成一行的理由等于把判断依据藏起来（悬停才看得到 = 看不见）。 */}
                    {reason.mine && (
                      <div className="flex items-start gap-1.5 mb-1">
                        <span
                          className={`shrink-0 text-[9px] font-bold px-1 py-[1px] rounded border ${
                            reason.mine.action === 'block'
                              ? 'bg-rose-50 text-rose-600 border-rose-200'
                              : 'bg-emerald-50 text-emerald-600 border-emerald-200'
                          }`}
                        >
                          {reason.mine.action === 'block' ? '我的理由' : '放行理由'}
                        </span>
                        <span
                          className={`text-gray-800 whitespace-pre-wrap break-words leading-snug min-w-0 ${
                            reason.mine.expired ? 'line-through opacity-60' : ''
                          }`}
                        >
                          {reason.mine.text}
                        </span>
                      </div>
                    )}
                    <div
                      className={`whitespace-pre-wrap break-words leading-snug ${
                        reason.mine ? 'text-gray-400 text-[11px]' : 'text-gray-600'
                      }`}
                    >
                      {reason.machine}
                    </div>
                    <div className="flex gap-1 mt-1 flex-wrap">
                      {(row.source_labels?.length ? row.source_labels : row.sources).map((s) => (
                        <span
                          key={s}
                          className="text-[9px] px-1 py-[1px] rounded bg-slate-100 text-slate-500 font-bold"
                        >
                          {s}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-3 py-1.5 font-mono text-[11px] text-gray-500">
                    {row.expire || '永久'}
                  </td>
                  <td className="px-3 py-1.5">
                    <span
                      className={`text-[10px] font-bold px-1.5 py-0.5 rounded border ${badge.cls}`}
                      title={badge.title}
                    >
                      {badge.label}
                    </span>
                  </td>
                  <td className="px-3 py-1.5 text-right">
                    {/* 按钮必须 stopPropagation：整行点击是「打开详情」，点到按钮上却
                        弹出详情的弹窗会盖掉他真正想做的那个动作 */}
                    <div className="flex justify-end gap-1">
                      {rowActions(row).map((action) => (
                        <button
                          key={action.kind}
                          disabled={busy === row.symbol}
                          onClick={(e) => {
                            e.stopPropagation();
                            runAction(row, action);
                          }}
                          className={`flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-bold border transition-colors disabled:opacity-50 ${
                            action.kind === 'unallow' || action.kind === 'delete'
                              ? 'bg-gray-50 text-gray-500 border-gray-200 hover:bg-gray-100'
                              : action.kind === 'edit'
                                ? 'bg-sky-50 text-sky-600 border-sky-100 hover:bg-sky-100'
                                : 'bg-emerald-50 text-emerald-600 border-emerald-100 hover:bg-emerald-100'
                          }`}
                        >
                          {action.kind === 'delete' ? (
                            <Trash2 size={10} />
                          ) : action.kind === 'unallow' ? (
                            <Undo2 size={10} />
                          ) : action.kind === 'edit' ? (
                            <Pencil size={10} />
                          ) : (
                            <Check size={10} />
                          )}
                          {action.label}
                        </button>
                      ))}
                    </div>
                  </td>
                </tr>
              );
            })}
            {!rows.length && (
              <tr>
                <td colSpan={5} className="px-3 py-8 text-center text-gray-400 text-[12px]">
                  {loading ? '加载中…' : summary.imported ? '没有匹配的条目' : '名单未导入（见上方提示）'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* 分页 */}
      {pages > 1 && (
        <div className="px-4 py-2 flex items-center justify-center gap-2 shrink-0 border-t border-gray-100">
          {/* 分页用传值更新而非 `setPage(p => ...)`：本仓 electron 的 tsc 环境下
              函数式 setter 的类型会被简化掉函数的那个分支，一律报 TS2345。 */}
          <button
            disabled={page <= 1}
            onClick={() => setPage(Math.max(1, page - 1))}
            className="px-2 py-0.5 rounded border border-gray-200 text-[11px] text-gray-600 disabled:opacity-40"
          >
            上一页
          </button>
          <span className="text-[11px] text-gray-500 font-mono">
            {page} / {pages}
          </span>
          <button
            disabled={page >= pages}
            onClick={() => setPage(Math.min(pages, page + 1))}
            className="px-2 py-0.5 rounded border border-gray-200 text-[11px] text-gray-600 disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      )}

      <EntryModal
        target={addOpen ? { mode: 'add' } : entry}
        onClose={() => {
          setAddOpen(false);
          setEntry(null);
        }}
        onSaved={async () => {
          setAddOpen(false);
          setEntry(null);
          await load();
        }}
      />
    </section>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone: 'slate' | 'rose' | 'amber' | 'emerald' }) {
  const tones = {
    slate: 'bg-slate-50 border-slate-200 text-slate-600',
    rose: 'bg-rose-50 border-rose-100 text-rose-600',
    amber: 'bg-amber-50 border-amber-100 text-amber-600',
    emerald: 'bg-emerald-50 border-emerald-100 text-emerald-600',
  } as const;
  return (
    <span className={`flex items-baseline gap-1 rounded-lg border px-2 py-0.5 ${tones[tone]}`}>
      <span className="text-[10px] font-semibold opacity-70">{label}</span>
      <span className="text-[13px] font-bold font-mono tabular-nums">{value}</span>
    </span>
  );
}

/** 三个入口共用一张表单：新增排除 / 改我自己那条 / 放行。 */
export type EntryTarget =
  | { mode: 'add'; row?: BlacklistRow }
  | { mode: 'edit'; row: BlacklistRow }
  | { mode: 'allow'; row: BlacklistRow };

const DEFAULT_BLOCK_REASON = '个人中心手工加入';
const DEFAULT_ALLOW_REASON = '个人中心手工放行';

/**
 * 新增 / 编辑手工条目。
 *
 * 三种模式共用一张表单是刻意的：**理由字段的语义在三种模式下是同一个**（「我为什么
 * 这么决定」），拆成三张表单只会让占位符与提示语各自漂移。
 *
 * ``edit`` 模式保留原有的 ``action``（block 仍是 block、allow 仍是 allow）——
 * 改理由**绝不能**顺手改方向，那会把「改个错别字」变成「反手拉黑」。
 */
function EntryModal({
  target,
  onClose,
  onSaved,
}: {
  target: EntryTarget | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const mode = target?.mode ?? 'add';
  const row = target?.row ?? null;
  const [symbol, setSymbol] = useState('');
  const [reason, setReason] = useState('');
  const [expire, setExpire] = useState('');
  const [saving, setSaving] = useState(false);

  // 每次打开都用目标行的现值预填：编辑时看到的是「当前这条」，不是空表单
  useEffect(() => {
    if (!target) return;
    setSymbol(row?.symbol ?? '');
    setReason(row?.manual?.reason ?? '');
    setExpire(row?.manual?.expire ?? '');
  }, [target, row]);

  if (!target) return null;

  const isEdit = mode === 'edit';
  const isAllow = mode === 'allow';
  const action: 'block' | 'allow' = isAllow
    ? 'allow'
    : isEdit
      ? (row?.manual?.action ?? 'block')
      : 'block';

  const copy = {
    add: {
      title: '加入交易黑名单',
      ok: '加入黑名单',
      reasonLabel: '加入原因',
      reasonHint: '会显示在候选页的风险徽章与推送预检里',
      reasonPlaceholder: '例如：基本面恶化，本人不买入',
      fallback: DEFAULT_BLOCK_REASON,
      done: '已加入交易黑名单，候选信号将默认排除该股票',
    },
    edit: {
      title: '修改我的理由',
      ok: '保存',
      reasonLabel: action === 'block' ? '排除原因' : '放行原因',
      reasonHint: '只改理由与到期日，不会改变排除/放行的方向',
      reasonPlaceholder: '写清楚为什么',
      fallback: action === 'block' ? DEFAULT_BLOCK_REASON : DEFAULT_ALLOW_REASON,
      done: '已保存',
    },
    allow: {
      title: '例外放行',
      ok: '确认放行',
      reasonLabel: '放行原因',
      reasonHint: '必填——三个月后你会需要它来解释这条放行',
      reasonPlaceholder: '例如：已完成重组，基本面改善',
      fallback: DEFAULT_ALLOW_REASON,
      done: '已放行，候选列表不再排除该股票',
    },
  }[mode];

  // 放行必须写理由：放行是把一只票**从风险闸后面放进来**，是所有操作里最需要留痕的那个。
  // 排除可以偷懒用缺省理由（后果只是多排一只），放行不行。
  const reasonRequired = isAllow;

  const submit = async () => {
    if (!symbol.trim()) {
      message.warning('请输入股票代码');
      return;
    }
    if (reasonRequired && !reason.trim()) {
      message.warning('请填写放行原因');
      return;
    }
    setSaving(true);
    try {
      await tradingBlacklistService.upsert({
        symbol: symbol.trim(),
        action,
        reason: reason.trim() || copy.fallback,
        expire: expire.trim() || null,
      });
      message.success(copy.done);
      onSaved();
    } catch (err) {
      message.error(`保存失败：${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  const tone = action === 'allow' ? 'text-emerald-600' : 'text-rose-600';

  return (
    <Modal
      title={
        <div className={`flex items-center gap-2 ${tone}`}>
          {action === 'allow' ? <Check size={16} /> : <ShieldAlert size={16} />}
          <span>{copy.title}</span>
          {row && <span className="font-mono text-[12px] text-gray-500">{row.symbol}</span>}
          {row?.name && <span className="text-[12px] text-gray-500">{row.name}</span>}
        </div>
      }
      open
      onCancel={onClose}
      onOk={submit}
      okText={copy.ok}
      cancelText="取消"
      confirmLoading={saving}
      width={460}
      destroyOnClose
    >
      <div className="flex flex-col gap-3 py-1">
        {/* 只读的命中详情：机器那一层改不动（下次导入整份覆盖），但必须先看得见全文，
            否则用户是在「不知道为什么被排除」的状态下改自己那条理由。 */}
        {row && (
          <div className="rounded-xl border border-gray-200 bg-gray-50 px-3 py-2">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-[11px] font-bold text-gray-600">命中来源与理由</span>
              {(row.source_labels?.length ? row.source_labels : row.sources).map((s) => (
                <span
                  key={s}
                  className="text-[9px] px-1 py-[1px] rounded bg-slate-200/70 text-slate-600 font-bold"
                >
                  {s}
                </span>
              ))}
              {row.expired && (
                <span className="text-[9px] px-1 py-[1px] rounded bg-slate-100 text-slate-400 font-bold">
                  窗口已结束
                </span>
              )}
            </div>
            <div className="mt-1.5 max-h-[168px] overflow-y-auto custom-scrollbar text-[11px] text-gray-700 whitespace-pre-wrap break-words leading-relaxed">
              {row.reason || '（无文字理由，仅命中来源）'}
            </div>
            {(() => {
              const lines = sourceLines(row);
              if (lines.length < 2) return null;
              return (
                <div className="mt-2 pt-2 border-t border-gray-200 flex flex-col gap-1">
                  <span className="text-[10px] font-bold text-gray-400">逐源明细</span>
                  {lines.map((l) => (
                    <div key={l.label} className="text-[10px] text-gray-500 leading-snug">
                      <span className="font-bold text-gray-600">{l.label}</span>
                      {l.reason ? `：${l.reason}` : '：—'}
                    </div>
                  ))}
                </div>
              );
            })()}
            {row.flags?.length ? (
              <div className="mt-1.5 flex gap-1 flex-wrap">
                {row.flags.map((f) => (
                  <span
                    key={f}
                    className="text-[9px] px-1 py-[1px] rounded bg-white border border-gray-200 text-gray-500 font-mono"
                  >
                    {f}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        )}
        <Field label="股票代码" hint="支持 600036 / SH600036 / 600036.SH 三种写法">
          <Input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            placeholder="600036"
            className="font-mono"
            disabled={isEdit || isAllow}
          />
        </Field>
        <Field label={copy.reasonLabel} hint={copy.reasonHint}>
          {/* 用 textarea 而不是单行 Input：理由经常要写两句（「为什么排除」+「什么条件下解除」），
              单行框在视觉上就暗示「别写长」，而这一列恰恰是要被读的。 */}
          <Input.TextArea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={copy.reasonPlaceholder}
            maxLength={200}
            showCount
            autoSize={{ minRows: 2, maxRows: 4 }}
          />
        </Field>
        <Field
          label="到期日"
          hint={
            action === 'allow'
              ? '留空 = 永久放行；填了到期日后过期自动恢复为拦买'
              : '留空 = 永久排除；填了到期日后过期自动恢复'
          }
        >
          <Input
            type="date"
            value={expire}
            onChange={(e) => setExpire(e.target.value)}
            className="font-mono"
          />
        </Field>
        <p className="text-[11px] text-gray-400 leading-relaxed">
          手工条目与系统名单分层保存，每日导入系统名单时**不会**被覆盖。
        </p>
      </div>
    </Modal>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[12px] font-bold text-gray-700">{label}</span>
      {children}
      {hint && <span className="text-[10px] text-gray-400">{hint}</span>}
    </label>
  );
}
