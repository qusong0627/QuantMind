/**
 * 技能中心 — 提示词库 + 报告档案 一体页
 *
 * 布局对齐全站 PAGE_LAYOUT：32px 外框 + 60px 顶栏 + 三列主体
 * 左中为提示词（分类/搜索/详情），右为报告档案，PDF 弹窗预览
 */
import React, { useState } from 'react';
import { Modal } from 'antd';
import { Sparkles } from 'lucide-react';
import { PAGE_LAYOUT } from '../../../config/pageLayout';
import PromptsLibrary from '../components/PromptsLibrary';
import ReportManagerPage from '../../trading-agents/pages/ReportManagerPage';
import PdfPreview from '../../trading-agents/components/PdfPreview';
import { PROMPTS } from '../prompts.generated';
import { SERVICE_URLS } from '../../../config/services';

// 用前端配置的服务器地址（桌面端设置 / 环境变量），不走 vite 代理，随用户配置 IP 变化
const ENGINE_BASE = (): string => `${SERVICE_URLS.API_GATEWAY}/api/v1/trading-agents`;

const SkillsCenterPage: React.FC = () => {
  const [previewFile, setPreviewFile] = useState<string | null>(null);

  return (
    <div className={PAGE_LAYOUT.outerClass}>
      <div className={PAGE_LAYOUT.frameClass}>
        {/* 顶栏：与 ModelTraining / StockTerminal 一致 */}
        <header className={PAGE_LAYOUT.headerClass} style={{ height: `${PAGE_LAYOUT.headerHeight}px` }}>
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-10 h-10 bg-gradient-to-br from-blue-500 to-purple-500 rounded-2xl flex items-center justify-center shadow-lg shrink-0">
              <Sparkles className="w-5 h-5 text-white" />
            </div>
            <div className="flex items-center gap-2.5 ml-1 min-w-0">
              <h1 className="text-xl font-bold text-slate-800 tracking-tight">技能中心</h1>
              <div className="h-4 w-[1px] bg-slate-200 self-center shrink-0" />
              <span className="text-sm font-medium text-slate-500 truncate">提示词库 · 报告档案</span>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <span className="hidden sm:inline-flex items-center gap-1.5 rounded-full bg-slate-100 border border-slate-200 px-3 py-1 text-[11px] font-bold text-slate-500">
              <span className="h-1.5 w-1.5 rounded-full bg-indigo-500" />
              {PROMPTS.length} 提示词
            </span>
            <span className="hidden sm:inline-flex items-center rounded-full bg-indigo-50 border border-indigo-100 px-2.5 py-1 text-[11px] font-bold text-indigo-600">
              复制到 QuantBot 即用
            </span>
          </div>
        </header>

        {/* 主体：左中提示词 + 右报告档案 */}
        <div className="flex flex-1 min-h-0 min-w-0 overflow-hidden">
          {/* 左 + 中：提示词库（占满剩余，自带左侧列表+详情） */}
          <div className="flex-1 min-w-0 flex overflow-hidden bg-gray-50/30">
            <PromptsLibrary />
          </div>

          {/* 右列：报告档案（320px，独立滚动） */}
          <div className="w-[320px] shrink-0 border-l border-gray-200 bg-white overflow-hidden flex flex-col">
            <ReportManagerPage embedded previewMode="modal" onPreviewFile={setPreviewFile} />
          </div>
        </div>
      </div>

      {/* PDF 预览弹窗 */}
      <Modal
        open={!!previewFile}
        title={<span className="text-sm font-bold text-slate-800 break-all">{previewFile}</span>}
        onCancel={() => setPreviewFile(null)}
        footer={null}
        width="82vw"
        centered
        destroyOnHidden
      >
        {previewFile && (
          <PdfPreview
            url={`${ENGINE_BASE()}/files/pdf/${encodeURIComponent(previewFile)}`}
            filename={previewFile}
            height="calc(100vh - 220px)"
          />
        )}
      </Modal>
    </div>
  );
};

export default SkillsCenterPage;
