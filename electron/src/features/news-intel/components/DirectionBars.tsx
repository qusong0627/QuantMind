/** 方向结构条：利好（红）/ 利空（绿）/ 中性（灰）。 */

type DirKey = 'positive' | 'negative' | 'neutral';

interface DirectionBarsProps {
  positive?: number;
  negative?: number;
  neutral?: number;
  height?: number;
  showLabels?: boolean;
  compact?: boolean;
  active?: DirKey | null;
  onSelect?: (key: DirKey) => void;
}

export function DirectionBars({
  positive = 0,
  negative = 0,
  neutral = 0,
  height = 8,
  showLabels = true,
  compact = false,
  active = null,
  onSelect,
}: DirectionBarsProps) {
  const total = positive + negative + neutral;
  const pct = (n: number) => (total ? (n / total) * 100 : 0);
  const label = (key: DirKey, text: string, cls: string) =>
    onSelect ? (
      <button
        type="button"
        className={`ni-dirbar-label ${cls} ${active === key ? 'is-active' : ''}`}
        onClick={() => onSelect(key)}
      >
        {text}
      </button>
    ) : (
      <span className={cls}>{text}</span>
    );
  return (
    <div className={`ni-dirbar ${compact ? 'is-compact' : ''}`}>
      <div className="ni-dirbar-track" style={{ height }}>
        {total > 0 && (
          <>
            <span className="is-up" style={{ width: `${pct(positive)}%` }} />
            <span className="is-down" style={{ width: `${pct(negative)}%` }} />
            <span className="is-flat" style={{ width: `${pct(neutral)}%` }} />
          </>
        )}
      </div>
      {showLabels && (
        <div className="ni-dirbar-labels">
          {label('positive', `利好 ${positive}`, 'is-up')}
          {label('negative', `利空 ${negative}`, 'is-down')}
          {label('neutral', `中性 ${neutral}`, 'is-flat')}
        </div>
      )}
    </div>
  );
}
