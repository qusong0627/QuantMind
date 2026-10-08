import { useMemo } from 'react';
import {
  GateParsed,
  ProtoRow,
  ProtoSpec,
  isGateAgent,
  parseGate,
  parseProto,
} from '../utils/newsProtocol';
import './NewsProtocolView.css';

/** 新闻 agent 行式协议的表格化渲染。
 *
 *  原始输出是「| 分隔的填表协议」，直接当散文显示会变成一屏竖线且没法扫读；
 *  这里按字段表拆成标签 + 单元格：代码等宽、判定/力度上色、传导链整行铺开。
 *  拆不出的行（模型偶尔写散文）原样保留，不吞内容。 */

/** 方向色：判定列（利好/利空/中性 或 偏向数值） */
const sentClass = (v: string): string => {
  const t = v.trim();
  if (t.includes('利好')) return 'up';
  if (t.includes('利空')) return 'down';
  const n = Number(t);
  if (Number.isFinite(n) && t !== '') return n > 0 ? 'up' : n < 0 ? 'down' : 'dim';
  return 'dim';
};

/** 数值列：带符号着色（正向红、负向绿，与全站涨跌同口径） */
const numClass = (v: string): string => {
  const n = Number(v.replace(/[^\d.+-]/g, ''));
  if (!Number.isFinite(n) || v === '') return 'dim';
  return n > 0 ? 'up' : n < 0 ? 'down' : 'dim';
};

const GateView = ({ g }: { g: GateParsed }) => {
  const total = g.skip.length + g.macro.length + g.holdings.length;
  return (
    <div className="npx-gate">
      <div className="npx-gate-head">
        {total === 0
          ? '本轮无剔除 · 全部条目保留（默认 micro）'
          : `例外 ${total} 条 · 其余全部保留（默认 micro）`}
      </div>
      <div className="npx-gate-stats">
        <span className="npx-gate-stat npx-chip-skip">剔除 {g.skip.length}</span>
        <span className="npx-gate-stat npx-chip-macro">转宏观 {g.macro.length}</span>
        <span className="npx-gate-stat npx-chip-hold">转持仓 {g.holdings.length}</span>
      </div>
      {(
        [
          ['剔除', g.skip, 'skip'],
          ['转宏观', g.macro, 'macro'],
          ['转持仓', g.holdings, 'hold'],
        ] as const
      ).map(([label, nos, cls]) =>
        nos.length ? (
          <div className="npx-gate-line" key={label}>
            <span className="npx-gate-k">{label}</span>
            <span className="npx-gate-nos">
              {nos.map((n) => (
                <span className={`npx-no npx-no-${cls}`} key={n}>
                  #{n}
                </span>
              ))}
            </span>
          </div>
        ) : null,
      )}
      {g.plain.length > 0 && (
        <pre className="npx-plain">{g.plain.join('\n')}</pre>
      )}
    </div>
  );
};

/** 一条协议行：标签 + 单元格（long 单元格整行铺开） */
const Row = ({ row }: { row: ProtoRow }) => {
  const inline = row.cells.filter((c) => c.kind !== 'long' && c.v);
  const blocks = row.cells.filter((c) => c.kind === 'long' && c.v);
  // 段标签已经说明含义时不再重复（如 C 行标签就叫「置信度」）
  const kOf = (k: string) => (k === row.label ? null : k);
  return (
    <div className={`npx-row npx-row-${row.cls}`}>
      <div className="npx-row-head">
        <span className={`npx-tag npx-tag-${row.cls}`}>{row.label}</span>
        {inline.map((c, i) => (
          <span className="npx-cell" key={`${c.k}-${i}`}>
            {c.kind === 'code' ? (
              <span className="npx-code">{c.v}</span>
            ) : c.kind === 'name' ? (
              <b className="npx-name">{c.v}</b>
            ) : c.kind === 'sent' ? (
              <span className={`npx-sent ${sentClass(c.v)}`}>{c.v}</span>
            ) : c.kind === 'num' ? (
              <span className={`npx-num ${numClass(c.v)}`}>
                {kOf(c.k) && <span className="npx-k">{c.k}</span>}
                {c.v}
              </span>
            ) : (
              <span className="npx-text">
                {kOf(c.k) && <span className="npx-k">{c.k}</span>}
                {c.v}
              </span>
            )}
          </span>
        ))}
      </div>
      {blocks.map((c, i) => (
        <div className="npx-block" key={`${c.k}-${i}`}>
          {kOf(c.k) && <span className="npx-k">{c.k}</span>}
          {c.v}
        </div>
      ))}
    </div>
  );
};

export default function NewsProtocolView({ text, agentId, spec }: { text: string; agentId: string; spec: ProtoSpec }) {
  const isGate = isGateAgent(agentId);
  const gate = useMemo(() => (isGate ? parseGate(text) : null), [isGate, text]);
  const parsed = useMemo(() => (isGate ? null : parseProto(text, spec)), [isGate, text, spec]);

  if (gate) return <GateView g={gate} />;
  if (!parsed) return null;
  const { rows, plain } = parsed;
  return (
    <div className="npx-body">
      {rows.map((r, i) => (
        <Row row={r} key={`${r.token}-${i}`} />
      ))}
      {rows.length === 0 && plain.length === 0 && <p className="npx-empty">本节本轮无输出。</p>}
      {plain.length > 0 && <pre className="npx-plain">{plain.join('\n')}</pre>}
    </div>
  );
}
