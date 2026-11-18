/**
 * 评分/体检徽章（T-FE-14 三处内嵌的统一组件）：
 * 在模型/策略/选股等列表宿主内展示对应对象的**最新评分或体检结论**。
 *
 * 契约：objectType ∈ eval API 枚举；objectId 对齐写侧（model=model_id、
 * strategy_health=策略 id、daily_selection=信号交易日）。无记录 → 渲染「未评分」
 * 灰态（不隐藏、不伪造）。点击跳后台管理·交易核心·评估中心（`/admin/eval`，
 * 2026-11-18 自交易控制台迁入）。
 */

import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { listScores } from '../../features/eval-center/services/evalCenterService';
import { gradeMeta } from '../../features/eval-center/components/evalCenterModel';
import { useAppSelector } from '../../store';

interface EvalScoreBadgeProps {
  objectType: 'model' | 'strategy_health' | 'daily_selection' | 'factor' | 'account' | 'strategy';
  objectId: string;
  /** 前缀文案（如「体检」/「评分」），缺省按类型推断 */
  prefix?: string;
  className?: string;
}

const DEFAULT_PREFIX: Record<EvalScoreBadgeProps['objectType'], string> = {
  model: '评分',
  strategy_health: '体检',
  daily_selection: '评分',
  factor: '评分',
  account: '评分',
  strategy: '评分',
};

export const EvalScoreBadge: React.FC<EvalScoreBadgeProps> = ({
  objectType,
  objectId,
  prefix,
  className,
}) => {
  const navigate = useNavigate();
  // 评估中心迁入后台管理（/admin 需管理员），非管理员点进去只会撞 403 ——
  // 那就保留徽章读数、去掉点击（不是隐藏，读数本身对普通用户仍有意义）。
  const isAdmin = useAppSelector((state) => Boolean(state.auth.user?.is_admin));
  const [row, setRow] = useState<{ grade: string | null; score: number | null; low: boolean; red: string[] } | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!objectId) {
      setLoaded(true);
      return;
    }
    listScores({ objectType, objectId, latestOnly: true, limit: 1 })
      .then((resp) => {
        if (cancelled) return;
        const item = resp?.data?.[0];
        if (item) {
          setRow({
            grade: item.grade,
            score: item.score,
            low: item.low_confidence,
            red: item.red_line_failed || [],
          });
        }
      })
      .catch(() => {
        // 徽章是增强位：拉取失败按"未评分"灰态展示，不打断宿主页
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [objectType, objectId]);

  if (!loaded) {
    return <span className={`text-[10px] text-slate-300 ${className || ''}`}>…</span>;
  }
  if (!row) {
    return (
      <span
        className={`text-[10px] px-1.5 py-0.5 rounded-full border border-slate-200 bg-slate-50 text-slate-400 ${className || ''}`}
        title="暂无评分/体检记录（评分任务与回测体检自动写入）"
      >
        {prefix || DEFAULT_PREFIX[objectType]} 未生成
      </span>
    );
  }
  const meta = gradeMeta(row.grade, row.low);
  const title = [
    `${prefix || DEFAULT_PREFIX[objectType]}：${row.grade || '—'}`,
    row.score !== null ? `${Number(row.score).toFixed(1)} 分` : null,
    row.red.length ? `红线：${row.red.join('、')}` : null,
    isAdmin ? '点击前往后台管理·评估中心下钻' : '评估中心在后台管理（需管理员权限）',
  ]
    .filter(Boolean)
    .join(' · ');

  const body = (
    <>
      {prefix || DEFAULT_PREFIX[objectType]} {row.grade || '—'}
      {meta.isLowConfidence ? ' †' : ''}
      {row.red.length > 0 ? ' ⚠' : ''}
    </>
  );
  const badgeClass = `text-[10px] px-1.5 py-0.5 rounded-full border ${meta.className} ${className || ''}`;

  if (!isAdmin) {
    return (
      <span title={title} className={badgeClass}>
        {body}
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        // 深链带上对象（object/object_id）：落地就选中这枚徽章对应的对象，
        // 而不是让评估中心停在榜单第一行（那会让人以为评分被清空了）。
        navigate(
          `/admin/eval?object=${encodeURIComponent(objectType)}&object_id=${encodeURIComponent(objectId)}`,
        );
      }}
      title={title}
      className={`${badgeClass} cursor-pointer hover:brightness-95`}
    >
      {body}
    </button>
  );
};
