import React from 'react';
import { BookOpen } from 'lucide-react';
import { MyPapersPanel } from '../components-v2/MyPapersPanel';
import { DocumentCenterPage } from './DocumentCenterPage';

export const PaperLibraryPage: React.FC = () => {
  return (
    <div className="space-y-4 animate-fade-in-up">
      <div>
        <h1 className="text-xl font-bold flex items-center gap-2">
          <BookOpen className="h-5 w-5 text-primary" />
          论文库
        </h1>
        <p className="text-xs text-muted-foreground mt-0.5">
          本人上传的论文持久保存，支持下载与删除；下方研究文档走解析整理流程，解析结果可供文档驱动挖掘使用。
        </p>
      </div>
      <MyPapersPanel />
      <DocumentCenterPage hideHeader />
    </div>
  );
};
