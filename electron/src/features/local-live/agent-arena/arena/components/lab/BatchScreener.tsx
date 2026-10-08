/** 单策略选股：选定一个策略 → 全池标的按该策略的回测表现排序，挑最适合它的股票。
 *
 * 与「策略排行」互补：排行回答「哪个策略平均最强」，选股回答「这个策略该买哪几只」。
 */
import { useEffect, useState } from 'react';
import { fetchLabBatchRun, type LabBatchRow } from '../../api/client';
import { asUpdater } from '../../reactCompat';

const SORTS: { id: string; label: string }[] = [
  { id: 'net', label: '净收益' },
  { id: 'sharpe', label: '夏普' },
  { id: 'profit_factor', label: '盈亏比' },
  { id: 'dd', label: '回撤最小' },
  { id: 'trades', label: '成交笔数' },
];

const fmt = (v: number | null, d = 1, suffix = '') =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : `${v.toFixed(d)}${suffix}`;

const fmtPct = (v: number | null, d = 1) =>
  v === null || v === undefined || Number.isNaN(v)
    ? '—'
    : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`;

const tone = (v: number | null) => (v === null ? '' : v >= 0 ? 'up' : 'down');

const median = (xs: number[]) => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

export default function BatchScreener({
  runId,
  strategies,
  onPickSymbol,
}: {
  runId: string;
  strategies: { id: string; name: string }[];
  onPickSymbol: (code: string) => void;
}) {
  const [sid, setSid] = useState('');
  const [sort, setSort] = useState('net');
  const [rows, setRows] = useState<LabBatchRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    // 换批次后当前策略可能不在新批次里 → 退回第一个，否则下拉框空值、表格空转
    setSid(asUpdater((cur) =>
      strategies.some((s) => s.id === cur) ? cur : (strategies[0]?.id ?? ''),
    ));
  }, [strategies]);

  useEffect(() => {
    if (!runId || !sid) return;
    setBusy(true);
    setErr('');
    fetchLabBatchRun(runId, sid, sort, 500)
      .then((d) => setRows(d.rows ?? []))
      .catch(() => {
        setRows([]);
        setErr('选股结果读取失败');
      })
      .finally(() => setBusy(false));
  }, [runId, sid, sort]);

  const winners = rows.filter(
    (r) => r.net_pct !== null && r.buy_hold_pct !== null && r.net_pct > r.buy_hold_pct,
  ).length;

  // 结论带：这策略在全池的典型表现（中位数比均值抗极值，更接近"随便买一只"的体验）
  const withNet = rows.filter((r) => r.net_pct !== null);
  const medNet = median(withNet.map((r) => r.net_pct as number));
  const medExcess = median(
    withNet
      .filter((r) => r.buy_hold_pct !== null)
      .map((r) => (r.net_pct as number) - (r.buy_hold_pct as number)),
  );
  const best = withNet.reduce<LabBatchRow | null>(
    (a, b) => (a === null || (b.net_pct ?? -Infinity) > (a.net_pct ?? -Infinity) ? b : a),
    null,
  );
  const winPct = rows.length ? (winners / rows.length) * 100 : null;

  return (
    <div className="lab-batch-body">
      <div className="lab-screen-bar">
        <label>
          <span>策略</span>
          <select value={sid} onChange={(e) => setSid(e.target.value)}>
            {strategies.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>排序</span>
          <select value={sort} onChange={(e) => setSort(e.target.value)}>
            {SORTS.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        <div className="lab-screen-stat">
          {busy ? '计算中…' : `${rows.length} 只 · 跑赢买入持有 ${winners} 只`}
        </div>
      </div>

      {err && <div className="lab-err">{err}</div>}

      {rows.length > 0 && (
        <div className="lab-hero">
          <div className="lab-hero-cell">
            <div className="lab-hero-k">跑赢买入持有</div>
            <div className="lab-hero-v">
              {winners} / {rows.length} 只
            </div>
            <div className="lab-hero-s">
              {winPct === null ? '—' : `${winPct.toFixed(1)}% 的标的里择时打得过躺着`}
            </div>
          </div>
          <div className="lab-hero-cell">
            <div className="lab-hero-k">中位策略收益</div>
            <div className={`lab-hero-v ${tone(medNet)}`}>{fmtPct(medNet)}</div>
            <div className="lab-hero-s">一半标的比它好、一半比它差</div>
          </div>
          <div className="lab-hero-cell">
            <div className="lab-hero-k">中位超额</div>
            <div className={`lab-hero-v ${tone(medExcess)}`}>{fmtPct(medExcess)}</div>
            <div className="lab-hero-s">策略收益 − 买入持有</div>
          </div>
          <div className="lab-hero-cell">
            <div className="lab-hero-k">全池最赚</div>
            <div className="lab-hero-v">{best ? best.name || best.symbol : '—'}</div>
            <div className={`lab-hero-s ${tone(best?.net_pct ?? null)}`}>
              {best ? `${fmtPct(best.net_pct)} · ${best.symbol}` : ''}
            </div>
          </div>
        </div>
      )}

      <div className="lab-table-wrap lab-table-tall">
        <table className="lab-rank">
          <thead>
            <tr>
              <th>#</th>
              <th>标的</th>
              <th className="num">策略收益</th>
              <th className="num">买入持有</th>
              <th className="num">超额</th>
              <th className="num">回撤</th>
              <th className="num">夏普</th>
              <th className="num">盈亏比</th>
              <th className="num">胜率</th>
              <th className="num">笔数</th>
              <th className="num">区间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const excess =
                r.net_pct !== null && r.buy_hold_pct !== null ? r.net_pct - r.buy_hold_pct : null;
              return (
                <tr key={r.symbol} onClick={() => onPickSymbol(r.symbol)} title="点击查看该标的K线与逐笔">
                  <td className="idx">
                    <span className={`lab-idx ${i === 0 ? 'top' : i < 3 ? 'medal' : ''}`}>{i + 1}</span>
                  </td>
                  <td className="name">
                    {r.name || r.symbol}
                    <span className="code">{r.symbol}</span>
                  </td>
                  <td className={`num ${tone(r.net_pct)}`}>{fmtPct(r.net_pct)}</td>
                  <td className="num">{fmtPct(r.buy_hold_pct)}</td>
                  <td className={`num ${tone(excess)}`}>{fmtPct(excess)}</td>
                  <td className="num down">{fmt(-(r.dd_pct ?? 0), 1, '%')}</td>
                  <td className="num">{fmt(r.sharpe, 2)}</td>
                  <td className="num">{fmt(r.profit_factor, 2)}</td>
                  <td className="num">{fmt(r.win_rate_pct, 1, '%')}</td>
                  <td className="num">{fmt(r.trades, 0)}</td>
                  <td className="num range">
                    {r.start.slice(2, 7)}~{r.end.slice(2, 7)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {!busy && !rows.length && !err && (
        <div className="lab-empty">该批次没有这个策略的结果（跑批时未包含？）</div>
      )}
      <p className="lab-note">
        点任意一行 → 跳到「单标的回测」看这只票的 K 线与逐笔。「超额」= 策略收益 − 买入持有，
        正值说明这段时间里择时比躺着强。
      </p>
    </div>
  );
}
