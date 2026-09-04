/**
 * 新闻情感（管理员）
 *
 * 将 RSS 源管理、标签词典、FinBERT 模型三个新闻情绪处理环节合并为一个
 * 「新闻情感」菜单，内部用 Tabs 切换，保持各自独立交互形态。
 */

import React, { useState } from 'react';
import { Tabs } from 'antd';
import { DisconnectOutlined, TagOutlined, ExperimentOutlined } from '@ant-design/icons';
import AdminRssSources from './AdminRssSources';
import AdminTagManagement from './AdminTagManagement';
import AdminFinbertModel from './AdminFinbertModel';

const AdminNewsEmotion: React.FC = () => {
  const [activeKey, setActiveKey] = useState('sources');

  return (
    <div className="pb-4">
      <Tabs
        activeKey={activeKey}
        onChange={setActiveKey}
        tabBarStyle={{ padding: '0 16px', marginBottom: 0 }}
        items={[
          {
            key: 'sources',
            label: (
              <span>
                <DisconnectOutlined style={{ marginRight: 6 }} />
                源管理
              </span>
            ),
            children: <AdminRssSources />,
          },
          {
            key: 'tags',
            label: (
              <span>
                <TagOutlined style={{ marginRight: 6 }} />
                标签词典
              </span>
            ),
            children: <AdminTagManagement />,
          },
          {
            key: 'finbert',
            label: (
              <span>
                <ExperimentOutlined style={{ marginRight: 6 }} />
                FinBERT
              </span>
            ),
            children: <AdminFinbertModel />,
          },
        ]}
      />
    </div>
  );
};

export default AdminNewsEmotion;