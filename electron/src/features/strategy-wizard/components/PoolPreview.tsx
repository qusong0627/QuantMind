import React, { useState, useEffect, useImperativeHandle, useRef, useCallback, useMemo } from 'react';

import { Card, Table, Statistic, Row, Col, Button, Typography, Space, Empty, Alert, message, Modal, Input, Popconfirm, Tag } from 'antd';
import ReactECharts from 'echarts-for-react';
import { useWizardV2Store } from '../store/wizardV2Store';
import { type WorkingPoolItemV2 } from '../services/wizardV2Service';
import { previewPoolFile, deletePoolFile } from '../services/wizardService';
import { getWizardUserId } from '../utils/userId';
import { loadFeaturesBySymbolsInBatches } from '../utils/featureEnrichment';

const { Text } = Typography;

export type PoolPreviewHandle = {
  triggerSaveAndNext: () => void;
};

export const PoolPreview = React.forwardRef<PoolPreviewHandle, { onNext: () => void; onBack: () => void }>(
  ({ onNext, onBack: _onBack }, ref) => {
  const { 
    workingPool, 
    setWorkingPool, 
    activePoolVersionId, 
    selectedSymbols,
    setSelectedSymbols,
    saveCurrentPoolAsVersion, 
    activateVersion,
    currentPoolName, 
    setCurrentPoolName, 
    savedPools: poolHistory,
    fetchSavedPools: loadPoolHistory,
    deleteSavedPool
  } = useWizardV2Store();

  const [saving, setSaving] = useState(false);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [poolName, setPoolName] = useState('');
  const [lastSaveError, setLastSaveError] = useState<string | null>(null);
  const pendingResolveRef = useRef<((ok: boolean) => void) | null>(null);
  const saveTriggerRef = useRef<'top' | 'bottom' | null>(null);
  
  const initialWorkingPoolRef = useRef<WorkingPoolItemV2[]>([]);
  const initialSelectedRef = useRef<string[]>([]);
  const initialCurrentPoolNameRef = useRef<string>('');
  const lastHydratedKeyRef = useRef<string>('');
  
  const [historyLoading, setHistoryLoading] = useState(false);
  const [featureHydrating, setFeatureHydrating] = useState(false);
  const [historySelectedKey, setHistorySelectedKey] = useState<string>('__current__');
  const [isHistoryModalOpen, setIsHistoryModalOpen] = useState(false);

  // 设置默认股票池名称
  useEffect(() => {
    if (!poolName) {
      const dateStr = new Date().toISOString().split('T')[0]; // YYYY-MM-DD
      setPoolName(`自定义股票池_${dateStr}`);
    }
  }, []);

  // 记录"本次筛选结果"快照，便于用户从历史池切回
  useEffect(() => {
    if (!initialWorkingPoolRef.current.length && workingPool.length) {
      initialWorkingPoolRef.current = [...workingPool];
      initialSelectedRef.current = [...(selectedSymbols || [])];
    }
    if (!initialCurrentPoolNameRef.current && currentPoolName) {
      initialCurrentPoolNameRef.current = currentPoolName;
    }
  }, [workingPool, selectedSymbols, currentPoolName]);

  // 加载"我的股票池"列表
  useEffect(() => {
    loadPoolHistory();
  }, []);

  useEffect(() => {
    if (!activePoolVersionId) {
      if (historySelectedKey !== '__current__') {
        setHistorySelectedKey('__current__');
      }
      return;
    }

    if (historySelectedKey === activePoolVersionId) {
      return;
    }

    const hasMatchingHistory = poolHistory.some((item) => (item?.id || (item as any)?.file_key) === activePoolVersionId);
    if (hasMatchingHistory) {
      setHistorySelectedKey(activePoolVersionId);
    }
  }, [activePoolVersionId, poolHistory, historySelectedKey]);

  // 特征补全
  useEffect(() => {
    const hydrateMissingFeatures = async () => {
      if (featureHydrating || !workingPool.length) return;
      
      const hydrateKey = workingPool.map((item) => item.symbol).join(',');
      if (hydrateKey && hydrateKey === lastHydratedKeyRef.current) return;

      const needHydrate = workingPool.some((item) => {
        const marketCap = Number(item?.marketCap);
        const pe = Number((item as any)?.pe);
        return !Number.isFinite(marketCap) || marketCap <= 0 || !Number.isFinite(pe) || pe === 0;
      });
      if (!needHydrate) return;

      try {
        setFeatureHydrating(true);
        const symbols = workingPool.map((item) => item.symbol);
        const features = await loadFeaturesBySymbolsInBatches(symbols);
        if (!features.length) return;

        const featureMap = new Map(features.map((f: any) => [f.code, f]));
        const merged = workingPool.map((item) => {
          const f: any = featureMap.get(item.symbol);
          if (!f) return item;
          return {
            ...item,
            name: item.name || f.name,
            marketCap: Number(item?.marketCap) > 0 ? item.marketCap : (f.marketCap ?? 0),
            pe: ((item as any)?.pe !== undefined && (item as any)?.pe !== null && (item as any)?.pe !== 0) ? (item as any).pe : (f.pe ?? f.pe_ttm ?? f.peTtm ?? 0),
            roe: item?.roe ?? f.roe ?? 0,
            price: (item as any)?.price ?? f.closePrice ?? 0,
          };
        });

        setWorkingPool(merged as any, true);
        lastHydratedKeyRef.current = hydrateKey;
      } catch (e) {
        console.warn('[PoolPreview] hydrate feature failed:', e);
      } finally {
        setFeatureHydrating(false);
      }
    };

    hydrateMissingFeatures();
  }, [workingPool, setWorkingPool, featureHydrating]);

  const handleSelectHistoryPool = async (fileKey: string) => {
    setHistorySelectedKey(fileKey);
    if (!fileKey || fileKey === '__current__') {
      if (initialWorkingPoolRef.current.length) {
        setWorkingPool(initialWorkingPoolRef.current, true);
        setSelectedSymbols(initialSelectedRef.current || []);
        setCurrentPoolName(initialCurrentPoolNameRef.current || '我的股票池');
        setPoolName(initialCurrentPoolNameRef.current || '我的股票池');
        await activateVersion(''); 
        message.info('已切换为本次筛选结果');
      }
      return;
    }

    try {
      setHistoryLoading(true);
      const userId = getWizardUserId();

      const res = await previewPoolFile({ user_id: userId, file_key: fileKey });
      if (!res?.success) {
        throw new Error(res?.error || '加载失败');
      }

      const items = res.items.map((x: any) => ({
        symbol: String(x?.symbol || '').trim(),
        name: String(x?.name || '').trim(),
        marketCap: Number(x?.metrics?.market_cap ?? 0),
        pe: Number(x?.metrics?.pe ?? 0),
        roe: Number(x?.metrics?.roe ?? 0),
        price: Number(x?.metrics?.close ?? 0),
      }));

      const historyPoolName = res.pool_file?.pool_name || '历史股票池';

      setWorkingPool(items, true);
      setSelectedSymbols(items.map((item: any) => item.symbol));
      setCurrentPoolName(historyPoolName);
      setPoolName(historyPoolName);
      
      await activateVersion(fileKey);

      message.success(`已复用股票池：${historyPoolName}`);
    } catch (e: any) {
      message.error(`加载历史股票池失败: ${e?.message || '未知错误'}`);
      setHistorySelectedKey('__current__');
    } finally {
      setHistoryLoading(false);
    }
  };

  const handleDeleteHistoryPool = async (poolItem: any) => {
    const fileKey = poolItem?.id;
    if (!fileKey) {
      message.error('缺少文件标识，无法删除');
      return;
    }
    try {
      const success = await deleteSavedPool(fileKey);
      if (success) {
        message.success('已删除股票池');
        if (historySelectedKey === fileKey) {
          await handleSelectHistoryPool('__current__');
        }
      } else {
        message.error('删除失败');
      }
    } catch (e: any) {
      message.error(`删除股票池失败: ${e?.message || '未知错误'}`);
    }
  };

  const canSkipRenameModal = () => {
    const isDirty = useWizardV2Store.getState().dirty;
    return !!activePoolVersionId && !isDirty;
  };

  const dataSource = useMemo(() => workingPool.map((x) => ({ ...x, key: x.symbol })), [workingPool]);
  const selectedList = useMemo(() => workingPool.filter((x) => selectedSymbols.includes(x.symbol)), [workingPool, selectedSymbols]);
  const listForStats = selectedList.length > 0 ? selectedList : workingPool;

  const capThresholdYi = 300; 
  const capSmallCount = listForStats.filter((x) => (x.marketCap || 0) < capThresholdYi).length;
  const capLargeCount = listForStats.length - capSmallCount;
  
  const marketCapOption = {
    title: { text: '市值分布（300亿阈值）', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'item' },
    grid: { top: 40, bottom: 20, left: 40, right: 20 },
    xAxis: { type: 'category', data: ['< 300亿', '≥ 300亿'] },
    yAxis: { type: 'value' },
    series: [{
      type: 'bar',
      data: [capSmallCount, capLargeCount],
      itemStyle: { color: '#1677ff' }
    }],
  };

  const rowSelection = {
    selectedRowKeys: selectedSymbols,
    onChange: (newSelectedRowKeys: React.Key[]) => {
      setSelectedSymbols(newSelectedRowKeys as string[]);
    },
  };

  const handleNextClick = () => {
    if (workingPool.length === 0 || selectedSymbols.length === 0) {
      message.warning('请先选择股票池');
      return;
    }
    if (canSkipRenameModal()) {
      onNext();
      return;
    }
    saveTriggerRef.current = 'bottom';
    setIsModalOpen(true);
  };

  useImperativeHandle(ref, () => ({
    triggerSaveAndNext: async () => {
      if (workingPool.length === 0 || selectedSymbols.length === 0) {
        message.warning('请先选择股票池');
        return;
      }
      if (canSkipRenameModal()) {
        onNext();
        return;
      }
      saveTriggerRef.current = 'top';
      const ok = await new Promise<boolean>((resolve) => {
        pendingResolveRef.current = resolve;
        setIsModalOpen(true);
      });
      if (!ok && lastSaveError) {
        message.error(lastSaveError);
        setLastSaveError(null);
      }
    }
  }), [workingPool, selectedSymbols, lastSaveError, onNext]);

  const handleConfirmSave = async () => {
    if (!poolName.trim()) {
      message.warning('请输入股票池名称');
      return;
    }

    setSaving(true);
    setLastSaveError(null);
    try {
      const filteredPool = workingPool.filter(x => selectedSymbols.includes(x.symbol));
      if (filteredPool.length !== workingPool.length) {
          setWorkingPool(filteredPool, false); 
      }

      const success = await saveCurrentPoolAsVersion(poolName.trim());
      if (!success) {
        throw new Error('保存失败');
      }

      const currentSavedPools = useWizardV2Store.getState().savedPools;
      if (currentSavedPools.length > 0) {
        const latestId = currentSavedPools[0].id;
        await activateVersion(latestId);
        setHistorySelectedKey(latestId);
      }

      message.success(`股票池 "${poolName}" 已保存并激活 (${filteredPool.length}只股票)`);
      setIsModalOpen(false);
      
      if (pendingResolveRef.current) {
        pendingResolveRef.current(true);
        pendingResolveRef.current = null;
      }
      saveTriggerRef.current = null;

      setTimeout(() => {
        onNext();
      }, 200);

    } catch (e: any) {
      console.error('[PoolPreview] 保存失败:', e);
      const msg = e?.message || '保存股票池失败';
      setLastSaveError(msg);
      if (saveTriggerRef.current !== 'top') {
        message.error(msg);
      }
      if (pendingResolveRef.current) {
        pendingResolveRef.current(false);
        pendingResolveRef.current = null;
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ padding: 0 }}>
      <Row gutter={24}>
        <Col span={16}>
          <Card
            title={
              <Space>
                <span>筛选结果 ({dataSource.length})</span>
                {selectedSymbols && selectedSymbols.length < dataSource.length && (
                  <Typography.Text type="secondary" style={{ fontSize: 13, fontWeight: 'normal' }}>
                    已选 {selectedSymbols.length} 只
                  </Typography.Text>
                )}
              </Space>
            }
            extra={
              <Space size="small" align="center">
                <Button
                  size="small"
                  loading={historyLoading}
                  onClick={() => {
                    loadPoolHistory();   
                    setIsHistoryModalOpen(true);
                  }}
                >
                  管理我的股票池
                </Button>
              </Space>
            }
            variant="borderless"
            styles={{ body: { padding: 0 } }}
          >
            {dataSource.length === 0 ? (
              <div style={{ padding: 32 }}>
                <Empty description="暂无股票池数据" />
              </div>
            ) : (
              <Table
                dataSource={dataSource}
                size="small"
                rowSelection={rowSelection}
                tableLayout="fixed"
                columns={[
                  { title: '代码', dataIndex: 'symbol', width: 140, ellipsis: true },
                  { title: '名称', dataIndex: 'name', width: 160, ellipsis: true },
                  {
                    title: '市值(亿)',
                    dataIndex: 'marketCap',
                    width: 140,
                    align: 'center',
                    render: (v) => (v !== undefined && v !== null) ? Number(v).toFixed(2) : '-',
                    sorter: (a, b) => (a.marketCap || 0) - (b.marketCap || 0)
                  },
                  {
                    title: '市盈率',
                    dataIndex: 'pe',
                    width: 140,
                    onHeaderCell: () => ({ style: { paddingRight: 30 } }),
                    onCell: () => ({ style: { paddingRight: 30 } }),
                    render: (v) => (v !== undefined && v !== null && v !== 0) ? Number(v).toFixed(2) : (v === 0 ? '0.00' : '-'),
                    sorter: (a, b) => (a.pe || 0) - (b.pe || 0)
                  },
                ]}
                pagination={{ pageSize: 10, showSizeChanger: false }}
              />
            )}
          </Card>
        </Col>
        <Col span={8}>
          <Space direction="vertical" style={{ width: '100%' }} size="large">
            <Card variant="borderless" title="统计概览">
              {dataSource.length === 0 ? (
                <Alert type="info" showIcon title="尚未生成股票池，请返回上一步完成解析。" />
              ) : (
                <Row gutter={16}>
                  <Col span={12}>
                    <Statistic
                      title="选出股票"
                      value={selectedSymbols?.length || 0}
                      suffix="只"
                      styles={{ content: { fontSize: 20, fontWeight: 600, color: '#1e293b' } }}
                    />
                  </Col>
                  <Col span={12}>
                    <Statistic
                      title="覆盖率"
                      value={((selectedSymbols?.length || 0) / 6017) * 100}
                      suffix="%"
                      precision={2}
                      styles={{ content: { fontSize: 20, fontWeight: 600, color: '#1e293b' } }}
                    />
                  </Col>
                </Row>
              )}
            </Card>

            <Card variant="borderless" styles={{ body: { padding: 12 } }}>
              {dataSource.length === 0 ? <Empty description="暂无图表" /> : <ReactECharts option={marketCapOption} style={{ height: 200 }} />}
            </Card>
          </Space>
        </Col>
      </Row>

      <Modal
        title="请为您的股票池命名"
        open={isModalOpen}
        style={{ top: '18vh' }}
        onOk={handleConfirmSave}
        onCancel={() => {
          setIsModalOpen(false);
          if (pendingResolveRef.current) {
            pendingResolveRef.current(false);
            pendingResolveRef.current = null;
          }
          saveTriggerRef.current = null;
        }}
        confirmLoading={saving}
        okText="保存并下一步"
        cancelText="取消"
      >
        <p>请为您的股票池命名，以便后续使用。</p>
        <Input
          placeholder="例如：高股息策略池_20240101"
          value={poolName}
          onChange={e => setPoolName(e.target.value)}
          maxLength={50}
          autoFocus
        />
      </Modal>

      <Modal
        title="我的股票池"
        open={isHistoryModalOpen}
        centered
        onCancel={() => setIsHistoryModalOpen(false)}
        footer={[
          <Button key="current" onClick={() => handleSelectHistoryPool('__current__')}>
            使用本次筛选结果
          </Button>,
          <Button key="close" type="primary" onClick={() => setIsHistoryModalOpen(false)}>
            关闭
          </Button>,
        ]}
        width={860}
      >
        <Table
          rowKey={(row: any) => row.id || row.file_key}
          loading={historyLoading}
          dataSource={poolHistory || []}
          pagination={{ pageSize: 8, showSizeChanger: false }}
          locale={{ 
            emptyText: (
              <div className="py-12 flex flex-col items-center">
                <Empty 
                  description={
                    <div className="text-slate-400">
                      <div>暂无已保存股票池</div>
                      <div className="text-[11px] mt-2 opacity-70">
                        如果持续出现此问题，请检查后端 API 状态
                      </div>
                    </div>
                  } 
                />
              </div>
            )
          }}
          columns={[
            {
              title: '股票池名称',
              dataIndex: 'name',
              render: (_: any, row: any) => (
                <Space>
                  <span>{row.name || '未命名股票池'}</span>
                  {historySelectedKey === (row.id || row.file_key) && <Tag variant="filled" color="blue">当前使用</Tag>}
                </Space>
              ),
            },
            {
              title: '股票数',
              dataIndex: 'stockCount',
              width: 120,
              render: (v: any) => `${Number(v || 0)} 只`,
            },
            {
              title: '创建时间',
              dataIndex: 'createdAt',
              width: 220,
              render: (v: any) => (v ? String(v).slice(0, 19).replace('T', ' ') : '-'),
            },
            {
              title: '操作',
              key: 'actions',
              width: 220,
              render: (_: any, row: any) => (
                <Space>
                  <Button size="small" onClick={() => handleSelectHistoryPool(row.id || row.file_key)}>
                    复用
                  </Button>
                  <Popconfirm
                    title="确认删除该股票池？"
                    description="删除后不可恢复"
                    okText="删除"
                    cancelText="取消"
                    onConfirm={() => handleDeleteHistoryPool(row)}
                  >
                    <Button size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Modal>
    </div>
  );
});
