import React from 'react';
import {
  Sparkles, Download, Heart, ShieldCheck, TrendingUp, TrendingDown,
  Layers, User, Calendar, HardDrive, Eye
} from 'lucide-react';
import { Button, Tag, Tooltip } from 'antd';
import { clsx } from 'clsx';
import { HubModelItem } from '../../services/modelHubService';

interface ModelHubCardProps {
  model: HubModelItem;
  onViewDetail: (model: HubModelItem) => void;
  onImport: (model: HubModelItem) => void;
  onLike?: (modelId: string) => void;
  importing?: boolean;
}

export const ModelHubCard: React.FC<ModelHubCardProps> = ({
  model,
  onViewDetail,
  onImport,
  onLike,
  importing = false,
}) => {
  const fmtNum = (v: unknown, digits = 2) =>
    typeof v === 'number' && Number.isFinite(v) ? (v as number).toFixed(digits) : '—';
  const fmtPct = (v: unknown, digits = 1) =>
    typeof v === 'number' && Number.isFinite(v) ? `${((v as number) * 100).toFixed(digits)}%` : '—';
  const extraMetrics: Record<string, unknown> =
    typeof (model as unknown as Record<string, unknown>).extra_metrics === 'object' &&
    (model as unknown as Record<string, unknown>).extra_metrics !== null
      ? ((model as unknown as Record<string, unknown>).extra_metrics as Record<string, unknown>)
      : {};
  const volatilityRaw =
    (model as unknown as Record<string, unknown>).volatility ??
    extraMetrics.volatility ??
    extraMetrics.annual_volatility ??
    (model as unknown as Record<string, unknown>).annual_volatility;
  const winRateRaw =
    (model as unknown as Record<string, unknown>).win_rate ??
    extraMetrics.win_rate ??
    extraMetrics.winRate;

  const formattedSize = (bytes?: number) => {
    if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes <= 0) return '—';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const getAlgoColor = (algo: string) => {
    switch (algo.toLowerCase()) {
      case 'catboost':
        return 'orange';
      case 'lightgbm':
        return 'blue';
      case 'xgboost':
        return 'purple';
      case 'gru':
      case 'lstm':
        return 'cyan';
      default:
        return 'geekblue';
    }
  };

  const getMarketLabel = (m: string) => {
    switch ((m || '').toUpperCase()) {
      case 'CN': return 'A股';
      case 'HK': return '港股';
      case 'US': return '美股';
      case 'CRYPTO': return '加密';
      case 'FUTURES': return '期货';
      case 'CUSTOM': return '自定义';
      default: return m || 'CN';
    }
  };
  const getMarketColor: Record<string, string> = {
    CN: 'blue', HK: 'volcano', US: 'purple', CRYPTO: 'magenta', FUTURES: 'gold', CUSTOM: 'default',
  };

  return (
    <div className="group relative rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm hover:shadow-md hover:border-blue-400/80 transition-all duration-200 flex flex-col justify-between">
      {/* 顶部标题与创作者 */}
      <div>
        <div className="flex items-start justify-between gap-2 mb-2">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1 mb-1.5">
              <Tag
                color={getMarketColor[(model.market || 'CN').toUpperCase()] || 'blue'}
                className="!text-[10px] !px-1.5 !py-0 !rounded-md font-bold shrink-0"
              >
                {getMarketLabel(model.market)}
              </Tag>
              <Tag color={getAlgoColor(model.algorithm)} className="!text-[10px] !px-1.5 !py-0 !rounded-md font-bold shrink-0">
                {(model.algorithm || '').toLowerCase()}
              </Tag>
              <Tag color="default" className="!text-[10px] !px-1.5 !py-0 !rounded-md shrink-0 font-mono">
                {model.target_horizon || 'T+5'}
              </Tag>
              <Tag color="default" className="!text-[10px] !px-1.5 !py-0 !rounded-md shrink-0">
                {model.target_mode || '—'}
              </Tag>
              {model.is_verified && (
                <Tooltip title="官方已验真策略">
                  <span className="flex items-center gap-0.5 text-[10px] text-blue-600 bg-blue-50 px-1.5 py-0.5 rounded-full font-semibold shrink-0 border border-blue-100">
                    <ShieldCheck size={11} /> 验真
                  </span>
                </Tooltip>
              )}
              {model.factors_summary && (
                <span className="text-[10px] text-slate-400 font-medium flex items-center gap-0.5">
                  <Layers size={10} /> {Array.isArray(model.factors_summary) ? model.factors_summary.length : (model.factors_summary as any)?.count ?? (model.factors_summary as any)?.items?.length ?? '—'} 因子
                </span>
              )}
            </div>
            <h4
              onClick={() => onViewDetail(model)}
              className="text-sm font-bold text-slate-800 group-hover:text-blue-600 transition-colors truncate cursor-pointer"
              title={model.name}
            >
              {model.name}
            </h4>
          </div>

          <button
            onClick={() => onLike?.(model.id)}
            className="text-slate-400 hover:text-red-500 hover:bg-red-50 rounded-lg p-1.5 transition-colors shrink-0"
            title="点赞"
          >
            <Heart size={14} />
          </button>
        </div>

        {/* 策略描述 */}
        <p className="text-xs text-slate-500 line-clamp-2 h-8 leading-relaxed mb-3">
          {model.description || '创作者暂未填写详细描述。'}
        </p>

        {/* 核心指标 4 宫格（0 值正常展示，仅 null/undefined 显示 —） */}
        <div className="grid grid-cols-2 gap-2 bg-slate-50/80 rounded-xl p-2.5 mb-3 border border-slate-100">
          <div>
            <div className="text-[10px] text-slate-400 font-semibold">夏普比率 (Sharpe)</div>
            <div className={clsx("text-sm font-black", typeof model.sharpe_ratio === 'number' && model.sharpe_ratio < 0 && "text-emerald-600")}>
              {fmtNum(model.sharpe_ratio, 2)}
            </div>
          </div>
          <div>
            <div className="text-[10px] text-slate-400 font-semibold">测试集 IC / Rank IC</div>
            <div className="text-sm font-black text-slate-800">
              {fmtNum(model.test_ic, 3)} / {fmtNum(model.rank_ic, 3)}
            </div>
          </div>
          <div>
            <div className="text-[10px] text-slate-400 font-semibold">年化收益率</div>
            <div className={clsx(
              "text-xs font-black",
              typeof model.annual_return === 'number' && model.annual_return > 0 ? "text-red-600" : typeof model.annual_return === 'number' && model.annual_return < 0 ? "text-emerald-600" : "text-slate-700"
            )}>
              {fmtPct(model.annual_return, 1)}
            </div>
          </div>
          <div>
            <div className="text-[10px] text-slate-400 font-semibold">最大回撤</div>
            <div className="text-xs font-black text-slate-700">
              {fmtPct(model.max_drawdown, 1)}
            </div>
          </div>
          <div>
            <div className="text-[10px] text-slate-400 font-semibold">卡玛比率 (Calmar)</div>
            <div className="text-xs font-black text-slate-800">{fmtNum(model.calmar_ratio, 2)}</div>
          </div>
          <div>
            <div className="text-[10px] text-slate-400 font-semibold">
              {typeof winRateRaw === 'number' && Number.isFinite(winRateRaw as number) ? '胜率 (Win Rate)' : '年化波动率'}
            </div>
            <div className="text-xs font-black text-slate-800">
              {typeof winRateRaw === 'number' && Number.isFinite(winRateRaw as number)
                ? fmtPct(winRateRaw as number, 1)
                : fmtPct(volatilityRaw as number, 1)}
            </div>
          </div>
        </div>
      </div>

      {/* 底部作者与操作 */}
      <div>
        <div className="flex items-center justify-between text-[11px] text-slate-400 border-t border-slate-100 pt-2.5 mb-3">
          <div className="flex items-center gap-1.5 truncate max-w-[130px]" title={model.author_username}>
            <div className="w-4 h-4 rounded-full bg-blue-100 text-blue-600 flex items-center justify-center text-[9px] font-black">
              {model.author_username?.charAt(0)?.toUpperCase() || 'U'}
            </div>
            <span className="truncate font-medium text-slate-600">{model.author_username}</span>
          </div>

          <div className="flex items-center gap-2 text-[10px]">
            <span className="flex items-center gap-0.5">
              <Download size={11} /> {model.downloads_count || 0}
            </span>
            <span className="flex items-center gap-0.5">
              <HardDrive size={11} /> {formattedSize(model.file_size_bytes)}
            </span>
          </div>
        </div>

        <div className="flex gap-2">
          <Button
            size="small"
            className="flex-1 rounded-xl font-bold text-xs h-8"
            onClick={() => onViewDetail(model)}
          >
            详情
          </Button>
          <Button
            type="primary"
            size="small"
            icon={<Download size={12} />}
            loading={importing}
            className="flex-1 rounded-xl bg-blue-600 hover:bg-blue-500 font-bold text-xs h-8 border-none shadow-sm"
            onClick={() => onImport(model)}
          >
            一键导入
          </Button>
        </div>
      </div>
    </div>
  );
};
