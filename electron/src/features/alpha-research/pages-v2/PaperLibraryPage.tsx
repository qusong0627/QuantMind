import React, { useState } from 'react';
import { BookOpen } from 'lucide-react';
import { MyPapersPanel } from '../components-v2/MyPapersPanel';
import { DocumentCenterPage } from './DocumentCenterPage';

export const PaperLibraryPage: React.FC = () => {
  const [documentRefresh, setDocumentRefresh] = useState(0);
  return (
    <div className="space-y-4 animate-fade-in-up">
      <div>
        <h1 className="text-xl font-bold flex items-center gap-2">
          <BookOpen className="h-5 w-5 text-primary" />
          论文库
        </h1>
        <p className="text-xs text-muted-foreground mt-0.5">
          保存和管理 PDF 原文。开源版暂不支持论文解析及由论文发起挖掘。
        </p>
      </div>
      <MyPapersPanel onImported={() => setDocumentRefresh((value) => value + 1)} />
      <DocumentCenterPage hideHeader refreshToken={documentRefresh} />
    </div>
  );
};
