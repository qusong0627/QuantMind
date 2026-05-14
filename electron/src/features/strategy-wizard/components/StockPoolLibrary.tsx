import React, { useEffect, useRef, useState } from 'react';
import { 
  Card, 
  Button, 
  Typography, 
  Space, 
  Empty, 
  Tooltip, 
  Flex, 
  message, 
  Spin, 
  Tag, 
  Badge, 
  Modal, 
  Popconfirm, 
  Table, 
  Divider,
  Menu
} from 'antd';
import { 
  FolderOutlined,
  AppstoreOutlined,
  HistoryOutlined, 
  DeleteOutlined, 
  ClockCircleOutlined,
  StockOutlined,
  GlobalOutlined,
  CloudServerOutlined,
  RightOutlined,
  SettingOutlined
} from '@ant-design/icons';
import { useWizardV2Store } from '../store/wizardV2Store';
// 使用按需导入以避免与其他模块的静态/动态导入冲突
import { loadFeaturesBySymbolsInBatches } from '../utils/featureEnrichment';
import { getWizardUserId } from '../utils/userId';
import dayjs from 'dayjs';

const { Text, Title } = Typography;

export const StockPoolLibrary: React.FC = () => {
  const { 
    savedPools: userStockPools, 
    fetchSavedPools,
    setWorkingPool,
    setCurrentPoolName,
    activateVersion,
    deleteSavedPool
  } = useWizardV2Store();

  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [isManageModalOpen, setIsManageModalOpen] = useState(false);
  const refreshingPoolsRef = useRef(false);

  const refreshPools = async (silent = true) => {
    if (refreshingPoolsRef.current) return;
    refreshingPoolsRef.current = true;
    try {
      await fetchSavedPools();
    } catch (err: any) {
      if (!silent) {
        message.warning(err?.message || '刷新云端股票池失败，请稍后重试');
      }
    } finally {
      refreshingPoolsRef.current = false;
    }
  };

  const handleDeletePool = async (pool: any) => {
    if (!pool?.id) return;

    try {
      setLoadingId(pool.id);
      const success = await deleteSavedPool(pool.id);
      if (success) {
        message.success('已删除股票池');
      } else {
        message.error('删除失败');
      }
    } catch (err: any) {
      message.error(err?.message || '删除失败');
    } finally {
      setLoadingId(null);
    }
  };

  useEffect(() => {
    refreshPools(true);
  }, []);

  useEffect(() => {
    if (!isManageModalOpen) return;
    refreshPools(true);
  }, [isManageModalOpen]);

  const handleSelectAllMarket = async () => {
    if (loadingId) return;
    setLoadingId('all-market');
    try {
      message.loading({ content: '正在初始化全市场 5205 只标的...', key: 'allMarket', duration: 0 });
      const response = await fetch('/data/stocks/stocks_index.json');
      const data = await response.json();
      const items = data.items || [];
      const symbols = items.map((s: any) => s.symbol);
      const itemMap = new Map<string, any>(items.map((s: any) => [s.symbol, s]));

      const richData = await loadFeaturesBySymbolsInBatches(symbols, 800, { lite: true });
      const richMap = new Map(richData.map((item) => [item.code, item]));
      const fullStocks = symbols.map((symbol: string) => {
        const item = richMap.get(symbol);
        return {
          symbol,
          name: item?.name || itemMap.get(symbol)?.name || symbol,
          marketCap: item?.marketCap,
          pe: (item as any)?.pe ?? (item as any)?.pe_ttm ?? (item as any)?.peTtm,
          roe: item?.roe,
          price: item?.closePrice,
        };
      });

      setCurrentPoolName('全市场 (A股)');
      setWorkingPool(fullStocks as any);
      message.success({ content: `已载入全市场 ${fullStocks.length} 只标的（最新交易日核心字段）`, key: 'allMarket' });
    } catch (err) {
      message.error({ content: '加载全市场标的失败', key: 'allMarket' });
    } finally {
      setLoadingId(null);
    }
  };

  const handleSelectPool = async (pool: any) => {
    if (loadingId) return;
    setLoadingId(pool.id);
    try {
      const userId = getWizardUserId();
      const { previewPoolFile } = await import('../services/wizardService');
      const res = await previewPoolFile({ user_id: userId, file_key: pool.id });
      
      if (res.success && res.items) {
        const fullStocks = res.items.map((x: any) => ({
          symbol: x.symbol,
          name: x.name || x.symbol,
          marketCap: x.metrics?.market_cap ?? x.marketCap ?? 0,
          pe: x.metrics?.pe ?? x.pe ?? 0,
          roe: x.metrics?.roe ?? x.roe ?? 0,
          price: x.metrics?.close ?? x.price ?? 0
        }));

        setCurrentPoolName(pool.name);
        setWorkingPool(fullStocks as any);
        await activateVersion(pool.id);
        message.success(`已复用股票池: ${pool.name}`);
      }
    } catch (err) {
      message.error('复用失败');
    } finally {
      setLoadingId(null);
      setIsManageModalOpen(false);
    }
  };

  return (
    <div style={{ 
      height: '100%', 
      background: '#fff',
      display: 'flex',
      flexDirection: 'column',
      overflow: 'hidden'
    }}>
      {/* 侧边栏头部 - 紧凑型 */}
      <div style={{ padding: '20px 16px 12px' }}>
        <Flex justify="space-between" align="center">
          <Title level={5} style={{ 
            margin: 0, 
            fontSize: '15px', 
            color: '#1e293b',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: '10px'
          }}>
            <FolderOutlined style={{ color: '#3b82f6' }} />
            资产库管理
          </Title>
          <Tooltip title="管理我的股票池">
            <Button 
              type="text" 
              size="small" 
              icon={<SettingOutlined style={{ fontSize: 16, color: '#64748b' }} />} 
              onClick={() => setIsManageModalOpen(true)}
            />
          </Tooltip>
        </Flex>
      </div>
      
      <div style={{ flex: 1, overflowY: 'auto', padding: '0 12px 12px' }}>
        <Flex vertical gap={16}>
          {/* 系统内置 */}
          <section>
            <div style={{ padding: '0 4px 8px', display: 'flex', alignItems: 'center', gap: 6 }}>
              <div style={{ width: 3, height: 12, background: '#3b82f6', borderRadius: 2 }} />
              <Text type="secondary" style={{ fontSize: 11, fontWeight: 600, letterSpacing: 0.5 }}>系统内置</Text>
            </div>
            <div 
              onClick={() => handleSelectAllMarket()}
              style={{
                background: loadingId === 'all-market' ? '#eff6ff' : '#f8fafc',
                padding: '12px',
                borderRadius: 12,
                border: loadingId === 'all-market' ? '1px solid #3b82f6' : '1px solid #e2e8f0',
                cursor: 'pointer',
                transition: 'all 0.2s'
              }}
              className="library-item-hover"
            >
              <Flex align="center" justify="space-between">
                <Space>
                  <AppstoreOutlined style={{ color: loadingId === 'all-market' ? '#3b82f6' : '#64748b' }} />
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 600, color: '#1e293b' }}>全部市场 (A股)</div>
                    <Text type="secondary" style={{ fontSize: 10 }}>5205 只标的</Text>
                  </div>
                </Space>
                {loadingId === 'all-market' ? <Spin size="small" /> : <RightOutlined style={{ fontSize: 10, color: '#bfbfbf' }} />}
              </Flex>
            </div>
          </section>

          <section>
            <div style={{ padding: '0 4px 8px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <Space size={6}>
                <div style={{ width: 3, height: 12, background: '#8b5cf6', borderRadius: 2 }} />
                <Text type="secondary" style={{ fontSize: 11, fontWeight: 600, letterSpacing: 0.5 }}>我的云端股票池</Text>
              </Space>
              <Space size={4}>
                <Popconfirm
                  title="清空所有云端记录？"
                  description="此操作不可恢复，将永久删除所有备份。"
                  onConfirm={async () => {
                    message.loading({ content: '正在清空...', key: 'clearPools' });
                    try {
                      const userId = getWizardUserId();
                      const { deletePoolFile } = await import('../services/wizardService');
                      for (const p of userStockPools) {
                        await deletePoolFile({ user_id: userId, file_key: p.id, file_url: '' });
                      }
                      refreshPools(false);
                      message.success({ content: '已清空云端记录', key: 'clearPools' });
                    } catch (e) {
                      message.error({ content: '清空失败', key: 'clearPools' });
                    }
                  }}
                  okText="确定"
                  cancelText="取消"
                  placement="bottomRight"
                >
                  <Button type="text" size="small" danger style={{ fontSize: 10 }}>清空</Button>
                </Popconfirm>
                <Button 
                  type="text" 
                  size="small" 
                  icon={<HistoryOutlined style={{ fontSize: 12 }} />} 
                  onClick={() => refreshPools(false)}
                  loading={loadingId === 'refresh'}
                />
              </Space>
            </div>
            
            {userStockPools.length === 0 ? (
              <Empty 
                image={Empty.PRESENTED_IMAGE_SIMPLE} 
                description={<Text type="secondary" style={{ fontSize: 11 }}>暂无已保存股票池</Text>} 
                style={{ margin: '20px 0' }}
              />
            ) : (
              <Flex vertical gap={8}>
                {userStockPools.slice(0, 15).map((pool, index) => (
                  <div 
                    key={`${pool.id}-${index}`}
                    onClick={() => handleSelectPool(pool)}
                    style={{
                      background: loadingId === pool.id ? '#f0f7ff' : '#fff',
                      padding: '8px 12px',
                      borderRadius: 10,
                      border: loadingId === pool.id ? '1px solid #1890ff' : '1px solid #f0f0f0',
                      cursor: 'pointer',
                      transition: 'all 0.2s',
                    }}
                    className="library-item-hover"
                  >
                    <Flex justify="space-between" align="center">
                      <div style={{ flex: 1, overflow: 'hidden' }}>
                        <div style={{ 
                          fontSize: 12, 
                          fontWeight: 600, 
                          color: loadingId === pool.id ? '#1890ff' : '#262626', 
                          overflow: 'hidden', 
                          textOverflow: 'ellipsis', 
                          whiteSpace: 'nowrap',
                          marginBottom: 2
                        }}>
                          {pool.name || '未命名'}
                        </div>
                        <Flex gap={8} align="center">
                          <Text type="secondary" style={{ fontSize: 10 }}>{pool.stockCount || 0} 只</Text>
                          <Divider type="vertical" style={{ height: 10, margin: 0 }} />
                          <Text type="secondary" style={{ fontSize: 10 }}>{dayjs(pool.updatedAt || pool.createdAt).format('MM-DD HH:mm')}</Text>
                        </Flex>
                      </div>
                      <div onClick={e => e.stopPropagation()}>
                        <Popconfirm
                          title="确认删除？"
                          onConfirm={() => handleDeletePool(pool)}
                          okText="删除"
                          cancelText="取消"
                          placement="right"
                        >
                          <Button 
                            type="text" 
                            icon={<DeleteOutlined style={{ fontSize: 11, color: '#ff4d4f' }} />} 
                            size="small" 
                          />
                        </Popconfirm>
                      </div>
                    </Flex>
                  </div>
                ))}
              </Flex>
            )}
          </section>
        </Flex>
      </div>

      <Modal
        title="我的股票池管理"
        open={isManageModalOpen}
        onCancel={() => setIsManageModalOpen(false)}
        width={800}
        footer={null}
        centered
      >
        <Table
          dataSource={userStockPools}
          rowKey="id"
          size="middle"
          columns={[
            {
              title: '股票池名称',
              dataIndex: 'name',
              render: (text, record) => (
                <Space>
                  <Text strong>{text || '未命名'}</Text>
                  {loadingId === record.id && <Tag color="blue">当前选中</Tag>}
                </Space>
              )
            },
            {
              title: '股票数',
              dataIndex: 'stockCount',
              render: (v) => `${v || 0} 只`
            },
            {
              title: '同步时间',
              dataIndex: 'updatedAt',
              render: (v, record) => dayjs(v || record.createdAt).format('YYYY-MM-DD HH:mm')
            },
                {
                  title: '操作',
                  render: (_, record) => (
                    <Space>
                      <Button size="small" onClick={() => handleSelectPool(record)}>复用</Button>
                      <Popconfirm title="确定删除？" onConfirm={() => handleDeletePool(record)}>
                        <Button size="small" danger icon={<DeleteOutlined />} />
                      </Popconfirm>
                    </Space>
                  )
                }
          ]}
        />
      </Modal>

      {/* 底部装饰 */}
      <div style={{ padding: '12px', background: '#fff', borderTop: '1px solid #eef0f2' }}>
        <div style={{ 
          padding: '8px', borderRadius: 8, background: '#f6ffed', 
          border: '1px solid #b7eb8f', display: 'flex', alignItems: 'center', gap: 8
        }}>
          <Badge status="processing" color="#52c41a" />
          <Text style={{ fontSize: 11, color: '#389e0d' }}>已连接 QuantMind 实时云端</Text>
        </div>
      </div>

      <style dangerouslySetInnerHTML={{ __html: `
        .library-item-hover:hover {
          border-color: #1890ff !important;
          transform: translateY(-2px);
          box-shadow: 0 4px 12px rgba(0,0,0,0.05);
        }
      `}} />
    </div>
  );
};
