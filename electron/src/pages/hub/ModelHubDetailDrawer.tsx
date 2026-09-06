import React from 'react';
import {
  Drawer, Button, Tag, Divider, Row, Col, Tooltip, Alert, Empty
} from 'antd';
import {
  Download, ShieldCheck, Heart, User, Calendar, HardDrive,
  BarChart3, Activity, Layers, CheckCircle2, AlertTriangle, Sparkles
} from 'lucide-react';
import { HubModelItem } from '../../services/modelHubService';

interface ModelHubDetailDrawerProps {
  model: HubModelItem | null;
  open: boolean;
  onClose: () => void;
  onImport: (model: HubModelItem) => void;
  importing?: boolean;
}

export const ModelHubDetailDrawer: React.FC<ModelHubDetailDrawerProps> = ({
  model,
  open,
  onClose,
  onImport,
  importing = false,
}) => {
  if (!model) return null;

  const factorList: string[] = Array.isArray(model.factors_summary)
    ? model.factors_summary
    : (model.factors_summary as any)?.items || [];

  const fmtNum = (v: unknown, d = 2) => typeof v === 'number' && Number.isFinite(v as number) ? (v as number).toFixed(d) : '—';
  const fmtPct = (v: unknown, d = 1) => typeof v === 'number' && Number.isFinite(v as number) ? `${((v as number) * 100).toFixed(d)}%` : '—';
  const formattedSize = (bytes?: number) => {
    if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes <= 0) return '—';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  // 生成详情页高清 SVG 净值曲线
  const renderDetailChart = (curve?: Array<{ date: string; value: number }>) => {
    if (!curve || curve.length < 2) {
      return (
        <div className="h-44 w-full flex items-center justify-center bg-slate-50 rounded-xl border border-slate-100 text-xs text-slate-400">
          暂无历史回测曲线数据
        </div>
      );
    }

    const values = curve.map((p) => p.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const width = 500;
    const height = 150;
    const padding = 12;

    const points = curve
      .map((p, i) => {
        const x = (i / (curve.length - 1)) * (width - padding * 2) + padding;
        const y = height - padding - ((p.value - min) / range) * (height - padding * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');

    const isPositive = values[values.length - 1] >= values[0];
    const strokeColor = isPositive ? '#ef4444' : '#10b981';
    const fillColor = isPositive ? 'rgba(239, 68, 68, 0.12)' : 'rgba(16, 185, 129, 0.12)';
    const areaPoints = `${padding},${height} ${points} ${width - padding},${height}`;

    return (
      <div className="w-full bg-slate-50/70 p-3 rounded-2xl border border-slate-100 relative overflow-hidden">
        <div className="flex justify-between items-center text-xs text-slate-400 mb-2 font-medium">
          <span>起点净值: {values[0].toFixed(2)}</span>
          <span className="font-bold text-slate-700">最新净值: {values[values.length - 1].toFixed(2)}</span>
        </div>
        <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-36">
          <defs>
            <linearGradient id="chartGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={strokeColor} stopOpacity="0.25" />
              <stop offset="100%" stopColor={strokeColor} stopOpacity="0.0" />
            </linearGradient>
          </defs>
          <polygon points={areaPoints} fill="url(#chartGrad)" />
          <polyline
            fill="none"
            stroke={strokeColor}
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
            points={points}
          />
        </svg>
        <div className="flex justify-between items-center text-[10px] text-slate-400 mt-1">
          <span>{curve[0]?.date}</span>
          <span>{curve[curve.length - 1]?.date}</span>
        </div>
      </div>
    );
  };

  return (
    <Drawer
      title={
        <div className="flex items-center gap-2">
          <span className="text-base font-black text-slate-800">{model.name}</span>
          <Tag color="blue" className="!rounded-md font-bold text-xs">{model.algorithm}</Tag>
          {model.is_verified && (
            <Tag color="green" className="!rounded-md font-bold text-xs flex items-center gap-1">
              <ShieldCheck size={12} /> 官方已验真
            </Tag>
          )}
        </div>
      }
      placement="right"
      width={560}
      open={open}
      onClose={onClose}
      footer={
        <div className="flex justify-between items-center px-2 py-1">
          <div className="text-xs text-slate-400">
            大小：{formattedSize(model.file_size_bytes)} · 下载：{model.downloads_count || 0} 次
          </div>
          <Button
            type="primary"
            icon={<Download size={14} />}
            loading={importing}
            className="rounded-xl bg-blue-600 hover:bg-blue-500 font-bold px-5 border-none shadow-sm"
            onClick={() => onImport(model)}
          >
            一键导入为本地模型
          </Button>
        </div>
      }
    >
      <div className="space-y-5 pb-6">
        {/* 作者信息栏 */}
        <div className="flex items-center justify-between bg-slate-50 p-3.5 rounded-2xl border border-slate-100">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-full bg-blue-600 text-white font-black flex items-center justify-center text-sm shadow-sm">
              {model.author_username?.charAt(0)?.toUpperCase() || 'U'}
            </div>
            <div>
              <div className="text-xs font-bold text-slate-800 flex items-center gap-1.5">
                {model.author_username}
              </div>
              <div className="text-[10px] text-slate-400">
                发布于 {model.created_at ? new Date(model.created_at).toLocaleDateString() : '—'}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Tag color="default" className="!rounded-md text-xs">{model.market || 'CN'} 市场</Tag>
            <Tag color="default" className="!rounded-md text-xs">{model.target_horizon || 'T+5'}</Tag>
          </div>
        </div>

        {/* 策略简介 */}
        <div>
          <h5 className="text-xs font-black uppercase text-slate-400 tracking-wider mb-2">策略逻辑与描述</h5>
          <div className="text-xs text-slate-600 leading-relaxed bg-slate-50/50 p-3.5 rounded-2xl border border-slate-100">
            {model.description || '创作者暂未填写详细描述。'}
          </div>
        </div>

        {/* 核心指标看板 */}
        <div>
          <h5 className="text-xs font-black uppercase text-slate-400 tracking-wider mb-2">回测评估指标</h5>
          <div className="grid grid-cols-3 gap-2.5">
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-100 text-center">
              <div className="text-[10px] text-slate-400 font-semibold">夏普比率 (Sharpe)</div>
              <div className="text-base font-black text-slate-800 mt-0.5">
                {fmtNum(model.sharpe_ratio, 2)}
              </div>
            </div>
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-100 text-center">
              <div className="text-[10px] text-slate-400 font-semibold">测试集 IC</div>
              <div className="text-base font-black text-slate-800 mt-0.5">
                {fmtNum(model.test_ic, 3)}
              </div>
            </div>
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-100 text-center">
              <div className="text-[10px] text-slate-400 font-semibold">Rank IC</div>
              <div className="text-base font-black text-slate-800 mt-0.5">
                {fmtNum(model.rank_ic, 3)}
              </div>
            </div>
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-100 text-center">
              <div className="text-[10px] text-slate-400 font-semibold">年化收益率</div>
              <div className="text-sm font-black text-red-600 mt-0.5">
                {fmtPct(model.annual_return, 1)}
              </div>
            </div>
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-100 text-center">
              <div className="text-[10px] text-slate-400 font-semibold">最大回撤</div>
              <div className="text-sm font-black text-slate-700 mt-0.5">
                {fmtPct(model.max_drawdown, 1)}
              </div>
            </div>
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-100 text-center">
              <div className="text-[10px] text-slate-400 font-semibold">卡玛比率 (Calmar)</div>
              <div className="text-sm font-black text-slate-800 mt-0.5">
                {fmtNum(model.calmar_ratio, 2)}
              </div>
            </div>
          </div>
        </div>

        {/* 特征与因子依赖清单 */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <h5 className="text-xs font-black uppercase text-slate-400 tracking-wider">
              依赖特征库 ({factorList.length > 0 ? `${factorList.length} 个因子` : '通用因子'})
            </h5>
            <span className="text-[10px] text-emerald-600 flex items-center gap-1 font-semibold">
              <CheckCircle2 size={12} /> 兼容本地 QuantDB 因子库
            </span>
          </div>

          {factorList.length > 0 ? (
            <div className="flex flex-wrap gap-1.5 max-h-[300px] overflow-y-auto p-3 bg-slate-50 rounded-2xl border border-slate-100 custom-scrollbar">
              {factorList.map((factor, idx) => (
                <Tag key={idx} className="!text-[11px] !px-2 !py-0.5 !rounded-md !bg-white !border-slate-200 text-slate-600 font-mono">
                  {factor}
                </Tag>
              ))}
            </div>
          ) : (
            <div className="text-xs text-slate-400 bg-slate-50 p-3 rounded-xl text-center">
              使用默认量化核心因子集（量价、动量与微观结构）
            </div>
          )}
        </div>

        {/* 免责提示 */}
        <Alert
          type="info"
          showIcon
          className="rounded-xl text-xs"
          message="一键导入说明"
          description="点击导入后，后端将自动下载模型包、解压并注册为本地模型，完成后可在「模型管理 → 我的模型」中直接用于推理，无需手动安装。"
        />
      </div>
    </Drawer>
  );
};
