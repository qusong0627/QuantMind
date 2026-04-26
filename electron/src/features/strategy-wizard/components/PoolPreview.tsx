import React, { useState, useEffect, useImperativeHandle, useRef, useCallback } from 'react';

import { Card, Table, Statistic, Row, Col, Button, Typography, Space, Empty, Alert, message, Modal, Input, Popconfirm, Tag } from 'antd';
import ReactECharts from 'echarts-for-react';
import { useWizardStore } from '../store/wizardStore';
import { savePoolFile, listPoolFiles, previewPoolFile, deletePoolFile, setActivePoolFile } from '../services/wizardService';
import { getWizardUserId } from '../utils/userId';

export type PoolPreviewHandle = {
  triggerSaveAndNext: () => void;
};

export const PoolPreview = React.forwardRef<PoolPreviewHandle, { onNext: () => void; onBack: () => void }>(
  ({ onNext, onBack: _onBack }, ref) => {
  const { pool, setPool, selectedSymbols, setSelectedSymbols, poolFile, setPoolFile, saveStatus } = useWizardStore();
  const [saving, setSaving] = useState(false);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [poolName, setPoolName] = useState('');
  const [lastSaveError, setLastSaveError] = useState<string | null>(null);
  const pendingResolveRef = useRef<((ok: boolean) => void) | null>(null);
  const saveTriggerRef = useRef<'top' | 'bottom' | null>(null);
  const initialPoolRef = useRef<typeof pool>(null);
  const initialSelectedRef = useRef<string[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [poolHistory, setPoolHistory] = useState<any[]>([]);
  const [historySelectedKey, setHistorySelectedKey] = useState<string>('__current__');
  const [isHistoryModalOpen, setIsHistoryModalOpen] = useState(false);

  const loadPoolHistory = useCallback(async (silent = false) => {
    try {
      setHistoryLoading(true);
      const userId = getWizardUserId();
      const res = await listPoolFiles({ user_id: userId, limit: 100 });
      if (res?.success) {
        setPoolHistory(res.pools || []);
      } else {
        setPoolHistory([]);
        if (!silent) {
          message.warning(res?.error || '加载历史股票池失败');
        }
        console.warn('[PoolPreview] listPoolFiles failed:', res?.error);
      }
    } catch (e: any) {
      setPoolHistory([]);
      if (!silent) {
        message.warning(`加载历史股票池失败: ${e?.message || '未知错误'}`);
      }
      console.warn('[PoolPreview] loadPoolHistory failed:', e);
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  // 组件挂载时验证poolFile数据结构
  useEffect(() => {
    if (poolFile && typeof poolFile === 'string') {
      console.error('[PoolPreview] 错误: 检测到poolFile是字符串,正在清除...', poolFile);
      setPoolFile(undefined);
      message.warning('检测到错误的股票池数据,已自动清除');
    }
  }, [poolFile, setPoolFile]);

  // 设置默认股票池名称
  useEffect(() => {
    if (!poolName) {
      const dateStr = new Date().toISOString().split('T')[0]; // YYYY-MM-DD
      setPoolName(`自定义股票池_${dateStr}`);
    }
  }, []);

  // 记录"本次筛选结果"快照，便于用户从历史池切回
  useEffect(() => {
    if (!initialPoolRef.current && pool?.items?.length) {
      initialPoolRef.current = pool;
      initialSelectedRef.current = [...(selectedSymbols || [])];
    }
  }, [pool, selectedSymbols]);

  // 加载"我的股票池"列表
  useEffect(() => {
    loadPoolHistory(true);
  }, [loadPoolHistory]);

  useEffect(() => {
    if (poolFile?.fileKey) {
      setHistorySelectedKey(poolFile.fileKey);
      return;
    }

    setHistorySelectedKey('__current__');
  }, [poolFile?.fileKey]);

  const handleSelectHistoryPool = async (fileKey: string) => {
    setHistorySelectedKey(fileKey);
    if (!fileKey || fileKey === '__current__') {
      if (initialPoolRef.current?.items?.length) {
        setPool(initialPoolRef.current as any);
        setSelectedSymbols(initialSelectedRef.current || []);
        // 切回"本次筛选结果"时，清理复用的 poolFile，避免后续误认为仍在复用历史池
        setPoolFile(undefined);
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

      // 更新向导状态：股票池列表 + 勾选 + poolFile 供后续 generate-qlib 复用
      setPool({ items: res.items || [], summary: res.summary || {}, charts: res.charts || {} } as any);
      setSelectedSymbols((res.items || []).map((x: any) => x.symbol));
      if (res.pool_file?.file_key) {
        setPoolFile({
          fileUrl: res.pool_file.file_url,
          fileKey: res.pool_file.file_key,
          format: (res.pool_file.format || 'txt') as any,
          relativePath: res.pool_file.relative_path,
          fileSize: res.pool_file.file_size,
          codeHash: res.pool_file.code_hash,
        });
        // 设置该股票池为活跃状态
        try {
          await setActivePoolFile({ user_id: userId, file_key: res.pool_file.file_key });
        } catch (e) {
          console.warn('Failed to set active pool file:', e);
        }
      }

      message.success(`已复用股票池：${res.pool_file?.pool_name || fileKey}`);
    } catch (e: any) {
      message.error(`加载历史股票池失败: ${e?.message || '未知错误'}`);
      setHistorySelectedKey('__current__');
    } finally {
      setHistoryLoading(false);
    }
  };

  const handleDeleteHistoryPool = async (poolItem: any) => {
    const fileKey = poolItem?.file_key;
    if (!fileKey) {
      message.error('缺少文件标识，无法删除');
      return;
    }
    try {
      const userId = getWizardUserId();
      const res = await deletePoolFile({
        user_id: userId,
        file_url: poolItem?.file_url || '',
        file_key: fileKey,
      });
      if (!res?.success) {
        throw new Error(res?.error || '删除失败');
      }

      await loadPoolHistory(true);
      if (historySelectedKey === fileKey) {
        await handleSelectHistoryPool('__current__');
      }
      message.success('已删除股票池');
    } catch (e: any) {
      message.error(`删除股票池失败: ${e?.message || '未知错误'}`);
    }
  };

  const canSkipRenameModal = () => {
    // 仅当"复用历史股票池"且当前选择未被修改时，允许直接进入下一步，不再弹出命名窗口。
    if (!pool?.items?.length) return false;
    if (historySelectedKey === '__current__') return false;
    if (!poolFile?.fileKey || poolFile.fileKey !== historySelectedKey) return false;

    const allSymbols = pool.items.map((x) => x.symbol);
    if (!Array.isArray(selectedSymbols)) return false;
    if (selectedSymbols.length !== allSymbols.length) return false;

    const setSel = new Set(selectedSymbols);
    for (const s of allSymbols) {
      if (!setSel.has(s)) return false;
    }
    return true;
  };


  const dataSource = (pool?.items || []).map((x) => ({ ...x, key: x.symbol }));

  const selectedList = (pool?.items || []).filter((x) => selectedSymbols.includes(x.symbol));
  const listForStats = selectedList.length > 0 ? selectedList : (pool?.items || []);
  const marketCapYI = (v: any) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return 0;
    // 标准话术：接口返回 market_cap 使用元，前端展示统一换算为亿（/100000000）。
    return n / 100000000;
  };
  const capThresholdYi = 300; // 300亿阈值
  const capSmallCount = listForStats.filter((x) => marketCapYI(x?.metrics?.market_cap) < capThresholdYi).length;
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
    if (!pool?.items || selectedSymbols.length === 0) {
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
      if (!pool?.items || selectedSymbols.length === 0) {
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
  }), [pool, selectedSymbols, lastSaveError]);

  const handleConfirmSave = async () => {
    if (!poolName.trim()) {
      message.warning('请输入股票池名称');
      return;
    }

    setSaving(true);
    setLastSaveError(null);
    try {
      const userId = getWizardUserId();

      // 若已有未保存文件,先删除 (仅当fileUrl存在且不是已保存到云端的状态时)
      if (!saveStatus.savedToCloud && poolFile?.fileUrl) {
        // try { await deletePoolFile({ file_url: poolFile.fileUrl, file_key: poolFile.fileKey }); } catch(e) { console.warn('Clean up failed', e) }
      }

      const selected = pool.items.filter((x) => selectedSymbols.includes(x.symbol));

      const toQlibSymbol = (symbol: string) => {
        const s = String(symbol || '').trim();
        if (!s) return '';
        const u = s.toUpperCase();
        if (u.length === 8 && (u.startsWith('SZ') || u.startsWith('SH')) && /^\d{6}$/.test(u.slice(2))) return u;
        const dotIdx = u.indexOf('.');
        if (dotIdx > 0) {
          const base = u.slice(0, dotIdx);
          const suffix = u.slice(dotIdx + 1);
          if ((suffix === 'SZ' || suffix === 'SH') && /^\d+$/.test(base)) {
            return `${suffix}${base.padStart(6, '0')}`;
          }
        }
        if ((u.startsWith('SZ') || u.startsWith('SH')) && u.length >= 8) {
          const digits = u.slice(2).replace(/\D+/g, '');
          if (digits) return `${u.slice(0, 2)}${digits.padStart(6, '0').slice(-6)}`;
        }
        return u;
      };

      // 使用TXT格式保存(QLib instruments),后端会自动创建时间戳文件夹
      console.log('[PoolPreview] 开始保存股票池文件...', { userId, count: selected.length, poolName });

      const res = await savePoolFile({
        user_id: userId,
        format: 'txt',
        pool: selected.map((x) => ({ symbol: toQlibSymbol(x.symbol), name: x.name })),
        pool_name: poolName
      });

      console.log('[PoolPreview] API响应:', res);

      // 严格验证响应 (兼容 legacy success字段 和 standard code字段)
      const isSuccess = res?.success === true || res?.code === 0;
      if (!isSuccess) {
        throw new Error(res?.error || res?.message || '保存股票池失败');
      }

      const responseData = res.data || res; // 提取实际数据

      // 验证必需字段
      if (!responseData.file_key) {
        throw new Error('服务器未返回文件Key,保存失败');
      }

      if (!responseData.relative_path) {
        console.warn('[PoolPreview] 警告: 未返回相对路径');
      }

      // 保存到store
      const poolFileData = {
        fileUrl: responseData.file_url,
        fileKey: responseData.file_key,
        format: 'txt' as const,
        relativePath: responseData.relative_path,
        fileSize: responseData.file_size,
        codeHash: responseData.code_hash,
      };

      setPoolFile(poolFileData);

      console.log('[PoolPreview] 股票池文件已保存到store:', poolFileData);

      // 验证store是否成功保存
      setTimeout(() => {
        const currentPoolFile = useWizardStore.getState().poolFile;
        if (!currentPoolFile?.fileKey) {
          console.error('[PoolPreview] 错误: store中未找到poolFile!');
          message.error('状态保存失败,请重试');
        } else {
          console.log('[PoolPreview] store验证成功:', currentPoolFile);
        }
      }, 100);

      message.success(`股票池 "${poolName}" 已保存 (${selected.length}只股票)`);
      await loadPoolHistory(true);
      setHistorySelectedKey(responseData.file_key || '__current__');

      setIsModalOpen(false);
      if (pendingResolveRef.current) {
        pendingResolveRef.current(true);
        pendingResolveRef.current = null;
      }
      saveTriggerRef.current = null;

      // 确保状态已保存后再进入下一步
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
    <div style={{ padding: 12 }}>
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
                    loadPoolHistory();   // 点击时立即从 DB 拉取最新列表
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
                    dataIndex: ['metrics', 'market_cap'],
                    width: 140,
                    align: 'center',
                    render: (v) => v ? (v / 100000000).toFixed(2) : '-',
                    sorter: (a, b) => (a.metrics?.market_cap || 0) - (b.metrics?.market_cap || 0)
                  },
                  {
                    title: '市盈率',
                    dataIndex: ['metrics', 'pe'],
                    width: 140,
                    onHeaderCell: () => ({ style: { paddingRight: 30 } }),
                    onCell: () => ({ style: { paddingRight: 30 } }),
                    render: (v) => (v !== undefined && v !== null && v !== 0) ? v.toFixed(2) : (v === 0 ? '0.00' : '-'),
                    sorter: (a, b) => (a.metrics?.pe || 0) - (b.metrics?.pe || 0)
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
                <Alert type="info" showIcon message="尚未生成股票池，请返回上一步完成解析。" />
              ) : (
                <Row gutter={16}>
                  <Col span={12}><Statistic title="选出股票" value={selectedSymbols?.length || 0} suffix="只抽选" /></Col>
                  <Col
                    span={12}
                  >
                    <Statistic
                      title="覆盖率"
                      value={Math.min(100, Number(pool?.summary?.matchRate || 0))}
                      suffix="%"
                      precision={2}
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
          rowKey={(row: any) => row.file_key}
          loading={historyLoading}
          dataSource={poolHistory || []}
          pagination={{ pageSize: 8, showSizeChanger: false }}
          locale={{ emptyText: '暂无已保存股票池' }}
          columns={[
            {
              title: '股票池名称',
              dataIndex: 'pool_name',
              render: (_: any, row: any) => (
                <Space>
                  <span>{row.pool_name || '未命名股票池'}</span>
                  {historySelectedKey === row.file_key && <Tag color="blue">当前使用</Tag>}
                  {row.is_active && <Tag color="green">活跃</Tag>}
                </Space>
              ),
            },
            {
              title: '股票数',
              dataIndex: 'stock_count',
              width: 120,
              render: (v: any) => `${Number(v || 0)} 只`,
            },
            {
              title: '创建时间',
              dataIndex: 'created_at',
              width: 220,
              render: (v: any) => (v ? String(v).slice(0, 19).replace('T', ' ') : '-'),
            },
            {
              title: '操作',
              key: 'actions',
              width: 220,
              render: (_: any, row: any) => (
                <Space>
                  <Button size="small" onClick={() => handleSelectHistoryPool(row.file_key)}>
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
