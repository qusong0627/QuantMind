import React from 'react';
import { Row, Col, Typography } from 'antd';
import { StockPoolTable } from './StockPoolTable';
import { StockPoolLibrary } from './StockPoolLibrary';

export const CustomStockSelector: React.FC = () => {
  return (
    <div style={{ height: '100%', padding: '0', marginBottom: 20 }}>
      <Row gutter={20} style={{ height: '100%' }}>
        {/* 左侧：资产库 (更紧凑) */}
        <Col span={6} style={{ height: '100%' }}>
          <StockPoolLibrary />
        </Col>

        {/* 右侧：主表格区域 */}
        <Col span={18} style={{ height: '100%' }}>
          <div style={{ 
            height: '100%', 
            background: '#fff', 
            borderRadius: 12,
            border: '1px solid #f0f0f0',
            padding: '16px 20px',
            display: 'flex',
            flexDirection: 'column'
          }}>
            <StockPoolTable />
          </div>
        </Col>
      </Row>
    </div>
  );
};
