/**
 * 策略管理页面
 */

import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useStrategies, useDeleteStrategy } from '../hooks';
import { Table, Button, Tag, Space, Popconfirm, message, Input, Select, Modal } from 'antd';
import {
  StarOutlined,
  StarFilled,
} from '@ant-design/icons';
import type { StrategyStatus } from '../types';
import { TagSelector } from '../../../components/common/TagSelector';
import { filterValidTags } from '../../../shared/types/strategyTags';

const { Search } = Input;
const { TextArea } = Input;

interface StrategiesPageProps {
  userId: string;
}

import { Plus, ArrowLeftRight as SwapOutlined, Search as SearchIcon, Eye, Trash2, RefreshCw, Edit, AlertTriangle } from 'lucide-react';

const StrategiesPage: React.FC<StrategiesPageProps> = ({ userId }) => {
  const navigate = useNavigate();
  const [deleteModalVisible, setDeleteModalVisible] = useState(false);
  const [strategyToDelete, setStrategyToDelete] = useState<any>(null);

  const {
    strategies,
    total,
    page,
    pageSize,
    isLoading,
    filters,
    handlePageChange,
    handleFilterChange,
    refetch,
  } = useStrategies(userId);

  const { deleteStrategy, deleteStatus } = useDeleteStrategy(userId);

  const handleDeleteClick = (strategy: any) => {
    setStrategyToDelete(strategy);
    setDeleteModalVisible(true);
  };

  const handleConfirmDelete = async () => {
    if (!strategyToDelete) return;

    try {
      await deleteStrategy(strategyToDelete.id);
      message.success('策略已删除');
      refetch();
    } catch (err: any) {
      message.error(err.message || '删除失败');
    } finally {
      setDeleteModalVisible(false);
      setStrategyToDelete(null);
    }
  };

  const handleEdit = (strategy: any) => {
    if (strategy.status === 'repository') {
      Modal.confirm({
        title: '编辑仓库策略',
        icon: <AlertTriangle className="w-5 h-5 text-orange-500" />,
        content: (
          <div className="space-y-2">
            <p>编辑仓库策略将自动降级到草稿状态，需要重新回测验证才能再次晋升到仓库。</p>
            <div className="bg-orange-50 p-3 rounded-lg text-sm">
              <p className="font-semibold text-orange-800">注意事项：</p>
              <ul className="list-disc list-inside text-orange-700 mt-1 space-y-1">
                <li>策略状态将变为"草稿"</li>
                <li>需要重新进行回测验证</li>
                <li>原回测记录将保留作为历史参考</li>
              </ul>
            </div>
          </div>
        ),
        okText: '确认编辑',
        cancelText: '取消',
        onOk: () => {
          message.success(`策略已降级到草稿，正在跳转到 AI-IDE...`);
          navigate(`/ai-ide?strategyId=${strategy.id}`);
        },
      });
    } else if (strategy.status === 'live_trading') {
      message.warning('实盘策略无法编辑，请先停止实盘');
    } else {
      navigate(`/ai-ide?strategyId=${strategy.id}`);
    }
  };

  const handleViewDetail = (strategyId: string) => {
    navigate(`/user-center/strategy/${strategyId}`);
  };

  const getStatusTag = (status: StrategyStatus) => {
    const statusMap: Record<StrategyStatus, { bg: string; text: string; color: string }> = {
      draft: { bg: 'bg-gray-50', color: 'text-gray-500', text: '草稿' },
      repository: { bg: 'bg-blue-50', color: 'text-blue-600', text: '仓库' },
      live_trading: { bg: 'bg-green-50', color: 'text-green-600', text: '实盘中' },
      active: { bg: 'bg-emerald-50', color: 'text-emerald-600', text: '已激活' },
      inactive: { bg: 'bg-slate-50', color: 'text-slate-400', text: '未激活' },
      paused: { bg: 'bg-amber-50', color: 'text-amber-600', text: '已暂停' },
      archived: { bg: 'bg-rose-50', color: 'text-rose-400', text: '已归档' },
      backtesting: { bg: 'bg-blue-50', color: 'text-blue-600', text: '回测中' },
    };
    const config = statusMap[status] || { bg: 'bg-slate-50', color: 'text-slate-400', text: status };
    return (
      <span className={`px-2 py-0.5 rounded-lg text-[10px] font-black uppercase tracking-tighter border border-transparent ${config.bg} ${config.color}`}>
        {config.text}
      </span>
    );
  };

  const columns = [
    {
      title: '策略名称',
      dataIndex: 'name',
      key: 'name',
      width: 240,
      render: (text: string, record: any) => (
        <div className="flex items-center gap-2">
          {record.is_favorite ? (
            <StarFilled className="text-amber-400 text-xs" />
          ) : (
            <StarOutlined className="text-slate-200 text-xs" />
          )}
          <span className="font-bold text-slate-700">{text}</span>
        </div>
      ),
    },
    {
      title: '策略类型',
      dataIndex: 'strategy_type',
      key: 'strategy_type',
      width: 120,
      render: (text: string) => <span className="text-xs font-bold text-slate-400 uppercase tracking-widest">{text}</span>
    },
    {
      title: '当前状态',
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: getStatusTag,
    },
    {
      title: '总收益率',
      dataIndex: ['performance_summary', 'total_return_pct'],
      key: 'return',
      width: 120,
      render: (value: number) => (
        <span className={`text-sm font-black font-mono ${value >= 0 ? 'text-emerald-500' : 'text-rose-500'}`}>
          {value >= 0 ? '+' : ''}{value?.toFixed(2)}%
        </span>
      ),
    },
    {
      title: '夏普比率',
      dataIndex: ['performance_summary', 'sharpe_ratio'],
      key: 'sharpe',
      width: 100,
      render: (value: number) => <span className="text-sm font-bold text-slate-600 font-mono">{value?.toFixed(2) || '-'}</span>,
    },
    {
      title: '回撤',
      dataIndex: ['performance_summary', 'max_drawdown'],
      key: 'drawdown',
      width: 100,
      render: (value: number) => (
        <span className="text-sm font-bold text-rose-400 font-mono">-{Math.abs(value || 0).toFixed(2)}%</span>
      ),
    },
    {
      title: '胜率',
      dataIndex: ['performance_summary', 'win_rate'],
      key: 'winrate',
      width: 100,
      render: (value: number) => <span className="text-sm font-bold text-slate-500 font-mono">{value?.toFixed(2)}%</span>,
    },
    {
      title: '创建于',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (text: string) => <span className="text-[11px] font-black text-slate-300 uppercase tracking-tighter">{new Date(text).toLocaleDateString()}</span>,
    },
    {
      title: '操作',
      key: 'action',
      width: 180,
      fixed: 'right' as const,
      render: (_: any, record: any) => (
        <div className="flex items-center gap-1">
          <button
            onClick={() => handleViewDetail(record.strategy_id)}
            className="p-2 hover:bg-blue-50 text-slate-400 hover:text-blue-500 rounded-xl transition-colors"
            title="查看详情"
          >
            <Eye size={16} />
          </button>
          {record.status !== 'live_trading' && (
            <button
              onClick={() => handleEdit(record)}
              className={`p-2 rounded-xl transition-colors ${record.status === 'repository'
                ? 'hover:bg-orange-50 text-slate-400 hover:text-orange-500'
                : 'hover:bg-purple-50 text-slate-400 hover:text-purple-500'
                }`}
              title={record.status === 'repository' ? '编辑（将降级到草稿）' : '编辑策略'}
            >
              <Edit size={16} />
            </button>
          )}
          {record.status !== 'live_trading' && (
            <button
              onClick={() => handleDeleteClick(record)}
              className="p-2 hover:bg-rose-50 text-slate-400 hover:text-rose-500 rounded-xl transition-colors"
              title="删除策略"
            >
              {deleteStatus === 'loading' ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Trash2 size={16} />}
            </button>
          )}
        </div>
      ),
    },
  ];

  return (
    <div className="strategies-page">
      {/* 筛选器 */}
      <div className="mb-8 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="relative">
            <SearchIcon className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
            <input
              placeholder="搜索策略..."
              className="pl-10 pr-4 py-2 bg-white border border-gray-200 rounded-xl text-sm font-medium focus:ring-4 focus:ring-blue-500/10 outline-none transition-all w-64 shadow-sm"
              onChange={(e) => handleFilterChange({ search: e.target.value })}
            />
          </div>
          <Select
            placeholder="所有状态"
            className="w-36 h-10 rounded-xl custom-select-xl shadow-sm"
            bordered={true}
            allowClear
            value={filters.status}
            onChange={(status: StrategyStatus) => handleFilterChange({ status })}
          >
            <Select.Option value="draft">草稿</Select.Option>
            <Select.Option value="repository">仓库</Select.Option>
            <Select.Option value="live_trading">实盘中</Select.Option>
            <Select.Option value="active">已激活</Select.Option>
            <Select.Option value="inactive">未激活</Select.Option>
            <Select.Option value="paused">已暂停</Select.Option>
            <Select.Option value="archived">已归档</Select.Option>
            <Select.Option value="backtesting">回测中</Select.Option>
          </Select>
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate('/strategy-comparison')}
            className="flex items-center gap-2 px-4 py-2 bg-slate-50 text-slate-600 border border-slate-200 rounded-xl font-bold text-sm hover:bg-slate-100 transition-all"
          >
            <SwapOutlined className="text-xs" />
            <span>策略对比</span>
          </button>
          <button className="flex items-center gap-2 px-6 py-2 bg-gradient-to-r from-blue-500 to-purple-600 text-white rounded-xl font-black text-sm shadow-md hover:shadow-lg transition-all active:scale-95">
            <Plus className="w-4 h-4" />
            <span>创建新策略</span>
          </button>
        </div>
      </div>

      {/* 策略列表 */}
      <div className="bg-white rounded-2xl border border-gray-200 shadow-sm overflow-hidden">
        <Table
          columns={columns}
          dataSource={strategies}
          rowKey="id"
          loading={isLoading}
          scroll={{ x: 1200 }}
          className="custom-modern-table"
          pagination={{
            current: page,
            pageSize,
            total,
            onChange: handlePageChange,
            showSizeChanger: false,
            showTotal: (total) => <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">Total {total} Strategy Records</span>,
          }}
        />
      </div>

      {/* 删除确认对话框 */}
      <Modal
        title={
          <div className="flex items-center gap-2">
            <AlertTriangle className="w-5 h-5 text-red-500" />
            <span>确认删除策略</span>
          </div>
        }
        open={deleteModalVisible}
        onOk={handleConfirmDelete}
        onCancel={() => {
          setDeleteModalVisible(false);
          setStrategyToDelete(null);
        }}
        okText="确认删除"
        cancelText="取消"
        okButtonProps={{ danger: true }}
      >
        {strategyToDelete && (
          <div className="space-y-3">
            <p>确定要删除以下策略吗？</p>
            <div className="bg-gray-50 p-3 rounded-lg">
              <div className="font-semibold text-gray-800 mb-2">{strategyToDelete.name || strategyToDelete.strategy_name}</div>
              <div className="text-sm text-gray-600 space-y-1">
                <div>状态: {getStatusTag(strategyToDelete.status)}</div>
                <div>创建时间: {new Date(strategyToDelete.created_at).toLocaleString('zh-CN')}</div>
                {strategyToDelete.performance_summary && (
                  <div className="mt-2 pt-2 border-t border-gray-200">
                    <div>收益率: {strategyToDelete.performance_summary.total_return_pct?.toFixed(2)}%</div>
                    <div>夏普比率: {strategyToDelete.performance_summary.sharpe_ratio?.toFixed(2)}</div>
                  </div>
                )}
              </div>
            </div>
            <div className="bg-red-50 p-3 rounded-lg text-sm text-red-700">
              <p className="font-semibold">⚠️ 警告</p>
              <p className="mt-1">删除后无法恢复，所有相关数据（包括回测记录）将被永久删除。</p>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default StrategiesPage;
