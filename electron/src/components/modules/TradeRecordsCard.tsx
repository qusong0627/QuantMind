import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Card } from '../common/Card';
import { TradeRecordsSkeleton } from '../common/CardSkeletons';
import { useTradeRecords } from '../../hooks/useTradeRecords';
import { useAppSelector } from '../../store';
import { selectCurrentMarket } from '../../store/slices/uiSlice';
import { formatBackendTime, parseBackendTimestamp } from '../../utils/format';

const MARKET_LABELS: Record<string, string> = { CN: 'A股', HK: '港股', US: '美股', CRYPTO: '区块链' };

export const TradeRecordsCard: React.FC = () => {
  const tradingMode = useAppSelector((state) => state.ui.tradingMode);
  const currentMarket = useAppSelector(selectCurrentMarket);
  const { records, loading, isOffline, isFallbackToOrders, isStale, lastUpdatedAt, refresh } = useTradeRecords({
    limit: 8,
    tradingMode,
    autoRefresh: true,
    refreshInterval: 12000,
  });
  const [flashTopId, setFlashTopId] = useState<string | null>(null);
  const [newBadgeId, setNewBadgeId] = useState<string | null>(null);
  const [newBadgeFading, setNewBadgeFading] = useState<boolean>(false);
  const previousTopIdRef = useRef<string | null>(null);
  const newBadgeHoldTimerRef = useRef<number | null>(null);
  const newBadgeFadeTimerRef = useRef<number | null>(null);
  const viewRows = useMemo(() => {
    const rows = records.slice(0, 8);
    const placeholders = Math.max(8 - rows.length, 0);
    return {
      rows,
      placeholders,
    };
  }, [records]);

  useEffect(() => {
    if (newBadgeHoldTimerRef.current !== null) {
      window.clearTimeout(newBadgeHoldTimerRef.current);
      newBadgeHoldTimerRef.current = null;
    }
    if (newBadgeFadeTimerRef.current !== null) {
      window.clearTimeout(newBadgeFadeTimerRef.current);
      newBadgeFadeTimerRef.current = null;
    }

    const currentTopId = viewRows.rows[0]?.id ?? null;
    if (currentTopId && previousTopIdRef.current && currentTopId !== previousTopIdRef.current) {
      setFlashTopId(currentTopId);
      const timer = window.setTimeout(() => setFlashTopId(null), 280);
      setNewBadgeId(currentTopId);
      setNewBadgeFading(false);

      newBadgeHoldTimerRef.current = window.setTimeout(() => {
        setNewBadgeFading(true);
      }, 2000);

      newBadgeFadeTimerRef.current = window.setTimeout(() => {
        setNewBadgeId(null);
        setNewBadgeFading(false);
      }, 2300);

      previousTopIdRef.current = currentTopId;
      return () => {
        window.clearTimeout(timer);
        if (newBadgeHoldTimerRef.current !== null) {
          window.clearTimeout(newBadgeHoldTimerRef.current);
          newBadgeHoldTimerRef.current = null;
        }
        if (newBadgeFadeTimerRef.current !== null) {
          window.clearTimeout(newBadgeFadeTimerRef.current);
          newBadgeFadeTimerRef.current = null;
        }
      };
    }
    previousTopIdRef.current = currentTopId;
    return undefined;
  }, [viewRows.rows]);

  useEffect(() => {
    return () => {
      if (newBadgeHoldTimerRef.current !== null) {
        window.clearTimeout(newBadgeHoldTimerRef.current);
      }
      if (newBadgeFadeTimerRef.current !== null) {
        window.clearTimeout(newBadgeFadeTimerRef.current);
      }
    };
  }, []);

  if (loading && viewRows.rows.length === 0) {
    return <TradeRecordsSkeleton />;
  }

  const getStatusColor = (status: string) => {
    switch (status) {
      case '已成交': return 'text-[var(--success)]';
      case '部分成交': return 'text-[var(--warning)]';
      case '待成交': return 'text-[var(--info)]';
      case '已撤销': return 'text-[var(--neutral)]';
      case '未知状态': return 'text-[var(--warning)]';
      default: return 'text-[var(--neutral)]';
    }
  };

  // 从 ISO 时间字符串中提取简短时间：当天只显示 HH:mm，跨天补 MM-DD，避免昨日记录看着像今天的
  // 日期比较统一用上海时区（与 formatBackendTime 一致），避免浏览器时区边缘错位
  const shYmd = (d: Date) => {
    const parts = new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(d);
    const get = (t: string) => parts.find((p) => p.type === t)?.value || '';
    return `${get('year')}-${get('month')}-${get('day')}`;
  };
  const formatTime = (timeStr: string) => {
    const d = parseBackendTimestamp(timeStr);
    if (!d) return '--';
    const hhmm = formatBackendTime(timeStr, { withSeconds: false });
    if (shYmd(d) === shYmd(new Date())) return hhmm;
    const [, mm, dd] = shYmd(d).split('-');
    return `${mm}-${dd} ${hhmm}`;
  };

  const formatAmount = (value: number) => {
    if (!Number.isFinite(value)) {
      return '--';
    }
    return value.toLocaleString('zh-CN');
  };

  const formatTimestamp = (timeStr: string | null) => {
    return formatBackendTime(timeStr, { withSeconds: true });
  };

  const statusText = () => {
    if (isFallbackToOrders) return '成交接口降级为委托记录';
    if (isOffline && viewRows.rows.length === 0) return '数据更新稍有延迟，正在重连...';
    if (isOffline || isStale) return '数据更新稍有延迟';
    if (viewRows.rows.length === 0) return '暂无交易记录';
    return '自动刷新';
  };

  return (
    <Card title={`实时交易记录 (${MARKET_LABELS[currentMarket] || ''})`} height="100%" background="trade">
      <div className="trade-records-table">
        <div className="trade-records-header">
          <div className="trade-cell time-cell">时间</div>
          <div className="trade-cell action-cell">操作</div>
          <div className="trade-cell symbol-cell trade-header-symbol">股票</div>
          <div className="trade-cell quantity-cell">数量</div>
          <div className="trade-cell status-cell">状态</div>
        </div>

        {/* 数据行 */}
        {viewRows.rows.map((record, index) => (
          <div key={record.id} className={`trade-records-row ${flashTopId === record.id && index === 0 ? 'new-row' : ''}`}>
            <div className="trade-cell time-cell text-[var(--text-tertiary)]" title={record.time}>
              {formatTime(record.time)}
            </div>
            <div
              className={`trade-cell action-cell font-bold ${record.type === '买入' ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'
                }`}
              title={record.type}
            >
              {record.type}
            </div>
            <div className="trade-cell symbol-cell font-bold text-[var(--text-primary)]" title={`${record.name} · ${record.symbol}`}>
              {record.name}
            </div>
            <div className="trade-cell quantity-cell font-bold text-[var(--text-secondary)]" title={`数量 ${record.amount}`}>
              {formatAmount(record.amount)}
            </div>
            <div className={`trade-cell status-cell font-bold ${getStatusColor(record.status)}`} title={record.status}>
              {record.status}
              {index === 0 && newBadgeId === record.id && (
                <span className={`new-badge ${newBadgeFading ? 'is-fading' : ''}`}>NEW</span>
              )}
            </div>
          </div>
        ))}

        {Array.from({ length: viewRows.placeholders }).map((_, index) => (
          <div key={`placeholder-${index}`} className="trade-records-row placeholder-row" aria-hidden="true">
            <div className="trade-cell time-cell text-[var(--text-quaternary)]">--:--</div>
            <div className="trade-cell action-cell text-[var(--text-quaternary)]">--</div>
            <div className="trade-cell symbol-cell text-[var(--text-quaternary)]">--</div>
            <div className="trade-cell quantity-cell text-[var(--text-quaternary)]">--</div>
            <div className="trade-cell status-cell text-[var(--text-quaternary)]">--</div>
          </div>
        ))}
      </div>

      {/* 底部状态提示 */}
      <div className="flex justify-between mt-2 mb-1 px-2 py-1 min-h-[28px] items-center bg-slate-50 border border-slate-100/50 rounded-xl">
        <button
          onClick={refresh}
          className="text-xs px-2 py-1 rounded bg-[var(--bg-card-hover)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
        >
          立即刷新
        </button>
        <div className="text-xs text-[var(--text-tertiary)] flex items-center leading-none">
          {isOffline || isStale ? (
            <>
              <div className="w-2 h-2 bg-[var(--warning, #f59e0b)] rounded-full mr-1"></div>
              {statusText()}
            </>
          ) : (
            <>
              <div className="w-2 h-2 bg-[var(--success)] rounded-full animate-pulse mr-1"></div>
              {statusText()}
            </>
          )}
        </div>
        <div className="text-xs text-[var(--text-quaternary)] leading-none">
          最后更新 {formatTimestamp(lastUpdatedAt)}
        </div>
      </div>

      <style>{`
        .trade-records-table {
          font-family: 'Noto Sans SC', 'Source Han Sans SC', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          font-size: 12px;
          line-height: 1.2;
        }

        .trade-records-header,
        .trade-records-row {
          display: grid;
          grid-template-columns: repeat(5, minmax(0, 1fr));
          gap: 0;
          align-items: center;
        }

        .trade-records-header {
          margin-bottom: 2px;
          padding: 0 2px;
          color: var(--text-quaternary);
          font-size: 11px;
          font-weight: 600;
          letter-spacing: 0.04em;
          text-transform: none;
        }

        .trade-records-row {
          background: #f8fafc;
          border: 1px solid #f1f5f9;
          border-radius: 6px;
          margin-bottom: 2px;
          min-height: 28px;
          transition: background-color 180ms ease, border-color 180ms ease;
        }

        .trade-records-row:hover {
          background: #f1f5f9;
          border-color: #e2e8f0;
        }

        .trade-records-row.new-row {
          animation: row-insert 280ms ease-out;
        }

        .trade-records-row.placeholder-row {
          background: #f8fafc;
          opacity: 0.6;
          border-style: dashed;
        }

        @keyframes row-insert {
          0% {
            transform: translateY(-5px);
            box-shadow: inset 0 0 0 1px rgba(59, 130, 246, 0.25);
          }
          100% {
            transform: translateY(0);
            box-shadow: inset 0 0 0 1px rgba(59, 130, 246, 0);
          }
        }

        .trade-cell {
          min-width: 0;
          padding: 3px 0;
          text-align: center;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          display: flex;
          align-items: center;
          justify-content: center;
        }

        .time-cell {
          font-size: 12px;
          font-weight: 500;
          font-variant-numeric: tabular-nums;
        }

        .action-cell {
          text-align: center;
          font-size: 12px;
          font-weight: 600;
        }

        .symbol-cell {
          font-size: 13px;
          font-weight: 600;
        }

        .trade-header-symbol {
          text-align: center;
        }

        .quantity-cell {
          font-size: 12px;
          font-weight: 500;
          font-variant-numeric: tabular-nums;
        }

        .status-cell {
          text-align: center;
          font-size: 12px;
          font-weight: 600;
          position: relative;
          overflow: visible;
          text-overflow: clip;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 4px;
        }

        .new-badge {
          margin-left: 6px;
          padding: 0 4px;
          border-radius: 999px;
          font-size: 10px;
          line-height: 16px;
          color: #0f4c81;
          background: rgba(147, 220, 255, 0.92);
          border: 1px solid rgba(91, 192, 248, 0.58);
          transition: opacity 300ms ease, transform 300ms ease;
          opacity: 1;
          transform: translateY(0);
          vertical-align: middle;
        }

        .new-badge.is-fading {
          opacity: 0;
          transform: translateY(-1px);
        }
      `}</style>
    </Card>
  );
};
