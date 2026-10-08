import { useMemo } from 'react';
import { fetchLogs, LogLine } from '../api/client';
import { usePolling } from '../hooks/usePolling';
import ChatStream from './ChatStream';
import './NewsAgentChat.css';

/** 新闻 Agent 阵容（scripts/news_brief.py 落盘 data/agent_data_astock/{id}/log/）。
 *  展示顺序 = 决策链顺序的倒序（主编结论最先看）。 */
export const NEWS_AGENTS = [
  { id: 'news-chief', cn: '新闻主编' },
  { id: 'news-review', cn: '晚间复盘' },
  { id: 'news-holdings', cn: '持仓情报' },
  { id: 'news-macro', cn: '宏观政策' },
  { id: 'news-micro', cn: '板块个股' },
  { id: 'news-gate', cn: '新闻门卫' },
];

/** 新闻 tab —— 各新闻 agent 的分析对话（与「模型对话」同款渲染，区别于文章流）。
 *  筛选由 Live 筛选栏（filter-select）控制：agent='all' = 混合时间流，单选 = 只看该段。
 *  2 分钟轮询 + 只取最近 N 个回合：news-gate/news-micro 全量已到 800KB/agent，
 *  六段并行全量拉取一次 ~1.7MB（2026-09-08 卡顿治理）。 */
const NEWS_LOG_LIMIT = 20;
const NEWS_POLL_MS = 120000;

export default function NewsAgentChat({ agent = 'all' }: { agent?: string }) {
  const sel = agent;
  const ids = sel === 'all' ? NEWS_AGENTS.map((a) => a.id) : [sel];
  const logs = usePolling<LogLine[][]>(
    () => Promise.all(ids.map((id) => fetchLogs(id, 'cn', NEWS_LOG_LIMIT).catch(() => [] as LogLine[]))),
    [sel],
    NEWS_POLL_MS,
  );

  const agents = useMemo(() => {
    const data = logs.data ?? [];
    return NEWS_AGENTS.filter((a) => sel === 'all' || a.id === sel)
      .map((a, i) => ({
        name: a.cn,
        id: a.id,
        // 存储层 signature 是英文 id，展示层统一中文化（用户口径：界面上不出现英文 agent 名）
        lines: (data[i] ?? []).map((ln) => ({ ...ln, signature: a.cn })),
      }))
      .filter((a) => sel !== 'all' || a.lines.length > 0);
  }, [logs.data, sel]);

  return (
    <div className="news-agent-chat">
      {agents.length === 0 ? (
        <div className="empty-state">
          暂无新闻 agent 对话。cron 触发点：北京 09:25 / 盘中每小时（整点前 5 分）/ 22:00；
          也可点上方「⚡ 立即分析」
        </div>
      ) : (
        <ChatStream agents={agents} />
      )}
    </div>
  );
}
