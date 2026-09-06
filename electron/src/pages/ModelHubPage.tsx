import React, { useState, useEffect, useCallback } from 'react';
import {
  Award, BarChart3, Clock3, Compass, Download, Filter, RefreshCw,
  Search, Sparkles, Target, ArrowLeft
} from 'lucide-react';
import { Input, Button, Select, Tag, Spin, Empty, Pagination, Modal, message } from 'antd';
import { useNavigate } from 'react-router-dom';
import { clsx } from 'clsx';
import { modelHubService, HubModelItem } from '../services/modelHubService';
import { ModelHubCard } from './hub/ModelHubCard';
import { ModelHubDetailDrawer } from './hub/ModelHubDetailDrawer';
import { PublishModelModal } from './hub/PublishModelModal';
import { modelTrainingService, UserModelRecord } from '../services/modelTrainingService';

export const ModelHubPage: React.FC = () => {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [models, setModels] = useState<HubModelItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(12);

  // 筛选与排序
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedMarket, setSelectedMarket] = useState<string>('ALL');
  const [selectedAlgo, setSelectedAlgo] = useState<string>('ALL');
  const [sortBy, setSortBy] = useState<string>('sharpe');

  // 抽屉与弹窗状态
  const [selectedModelForDetail, setSelectedModelForDetail] = useState<HubModelItem | null>(null);
  const [showDetailDrawer, setShowDetailDrawer] = useState(false);
  const [showPublishModal, setShowPublishModal] = useState(false);
  const [userModels, setUserModels] = useState<UserModelRecord[]>([]);
  const [importingId, setImportingId] = useState<string | null>(null);
  // 导入命名确认框：默认填广场公开名（COS 原始命名），允许用户改名
  const [importTarget, setImportTarget] = useState<HubModelItem | null>(null);
  const [importName, setImportName] = useState('');

  // 加载广场模型列表
  const fetchHubModels = useCallback(async () => {
    try {
      setLoading(true);
      setLoadError(null);
      const res = await modelHubService.listModels({
        market: selectedMarket,
        algorithm: selectedAlgo,
        sort_by: sortBy,
        query: searchQuery.trim(),
        page,
        page_size: pageSize,
      });
      setModels(res?.items || []);
      setTotal(res?.total || 0);
    } catch (err: any) {
      console.error('加载广场模型失败:', err);
      setLoadError(err?.message || '无法连接模型广场服务');
      setModels([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [selectedMarket, selectedAlgo, sortBy, searchQuery, page, pageSize]);

  useEffect(() => {
    fetchHubModels();
  }, [fetchHubModels]);

  // 加载本地用户模型供发布弹窗使用
  const loadLocalUserModels = useCallback(async () => {
    try {
      const res = await modelTrainingService.listUserModels(true);
      setUserModels(res?.items || []);
    } catch (e) {
      console.warn('加载本地模型失败:', e);
    }
  }, []);

  useEffect(() => {
    loadLocalUserModels();
  }, [loadLocalUserModels]);

  // 后端一键导入：下载 COS 包 → 解压 → 注册为本地模型，可直接在“我的模型”中推理
  // 命名优先级：弹窗输入名 > 广场公开名 > 包内自带名（后端最终裁决）
  const openImportModal = (model: HubModelItem) => {
    setImportTarget(model);
    setImportName(model.name || '');
  };

  const doImportModel = async (model: HubModelItem, localName?: string) => {
    try {
      setImportingId(model.id);
      const showName = (localName || '').trim() || model.name;
      message.loading({ content: `正在导入 "${showName}" 到本地模型库...`, key: 'hub_import' });

      const result = await modelHubService.importRemoteModel(model.id, (localName || '').trim() || undefined);
      if (!result?.model_id) {
        throw new Error('导入返回缺少 model_id');
      }
      const displayName = (result as any)?.display_name || showName;

      message.success({
        content: result.already_exists
          ? `模型 "${displayName}" 已存在于本地（${result.model_id}）`
          : `已导入为本地模型 ${displayName}（${result.model_id}），可在“模型管理”中查看与推理`,
        key: 'hub_import',
        duration: 5,
      });

      // 刷新下载计数
      fetchHubModels();
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || '未知异常';
      // 共享模型尚未发布或包异常时，兜底提供直接下载
      if (String(detail).includes('下载地址为空') || String(detail).includes('模型包')) {
        try {
          const ticket = await modelHubService.getDownloadTicket(model.id);
          if (ticket?.download_url) {
            window.open(ticket.download_url, '_blank', 'noopener,noreferrer');
            message.info({ content: '已为你打开浏览器直接下载模型包', key: 'hub_import' });
            return;
          }
        } catch {
          // ignore fallback error
        }
      }
      message.error({ content: `导入失败: ${detail}`, key: 'hub_import' });
    } finally {
      setImportingId(null);
    }
  };

  // 处理点赞
  const handleLike = async (modelId: string) => {
    try {
      await modelHubService.likeModel(modelId);
      message.success('点赞成功！');
      fetchHubModels();
    } catch (e) {
      message.info('已记录点赞');
    }
  };

  return (
    <div className="h-full flex flex-col bg-slate-50 overflow-y-auto custom-scrollbar">
      {/* 顶部 Header */}
      <div className="bg-white border-b border-slate-200/80 px-6 pt-7 pb-5 sticky top-0 z-10 shadow-sm">
        <div className="max-w-7xl mx-auto flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <Button
              icon={<ArrowLeft size={16} />}
              className="rounded-xl border-slate-200 hover:text-blue-600 font-bold text-xs"
              onClick={() => navigate('/model-registry')}
            >
              返回模型管理
            </Button>
            <div className="w-9 h-9 rounded-2xl bg-blue-50 text-blue-600 flex items-center justify-center shadow-inner">
              <Compass size={20} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-lg font-black text-slate-800 tracking-tight !mb-0">
                  社区模型广场 (Model Hub)
                </h1>
                <Tag color="blue" className="!rounded-md font-bold text-[10px]">
                  QuantDB 开放生态
                </Tag>
              </div>
              <p className="text-xs text-slate-400 !mb-0 mt-0.5">
                浏览、检索并一键导入社区量化策略模型，或将您的训练成果一键分享给全网开发者
              </p>
            </div>
          </div>

          {/* 右侧搜索与发布操作 */}
          <div className="flex items-center gap-2.5">
            <Input
              prefix={<Search size={14} className="text-slate-400" />}
              placeholder="搜索模型名称、策略关键词或作者..."
              allowClear
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onPressEnter={() => { setPage(1); fetchHubModels(); }}
              className="rounded-xl w-64 text-xs h-9"
            />
            <Button
              type="primary"
              icon={<Sparkles size={14} />}
              className="rounded-xl bg-blue-600 hover:bg-blue-500 font-bold text-xs h-9 border-none shadow-sm flex items-center gap-1"
              onClick={() => setShowPublishModal(true)}
            >
              发布我的模型
            </Button>
            <Button
              icon={<RefreshCw size={14} className={loading ? 'animate-spin' : ''} />}
              className="rounded-xl h-9 border-slate-200"
              onClick={() => fetchHubModels()}
              title="刷新广场"
            />
          </div>
        </div>
      </div>

      {/* 筛选与排序 */}
      <div className="max-w-7xl mx-auto w-full px-6 pt-8 pb-3">
        <div className="bg-white rounded-2xl p-3.5 border border-slate-200/80 shadow-sm flex flex-wrap items-center justify-between gap-3">
          {/* 左侧维度选择 */}
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <div className="flex items-center gap-1.5 text-slate-500 font-bold">
              <Filter size={13} className="text-blue-500" />
              <span>筛选:</span>
            </div>

            {/* 市场过滤（与 OSS 6 市场一致） */}
            <Select
              size="small"
              value={selectedMarket}
              onChange={(v) => { setSelectedMarket(v); setPage(1); }}
              className="min-w-32 font-medium"
              options={[
                { value: 'ALL', label: '全部市场' },
                { value: 'CN', label: 'A股市场' },
                { value: 'US', label: '美股市场' },
                { value: 'HK', label: '港股市场' },
                { value: 'CRYPTO', label: '加密市场' },
                { value: 'FUTURES', label: '期货市场' },
                { value: 'CUSTOM', label: '自定义市场' },
              ]}
            />

            {/* 算法过滤 */}
            <Select
              size="small"
              value={selectedAlgo}
              onChange={(v) => { setSelectedAlgo(v); setPage(1); }}
              className="min-w-32 font-medium"
              options={[
                { value: 'ALL', label: '全部算法架构' },
                { value: 'CatBoost', label: 'CatBoost 决策树' },
                { value: 'LightGBM', label: 'LightGBM 梯度提升' },
                { value: 'XGBoost', label: 'XGBoost 模型' },
                { value: 'GRU', label: 'GRU 深度时序' },
                { value: 'LSTM', label: 'LSTM 长短记忆' },
              ]}
            />
          </div>

          {/* 右侧排序方式 */}
          <div className="flex items-center gap-1 bg-slate-100/80 p-1 rounded-xl text-xs">
            <span className="text-[11px] font-bold text-slate-400 px-2">排序:</span>
            {[
              { key: 'sharpe', label: '夏普比率', icon: Award },
              { key: 'ic', label: '测试集 IC', icon: Target },
              { key: 'return', label: '年化收益', icon: BarChart3 },
              { key: 'downloads', label: '下载最多', icon: Download },
              { key: 'newest', label: '最新发布', icon: Clock3 },
            ].map((sortItem) => {
              const SortIcon = sortItem.icon;
              return (
                <button
                  key={sortItem.key}
                  onClick={() => { setSortBy(sortItem.key); setPage(1); }}
                  className={clsx(
                    'inline-flex items-center gap-1 px-2.5 py-1 rounded-lg font-bold text-xs transition-all',
                    sortBy === sortItem.key
                      ? 'bg-white text-blue-600 shadow-xs'
                      : 'text-slate-500 hover:text-slate-800'
                  )}
                >
                  <SortIcon size={12} strokeWidth={1.8} />
                  {sortItem.label}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* 模型卡片网格 */}
      <div className="max-w-7xl mx-auto w-full px-6 py-5 flex-1">
        {loading ? (
          <div className="flex flex-col items-center justify-center py-28 gap-3">
            <Spin size="large" />
            <span className="text-xs text-slate-400 font-medium">正在连接 QuantDB 广场模型仓库...</span>
          </div>
        ) : loadError ? (
          <div className="bg-white rounded-3xl p-16 text-center border border-slate-200/80 shadow-sm flex flex-col items-center justify-center gap-4">
            <div className="w-16 h-16 rounded-3xl bg-amber-50 text-amber-600 flex items-center justify-center">
              <RefreshCw size={28} />
            </div>
            <div>
              <h3 className="text-base font-black text-slate-700 !mb-1">模型广场暂时不可用</h3>
              <p className="text-xs text-slate-400 !mb-0">未展示示例数据。请检查 QuantDB 服务配置后重试。</p>
            </div>
            <Button icon={<RefreshCw size={14} />} className="rounded-xl font-bold mt-2" onClick={fetchHubModels}>
              重新连接
            </Button>
          </div>
        ) : models.length === 0 ? (
          <div className="bg-white rounded-3xl p-16 text-center border border-slate-200/80 shadow-sm flex flex-col items-center justify-center gap-4">
            <div className="w-16 h-16 rounded-3xl bg-blue-50 text-blue-500 flex items-center justify-center">
              <Compass size={32} />
            </div>
            <div>
              <h3 className="text-base font-black text-slate-700 !mb-1">未找到符合条件的模型</h3>
              <p className="text-xs text-slate-400 !mb-0">尝试调整筛选关键词，或者成为第一个发布该领域模型的创作者！</p>
            </div>
            <Button
              type="primary"
              icon={<Sparkles size={14} />}
              className="rounded-xl bg-blue-600 font-bold mt-2"
              onClick={() => setShowPublishModal(true)}
            >
              发布我的第一个模型
            </Button>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
              {models.map((model) => (
                <ModelHubCard
                  key={model.id}
                  model={model}
                  onViewDetail={(m) => {
                    setSelectedModelForDetail(m);
                    setShowDetailDrawer(true);
                  }}
                  onImport={openImportModal}
                  onLike={handleLike}
                  importing={importingId === model.id}
                />
              ))}
            </div>

            {/* 分页控制器 */}
            {total > pageSize && (
              <div className="flex justify-center mt-8 mb-4">
                <Pagination
                  current={page}
                  pageSize={pageSize}
                  total={total}
                  onChange={(p, ps) => {
                    setPage(p);
                    setPageSize(ps);
                  }}
                  showTotal={(tot) => `共 ${tot} 个共享模型`}
                  className="bg-white px-4 py-2 rounded-2xl border border-slate-200 shadow-sm"
                />
              </div>
            )}
          </>
        )}
      </div>

      {/* ═══ 详情抽屉 ═══ */}
      <ModelHubDetailDrawer
        model={selectedModelForDetail}
        open={showDetailDrawer}
        onClose={() => {
          setShowDetailDrawer(false);
          setSelectedModelForDetail(null);
        }}
        onImport={openImportModal}
        importing={importingId === selectedModelForDetail?.id}
      />

      {/* ═══ 导入命名确认框 ═══ */}
      <Modal
        title={<span className="font-black text-slate-800">导入模型到本地</span>}
        open={!!importTarget}
        onCancel={() => setImportTarget(null)}
        okText="确认导入"
        cancelText="取消"
        confirmLoading={!!importTarget && importingId === importTarget.id}
        onOk={() => {
          if (importTarget) {
            const t = importTarget;
            setImportTarget(null);
            void doImportModel(t, importName);
          }
        }}
      >
        <p className="text-xs text-slate-500 mb-2">
          广场公开名：<span className="font-bold text-slate-700">{importTarget?.name}</span>
        </p>
        <Input
          value={importName}
          onChange={(e) => setImportName(e.target.value)}
          placeholder="本地展示名（默认使用广场公开名）"
          maxLength={64}
          className="rounded-xl"
        />
        <p className="text-[11px] text-slate-400 mt-2 !mb-0">
          留空则直接使用广场公开名；本地 model_id 将自动生成可读 slug。
        </p>
      </Modal>

      {/* ═══ 发布模型弹窗 ═══ */}
      <PublishModelModal
        open={showPublishModal}
        onClose={() => setShowPublishModal(false)}
        userModels={userModels}
        onSuccess={() => {
          fetchHubModels();
          loadLocalUserModels();
        }}
      />
    </div>
  );
};

export default ModelHubPage;
