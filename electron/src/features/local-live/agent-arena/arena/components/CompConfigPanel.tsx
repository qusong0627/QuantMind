import { useEffect, useState } from 'react';
import { CompMode, CompSelection, MarketId, fetchCompConfig, saveCompConfig } from '../api/client';
import { logoOf, modelColor, shortName } from './ModelCard';
import { asUpdater } from '../reactCompat';

const MARKET_LABEL: Record<MarketId, string> = {
  cn: 'A 股（T+1 · 涨跌停）',
  hk: '港股（T+0 · 可做空）',
  us: '美股（T+0 · 可做空）',
};

/** 比赛配置面板（实况页右侧 tab）：
 *  每个模型一行，4 种分析配置多选开关；选中 N 个 → 分析引擎按 N 个配置各做一轮分析。
 *  按市场分区：cn/hk 各自维护模型多选，注入的系统提示词按市场交易规则调整（A股 T+1 / 港股 T+0 可做空）。
 *  保存后写后端 configs/comp-config.json（实盘分析脚本与回放 prompt 共用）。 */
export default function CompConfigPanel({ models, market }: { models: string[]; market: MarketId }) {
  const [catalog, setCatalog] = useState<CompMode[]>([]);
  const [draft, setDraft] = useState<Record<string, Set<string>>>({});
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** 展开查看完整提示词的配置 id（默认只显示 2 行预览） */
  const [openPrompt, setOpenPrompt] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchCompConfig(market)
      .then(({ catalog: cat, selection }) => {
        if (!alive) return;
        setCatalog(cat);
        setDraft(
          Object.fromEntries(
            models.map((m) => [m, new Set(selection[m] ?? [])]),
          ),
        );
      })
      .catch(() => alive && setError('读取配置失败'));
    return () => {
      alive = false;
    };
  }, [models.join('|'), market]);

  const toggle = (model: string, id: string) =>
    setDraft(asUpdater((prev) => {
      const next = new Set(prev[model] ?? []);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return { ...prev, [model]: next };
    }));

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const selection: CompSelection = Object.fromEntries(
        Object.entries(draft).map(([m, ids]) => [m, [...ids]]),
      );
      await saveCompConfig(market, selection);
      setSavedAt(Date.now());
    } catch {
      setError('保存失败，请重试');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="comp-body">
      <div className="comp-market-badge">{MARKET_LABEL[market]}</div>
      {catalog.length === 0 && !error && <div className="empty-state">加载配置中…</div>}
      {error && <div className="comp-error">{error}</div>}
      {models.map((model) => {
        const picked = catalog.filter((c) => draft[model]?.has(c.id));
        return (
          <div className="comp-model" key={model}>
            <div className="comp-model-head">
              <span className="comp-model-logo">{logoOf(model)}</span>
              <span className="comp-model-name" style={{ color: modelColor(model) }}>
                {shortName(model)}
              </span>
              <span className="comp-model-count">{draft[model]?.size ?? 0} 个配置</span>
            </div>
            <div className="comp-model-switches">
              {catalog.map((c) => {
                const on = draft[model]?.has(c.id) ?? false;
                return (
                  <button
                    key={c.id}
                    type="button"
                    className={`comp-switch ${on ? 'on' : ''}`}
                    onClick={() => toggle(model, c.id)}
                    aria-pressed={on}
                  >
                    <span className="comp-switch-box">{on ? '✓' : ''}</span>
                    {c.name}
                  </button>
                );
              })}
            </div>
            {picked.length > 0 && (
              <div className="comp-model-desc">
                <div className="comp-desc-head">
                  选中配置的提示词（每轮按下列顺序各跑一次，互不合并）
                </div>
                {picked.map((c) => {
                  const open = openPrompt === `${model}:${c.id}`;
                  const text = c.prompt.replace(/\s+/g, ' ').trim();
                  return (
                    <div className="comp-desc-item" key={c.id}>
                      <span className="comp-desc-name">{c.name}</span>
                      <span className={`comp-desc-text ${open ? '' : 'clamp'}`}>{text}</span>
                      <button
                        type="button"
                        className="comp-desc-more"
                        onClick={() => setOpenPrompt(open ? null : `${model}:${c.id}`)}
                      >
                        {open ? '收起' : '展开全文'}
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
      <div className="comp-save-row">
        <button type="button" className="comp-save" onClick={save} disabled={saving}>
          {saving ? '保存中…' : '保存配置'}
        </button>
        {savedAt != null && !error && (
          <span className="comp-saved-at">
            ✅ 已保存{' '}
            {new Date(savedAt).toLocaleTimeString('zh-CN', { hour12: false })}，下一轮分析生效
          </span>
        )}
      </div>
      <div className="comp-note">
        选中 N 个配置 → 每次分析将按 N 个配置各执行一轮独立分析（实盘盘中 + 回放均生效）。
        未选择 = 基线模式单轮。配置按市场分区，互不影响。
      </div>
    </div>
  );
}
