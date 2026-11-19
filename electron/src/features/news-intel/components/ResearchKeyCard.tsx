/** 右侧「研究口径」要点卡：只放三条可执行规则，完整内容进抽屉。 */

interface ResearchKeyCardProps {
  onOpen?: () => void;
}

export function ResearchKeyCard({ onOpen }: ResearchKeyCardProps) {
  return (
    <div className="ni-keycard">
      <div className="ni-kicker">研究口径 · 重点</div>
      <ul>
        <li>
          <b className="is-risk">P0</b> 硬利空 + pre5&gt;5% → 回避，不抄底
        </li>
        <li>
          <b className="is-caution">P2</b> 利好 + pre5&gt;5% → 不追，不加仓
        </li>
        <li>
          <b className="is-watch">WATCH</b> 软利空 -2~5% → 观察，不追空
        </li>
      </ul>
      {onOpen && (
        <button type="button" className="ni-link-btn" onClick={onOpen}>
          完整口径与证据 →
        </button>
      )}
    </div>
  );
}
