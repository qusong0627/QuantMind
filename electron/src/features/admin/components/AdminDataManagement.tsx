import React, { useState } from 'react';
import { Card, Tabs, Typography, Button, Space, Tag } from 'antd';
import {
  DatabaseOutlined,
  GlobalOutlined,
  ThunderboltOutlined,
  SettingOutlined,
  StockOutlined,
  FundOutlined,
  CheckCircleFilled,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { AdminQuantDBPanel } from './AdminQuantDBPanel';
import { AdminQuantMarketPanel } from './AdminQuantMarketPanel';

const { Title, Text, Paragraph } = Typography;

export const AdminDataManagement: React.FC = () => {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<string>('a_share');

  const tabItems = [
    {
      key: 'a_share',
      label: (
        <span className="flex items-center gap-2 font-bold text-sm">
          <span>🇨🇳</span>
          <span>A 股市场 (QuantDB)</span>
        </span>
      ),
      children: <AdminQuantDBPanel />,
    },
    {
      key: 'quanthk',
      label: (
        <span className="flex items-center gap-2 font-bold text-sm">
          <span>🇭🇰</span>
          <span>港股市场 (QuantHK)</span>
          <Tag className="m-0 border-orange-200 text-orange-500 bg-orange-50 rounded-full px-1.5 py-0 text-[10px] font-black">Beta</Tag>
        </span>
      ),
      children: (
        <AdminQuantMarketPanel
          market="quanthk"
          marketLabel="港股市场"
          color="#10b981"
        />
      ),
    },
    {
      key: 'quantus',
      label: (
        <span className="flex items-center gap-2 font-bold text-sm">
          <span>🇺🇸</span>
          <span>美股市场 (QuantUS)</span>
          <Tag className="m-0 border-orange-200 text-orange-500 bg-orange-50 rounded-full px-1.5 py-0 text-[10px] font-black">Beta</Tag>
        </span>
      ),
      children: (
        <AdminQuantMarketPanel
          market="quantus"
          marketLabel="美股市场"
          color="#3b82f6"
        />
      ),
    },
    {
      key: 'quantfutures',
      label: (
        <span className="flex items-center gap-2 font-bold text-sm">
          <span>⚡</span>
          <span>国内期货 (QuantFutures)</span>
          <Tag className="m-0 border-orange-200 text-orange-500 bg-orange-50 rounded-full px-1.5 py-0 text-[10px] font-black">Beta</Tag>
        </span>
      ),
      children: (
        <AdminQuantMarketPanel
          market="quantfutures"
          marketLabel="期货市场"
          color="#f59e0b"
        />
      ),
    },
    {
      key: 'quantbc',
      label: (
        <span className="flex items-center gap-2 font-bold text-sm">
          <span>🪙</span>
          <span>加密货币 (QuantBC)</span>
          <Tag className="m-0 border-orange-200 text-orange-500 bg-orange-50 rounded-full px-1.5 py-0 text-[10px] font-black">Beta</Tag>
        </span>
      ),
      children: (
        <AdminQuantMarketPanel
          market="quantbc"
          marketLabel="加密货币"
          color="#8b5cf6"
        />
      ),
    },
  ];

  return (
    <div className="space-y-6">
      {/* 顶部标题与直供说明 */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 bg-white p-6 rounded-3xl border border-slate-100 shadow-sm">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-2xl bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600 text-2xl shrink-0">
            <DatabaseOutlined />
          </div>
          <div>
            <div className="flex items-center gap-3">
              <Title level={4} className="!m-0 !font-black !text-slate-800 tracking-tight">
                数据管理平台
              </Title>
              <Tag color="success" className="m-0 border-none rounded-full px-2.5 py-0.5 text-[11px] font-black bg-emerald-50 text-emerald-600 flex items-center gap-1">
                <CheckCircleFilled /> QuantDB 云端直供
              </Tag>
            </div>
            <Text className="text-slate-400 text-xs mt-1 block">
              全量标准化行情、L2 资金流、板块成分与 AI 预计算因子由 QuantDB 直接云端供应与同步，免除本地二次 ETL 清洗。
            </Text>
          </div>
        </div>

        <Space>
          <Button
            icon={<SettingOutlined />}
            className="rounded-xl h-10 px-5 font-bold border-slate-200 text-slate-600 hover:bg-slate-50 transition-all"
            onClick={() => navigate('/admin/profile?tab=data-platform')}
          >
            云端节点与 API 配置
          </Button>
        </Space>
      </div>

      {/* 多市场直供看板 Tabs */}
      <Card className="rounded-3xl border-none shadow-xl shadow-slate-200/40 bg-white overflow-hidden p-2">
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={tabItems}
          type="card"
          className="admin-market-tabs"
        />
      </Card>
    </div>
  );
};

export default AdminDataManagement;
