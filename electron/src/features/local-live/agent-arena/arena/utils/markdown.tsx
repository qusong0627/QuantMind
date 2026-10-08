import { ReactNode } from 'react';

/**
 * 轻量行内 markdown 渲染（不引库）：
 *   `**粗体**` → <strong>，`` `代码` `` → <code>，`*斜体*` → <em>
 *   `### 标题`（1~6 级）→ 去掉 # 前缀渲染为加粗（模型输出里的 ### 不再裸奔）
 * 换行保留（配合 white-space: pre-wrap 的容器）。
 */
function renderParts(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*)/g).map((part, j) => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={j}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code key={j} style={{ background: '#f0f0f0', border: '1px solid #ddd', padding: '0 3px', fontSize: '0.92em' }}>
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith('*') && part.endsWith('*') && !part.startsWith('**') && part.length > 2) {
      return <em key={j}>{part.slice(1, -1)}</em>;
    }
    // 未成对闭合的 **（截断/模型输出不规范）不渲染，直接剥掉符号防裸奔
    return part.replace(/\*\*/g, '');
  });
}

export function renderInline(text: string): ReactNode[] {
  const lines = text.split('\n');
  return lines.map((line, i) => {
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    const body = heading ? heading[1] : line;
    return (
      <span key={i}>
        {heading ? <strong>{renderParts(body)}</strong> : renderParts(body)}
        {i < lines.length - 1 ? '\n' : null}
      </span>
    );
  });
}

/**
 * 块级 markdown 渲染（用于思考链/提示词长文，排版结构化）：
 *   `### 标题` → 黑底白字小节头；`- ` 连续项 → <ul> 列表；
 *   `⚠️`/`【】` 开头行 → 警示条（黄底左竖条）；普通行 → 段落。
 * 行内 `**粗体**`/`code`/`*斜体*` 沿用 renderParts。
 */
export function renderMarkdown(text: string): ReactNode[] {
  const lines = text.split('\n');
  const out: ReactNode[] = [];
  let list: string[] | null = null;

  const flushList = (key: number) => {
    if (list) {
      out.push(
        <ul key={key} className="mc-md-list">
          {list.map((item, j) => (
            <li key={j}>{renderParts(item)}</li>
          ))}
        </ul>
      );
      list = null;
    }
  };

  lines.forEach((line, i) => {
    const trimmed = line.trim();
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    const bullet = trimmed.match(/^[-•]\s+(.*)$/);
    if (heading) {
      flushList(i);
      const level = heading[0].match(/^#+/)?.[0].length ?? 3;
      out.push(
        <div key={i} className={`mc-md-h${Math.min(level, 4)}`}>
          {renderParts(heading[1])}
        </div>
      );
    } else if (bullet) {
      (list ??= []).push(bullet[1]);
    } else if (trimmed === '') {
      flushList(i);
    } else if (trimmed.startsWith('⚠️') || trimmed.startsWith('【')) {
      flushList(i);
      out.push(
        <div key={i} className="mc-md-warn">
          {renderParts(trimmed)}
        </div>
      );
    } else {
      flushList(i);
      out.push(
        <div key={i} className="mc-md-p">
          {renderParts(line)}
        </div>
      );
    }
  });
  flushList(lines.length);
  return out;
}
