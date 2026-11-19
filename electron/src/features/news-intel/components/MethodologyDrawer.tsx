/** 研究口径右侧抽屉：标签字典、决策卡、来源质量、Do-Not-Redo。 */

import { Drawer } from 'antd';
import { MethodologyBoard } from './MethodologyBoard';

interface MethodologyDrawerProps {
  open: boolean;
  onClose: () => void;
}

export function MethodologyDrawer({ open, onClose }: MethodologyDrawerProps) {
  return (
    <Drawer
      open={open}
      onClose={onClose}
      placement="right"
      width={780}
      className="ni-article-drawer ni-method-drawer"
      title="研究口径"
      styles={{ body: { padding: 0 } }}
    >
      <MethodologyBoard />
    </Drawer>
  );
}
