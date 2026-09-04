/**
 * 股票报告（Stock Report）— 分析报告档案库 + PDF 预览
 *
 * 左栏：报告文件管理（文件夹 + 文件列表 + 多选删除 + 新建文件夹）
 * 右栏：选中 PDF 的 PdfPreview（PDF.js 渲染，白底无缩略图栏）
 * 顶部：引导横幅（提示用户先通过 QuantBot 技能生成报告）
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  FileText,
  FolderPlus,
  Folder,
  Trash2,
  ChevronRight,
  ChevronDown,
  RefreshCw,
  File as FileIcon,
  Info,
} from 'lucide-react';
import PdfPreview from '../components/PdfPreview';
import { Modal } from 'antd';
import { SERVICE_URLS } from '../../../config/services';

// 用前端配置的服务器地址（桌面端设置 / 环境变量），不走 vite 代理，随用户配置 IP 变化
const ENGINE_BASE = (): string => `${SERVICE_URLS.API_GATEWAY}/api/v1/trading-agents`;

/** PDF 预览错误边界：单个 PDF 渲染失败不拖垮整个页面 */
class PdfErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: string | null }> {
  state = { error: null as string | null };

  static getDerivedStateFromError(err: any) {
    return { error: err?.message || String(err) };
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{ textAlign: 'center', padding: 60, color: '#ef4444', fontSize: 13 }}>
          ⚠️ PDF 渲染失败：{this.state.error}
          <br />
          <button
            onClick={() => this.setState({ error: null })}
            style={{ marginTop: 16, padding: '6px 16px', border: '1px solid #e5e7eb', borderRadius: 6, background: '#fff', cursor: 'pointer' }}
          >
            重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

interface ReportFile {
  filename: string;
  ticker: string;
  date: string;
  time?: string;
  name: string;
  signal: string | null;
  size: number;
  modified: number;
}

interface ReportFolder {
  name: string;
  files: ReportFile[];
  /** 股票名子文件夹（市场文件夹内第二层） */
  subfolders?: ReportFolder[];
}

interface FileListResponse {
  root: string;
  folders: ReportFolder[];
  files: ReportFile[];
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = localStorage.getItem('access_token');
  const resp = await fetch(`${ENGINE_BASE()}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...((options?.headers as Record<string, string>) || {}),
    },
    ...options,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `Request failed: ${resp.status}`);
  }
  const data = await resp.json();
  return data.data ?? data;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

const SIGNAL_COLORS: Record<string, string> = {
  Buy: '#10b981',
  Overweight: '#22c55e',
  Hold: '#f59e0b',
  Underweight: '#ef4444',
  Sell: '#dc2626',
};

interface ReportManagerPageProps {
  /** 嵌入模式：去掉页面级 padding 与 32px 圆角卡片外壳，供技能中心三列布局复用 */
  embedded?: boolean;
  /** 预览方式：inline = 右侧内嵌预览面板（默认）；modal = 不渲染预览面板，点击文件经 onPreviewFile 回调给宿主弹窗预览 */
  previewMode?: 'inline' | 'modal';
  onPreviewFile?: (filename: string) => void;
}

const ReportManagerPage: React.FC<ReportManagerPageProps> = ({
  embedded = false,
  previewMode = 'inline',
  onPreviewFile,
}) => {
  const [list, setList] = useState<FileListResponse>({ root: '', folders: [], files: [] });
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null); // 当前预览的文件
  const [selectedForDelete, setSelectedForDelete] = useState<Set<string>>(new Set<string>());
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set<string>());
  /** 展开的股票名子文件夹（键 = "市场/股票名"） */
  const [expandedSubfolders, setExpandedSubfolders] = useState<Set<string>>(new Set<string>());
  const [newFolderName, setNewFolderName] = useState('');
  const [showNewFolder, setShowNewFolder] = useState(false);
  const [error, setError] = useState('');
  const [previewKey, setPreviewKey] = useState(0); // 用于刷新 iframe
  const [bannerDismissed, setBannerDismissed] = useState(false); // 引导横幅已关闭
  const [showMoveFolder, setShowMoveFolder] = useState(false); // 移动到文件夹弹层
  const [moveTarget, setMoveTarget] = useState('');

  const loadFiles = useCallback(async () => {
    try {
      setLoading(true);
      const data = await request<FileListResponse>('/files/list');
      setList(data);
      setSelectedForDelete(new Set());
      setError('');
    } catch (err: any) {
      setError(err.message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadFiles();
  }, [loadFiles]);

  // 解析文件归属（根目录或某文件夹）
  const rootFiles = list.files;
  const allFolders = list.folders;

  // 文件夹内文件数（含股票名子文件夹）
  const countFolderFiles = (folder: ReportFolder): number =>
    folder.files.length + (folder.subfolders || []).reduce((s, sf) => s + sf.files.length, 0);

  // 展开文件夹全路径选项（顶层市场 + 二级股票名）
  const folderPathOptions: string[] = allFolders.flatMap((f) => [
    f.name,
    ...(f.subfolders || []).map((sf) => `${f.name}/${sf.name}`),
  ]);

  const toggleExpand = (folder: string) => {
    (setExpandedFolders as any)((prev: Set<string>) => {
      const next = new Set(prev);
      if (next.has(folder)) next.delete(folder);
      else next.add(folder);
      return next;
    });
  };

  const toggleSubfolder = (path: string) => {
    (setExpandedSubfolders as any)((prev: Set<string>) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const toggleSelect = (filename: string) => {
    (setSelectedForDelete as any)((prev: Set<string>) => {
      const next = new Set(prev);
      if (next.has(filename)) next.delete(filename);
      else next.add(filename);
      return next;
    });
  };

  const handlePreview = (filename: string) => {
    if (previewMode === 'modal') {
      onPreviewFile?.(filename);
      return;
    }
    setSelected(filename);
    (setPreviewKey as any)((k: number) => k + 1);
  };

  const handleCreateFolder = async () => {
    const name = newFolderName.trim();
    if (!name) return;
    try {
      await request('/files/create-folder', {
        method: 'POST',
        body: JSON.stringify({ folder: name }),
      });
      setNewFolderName('');
      setShowNewFolder(false);
      const top = name.split('/')[0];
      (setExpandedFolders as any)((prev: Set<string>) => new Set(prev).add(top));
      await loadFiles();
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleDeleteSelected = () => {
    if (selectedForDelete.size === 0) return;
    Modal.confirm({
      title: '删除文件',
      content: `确认删除选中的 ${selectedForDelete.size} 个文件？`,
      okText: '删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          await request('/files/delete', {
            method: 'POST',
            body: JSON.stringify({ files: Array.from(selectedForDelete) }),
          });
          if (selected && selectedForDelete.has(selected)) setSelected(null);
          await loadFiles();
        } catch (err: any) {
          setError(err.message);
        }
      },
    });
  };

  const handleMoveSelected = async (targetOverride?: string) => {
    const target = targetOverride !== undefined ? targetOverride : moveTarget;
    if (selectedForDelete.size === 0 || (!target && target !== '')) return;
    try {
      await request('/files/move', {
        method: 'POST',
        body: JSON.stringify({ files: Array.from(selectedForDelete), target_folder: target }),
      });
      if (selected && selectedForDelete.has(selected)) setSelected(null);
      setShowMoveFolder(false);
      setMoveTarget('');
      await loadFiles();
    } catch (err: any) {
      setError(err.message);
    }
  };

  // 文件总数（用于判断是否显示引导）
  const totalFiles = rootFiles.length + allFolders.reduce((s, f) => s + countFolderFiles(f), 0);
  const showBanner = !bannerDismissed && totalFiles === 0;

  const handleDeleteFolder = (path: string) => {
    Modal.confirm({
      title: '删除文件夹',
      content: `确认删除文件夹「${path}」及其中所有文件？`,
      okText: '删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          await request('/files/delete-folder', {
            method: 'POST',
            body: JSON.stringify({ folder: path }),
          });
          await loadFiles();
        } catch (err: any) {
          setError(err.message);
        }
      },
    });
  };

  const renderFileItem = (file: ReportFile, indent = 0) => {
    const isPdf = file.filename.toLowerCase().endsWith('.pdf');
    return (
      <div
        key={file.filename}
        onClick={() => isPdf && handlePreview(file.filename)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '7px 12px 7px',
          marginLeft: indent,
          cursor: isPdf ? 'pointer' : 'default',
          background: selected === file.filename ? '#eef2ff' : 'transparent',
          borderRadius: 8,
          transition: 'background 0.15s',
        }}
      >
        <input
          type="checkbox"
          checked={selectedForDelete.has(file.filename)}
          onChange={(e) => {
            e.stopPropagation();
            toggleSelect(file.filename);
          }}
          style={{ accentColor: '#6366f1', flexShrink: 0 }}
        />
        {isPdf ? (
          <FileText style={{ width: 15, height: 15, color: '#ef4444', flexShrink: 0 }} />
        ) : (
          <FileIcon style={{ width: 15, height: 15, color: '#94a3b8', flexShrink: 0 }} />
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 11.5, fontWeight: 600, color: '#1e293b', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {file.name || file.ticker || file.filename}
          </div>
          <div style={{ fontSize: 10, color: '#94a3b8', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {[file.ticker, file.date, formatTime(file.modified)].filter(Boolean).join(' · ')}
          </div>
        </div>
        {file.signal && (
          <span
            style={{
              fontSize: 9,
              fontWeight: 700,
              padding: '2px 6px',
              borderRadius: 4,
              color: '#fff',
              background: SIGNAL_COLORS[file.signal] || '#6366f1',
              flexShrink: 0,
            }}
          >
            {file.signal}
          </span>
        )}
        <span style={{ fontSize: 10, color: '#cbd5e1', flexShrink: 0 }}>{formatSize(file.size)}</span>
      </div>
    );
  };

  return (
    <div
      className={`w-full h-full flex flex-col overflow-hidden font-sans box-border select-none ${
        embedded ? '' : 'bg-[#f8fafc] p-6'
      }`}
    >
      {/* 主一体化框架 (32px 大圆角，嵌入模式由外层容器提供) */}
      <div
        className={`w-full h-full flex overflow-hidden ${
          embedded ? '' : 'bg-white border border-gray-200 shadow-sm rounded-[32px]'
        }`}
      >
        <div
          className={`shrink-0 flex flex-col bg-white overflow-hidden ${
            previewMode === 'modal' ? 'w-full flex-1' : 'w-80 border-r border-gray-200'
          }`}
        >
          <div className="p-4 border-b border-gray-200">
            <div className="text-sm font-bold text-slate-800 flex items-center gap-2">
              <div className="w-7 h-7 rounded-lg bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600">
                <FileText className="w-4 h-4" />
              </div>
              <span>股票报告档案</span>
              <button
                onClick={loadFiles}
                className="ml-auto border-none bg-transparent text-slate-400 hover:text-slate-700 cursor-pointer p-1 transition-colors"
                title="刷新列表"
              >
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
              <span className="text-[10px] font-bold bg-indigo-50 text-indigo-600 px-2 py-0.5 rounded-md border border-indigo-100/80">
                {rootFiles.length + allFolders.reduce((s, f) => s + countFolderFiles(f), 0)} 份
              </span>
            </div>
            <div style={{ display: 'flex', gap: 6, marginTop: 12 }}>
              <button
                onClick={() => setShowNewFolder(!showNewFolder)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 5,
                  padding: '5px 10px',
                  background: '#eef2ff',
                  border: 'none',
                  borderRadius: 7,
                  color: '#6366f1',
                  fontSize: 11,
                  fontWeight: 600,
                  cursor: 'pointer',
                }}
              >
                <FolderPlus style={{ width: 13, height: 13 }} /> 新建
              </button>
              <button
                onClick={handleDeleteSelected}
                disabled={selectedForDelete.size === 0}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 5,
                  padding: '5px 10px',
                  background: selectedForDelete.size ? '#fee2e2' : '#f1f5f9',
                  border: 'none',
                  borderRadius: 7,
                  color: selectedForDelete.size ? '#dc2626' : '#94a3b8',
                  fontSize: 11,
                  fontWeight: 600,
                  cursor: selectedForDelete.size ? 'pointer' : 'not-allowed',
                }}
              >
                <Trash2 style={{ width: 13, height: 13 }} /> 删除
              </button>
              <button
                onClick={() => setShowMoveFolder(!showMoveFolder)}
                disabled={selectedForDelete.size === 0}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 5,
                  padding: '5px 10px',
                  background: selectedForDelete.size ? '#eef2ff' : '#f1f5f9',
                  border: 'none',
                  borderRadius: 7,
                  color: selectedForDelete.size ? '#6366f1' : '#94a3b8',
                  fontSize: 11,
                  fontWeight: 600,
                  cursor: selectedForDelete.size ? 'pointer' : 'not-allowed',
                }}
              >
                移动
              </button>
            </div>
            {showMoveFolder && (
              <div style={{
                marginTop: 8,
                padding: '8px 10px',
                background: '#f8fafc',
                border: '1px solid #e2e8f0',
                borderRadius: 8,
                fontSize: 12,
              }}>
                <div style={{ fontWeight: 600, color: '#475569', marginBottom: 6 }}>选择目标文件夹：</div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                  <button
                    onClick={() => handleMoveSelected('')}
                    style={{
                      padding: '3px 8px',
                      background: '#fff',
                      border: '1px solid #cbd5e1',
                      borderRadius: 6,
                      fontSize: 11,
                      cursor: 'pointer',
                    }}
                  >
                    / 根目录
                  </button>
                  {allFolders.map((f) => (
                    <button
                      key={f.name}
                      onClick={() => handleMoveSelected(f.name)}
                      style={{
                        padding: '3px 8px',
                        background: '#fff',
                        border: '1px solid #cbd5e1',
                        borderRadius: 6,
                        fontSize: 11,
                        cursor: 'pointer',
                      }}
                    >
                      📁 {f.name}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {showNewFolder && (
              <div style={{
                display: 'flex',
                gap: 6,
                marginTop: 8,
                padding: '6px 8px',
                background: '#f8fafc',
                borderRadius: 8,
                border: '1px solid #e2e8f0',
              }}>
                <input
                  type="text"
                  placeholder="文件夹名称..."
                  value={newFolderName}
                  onChange={(e) => setNewFolderName(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleCreateFolder()}
                  autoFocus
                  style={{
                    flex: 1,
                    border: '1px solid #cbd5e1',
                    borderRadius: 6,
                    padding: '4px 8px',
                    fontSize: 11,
                    outline: 'none',
                  }}
                />
                <button
                  onClick={handleCreateFolder}
                  style={{
                    padding: '4px 10px',
                    background: '#6366f1',
                    border: 'none',
                    borderRadius: 7,
                    color: '#fff',
                    fontSize: 11,
                    cursor: 'pointer',
                  }}
                >
                  创建
                </button>
              </div>
            )}
          </div>

          <div style={{ flex: 1, overflowY: 'auto', padding: '10px 8px' }}>
            {loading ? (
              <div style={{ textAlign: 'center', padding: 40, color: '#94a3b8', fontSize: 13 }}>
                加载中...
              </div>
            ) : (
              <>
                {rootFiles.length > 0 && (
                  <div style={{ marginBottom: 8 }}>
                    <div style={{ fontSize: 11, fontWeight: 600, color: '#6366f1', padding: '4px 12px', textTransform: 'uppercase', letterSpacing: 0.5 }}>
                      全部报告
                    </div>
                    {rootFiles.map((f) => renderFileItem(f))}
                  </div>
                )}
                {allFolders.map((folder) => {
                  const subfolders = folder.subfolders || [];
                  return (
                    <div key={folder.name} style={{ marginBottom: 8 }}>
                      <div
                        onClick={() => toggleExpand(folder.name)}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 6,
                          padding: '6px 12px',
                          cursor: 'pointer',
                          borderRadius: 8,
                          fontSize: 13,
                          fontWeight: 600,
                          color: '#334155',
                        }}
                      >
                        {expandedFolders.has(folder.name) ? (
                          <ChevronDown style={{ width: 14, height: 14, color: '#94a3b8' }} />
                        ) : (
                          <ChevronRight style={{ width: 14, height: 14, color: '#94a3b8' }} />
                        )}
                        <Folder style={{ width: 15, height: 15, color: '#f59e0b' }} />
                        <span style={{ flex: 1 }}>{folder.name}</span>
                        <span style={{ fontSize: 10, color: '#94a3b8' }}>{countFolderFiles(folder)}</span>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDeleteFolder(folder.name);
                          }}
                          style={{
                            border: 'none',
                            background: 'transparent',
                            color: '#cbd5e1',
                            cursor: 'pointer',
                            padding: 2,
                          }}
                          title={`删除 ${folder.name}`}
                        >
                          <Trash2 style={{ width: 12, height: 12 }} />
                        </button>
                      </div>
                      {expandedFolders.has(folder.name) && (
                        <div style={{ marginTop: 2 }}>
                          {folder.files.map((f) => renderFileItem(f, 12))}
                          {subfolders.map((sub) => {
                            const subPath = `${folder.name}/${sub.name}`;
                            const isSubOpen = expandedSubfolders.has(subPath);
                            return (
                              <div key={sub.name} style={{ marginLeft: 12, marginBottom: 4 }}>
                                <div
                                  onClick={() => toggleSubfolder(subPath)}
                                  style={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: 6,
                                    padding: '5px 10px',
                                    cursor: 'pointer',
                                    borderRadius: 6,
                                    fontSize: 12,
                                    fontWeight: 500,
                                    color: '#475569',
                                    background: isSubOpen ? 'rgba(99,102,241,0.05)' : 'transparent',
                                  }}
                                >
                                  {isSubOpen ? (
                                    <ChevronDown style={{ width: 12, height: 12, color: '#94a3b8' }} />
                                  ) : (
                                    <ChevronRight style={{ width: 12, height: 12, color: '#94a3b8' }} />
                                  )}
                                  <Folder style={{ width: 13, height: 13, color: '#6366f1' }} />
                                  <span style={{ flex: 1 }}>{sub.name}</span>
                                  <span style={{ fontSize: 10, color: '#94a3b8' }}>{sub.files.length}</span>
                                </div>
                                {isSubOpen && (
                                  <div style={{ marginTop: 2 }}>
                                    {sub.files.map((f) => renderFileItem(f, 20))}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </>
            )}
          </div>
          <div style={{
            padding: '10px 14px',
            borderTop: '1px solid #f1f5f9',
            fontSize: 11,
            color: '#94a3b8',
            lineHeight: 1.5,
          }}>
            提示：勾选文件可多选删除；分析完成后 md + PDF 自动归档到「市场文件夹 → 股票名文件夹」。
          </div>
        </div>
        {previewMode !== 'modal' && (
        <div className="flex-1 min-w-0 flex flex-col bg-gray-50/50 overflow-hidden">
          {selected ? (
            <>
              <div className="flex items-center gap-2 px-5 py-3 border-b border-gray-200 bg-white shrink-0">
                <FileText className="w-4 h-4 text-indigo-600" />
                <span className="text-xs font-bold text-slate-800">{selected}</span>
                <a
                  href={`${ENGINE_BASE()}/files/pdf/${encodeURIComponent(selected)}`}
                  target="_blank"
                  rel="noreferrer"
                  className="ml-auto px-3 py-1 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-xs font-bold transition-colors shadow-2xs no-underline"
                >
                  新窗口打开
                </a>
              </div>
              <div className="flex-1 min-h-0 overflow-y-auto p-4 flex flex-col items-center">
                <PdfErrorBoundary>
                  <PdfPreview
                    key={previewKey}
                    url={`${ENGINE_BASE()}/files/pdf/${encodeURIComponent(selected)}`}
                    filename={selected}
                  />
                </PdfErrorBoundary>
              </div>
            </>
          ) : (
            <div className="flex-1 flex flex-col items-center justify-center text-center p-10">
              <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center mb-4 shadow-md text-white">
                <FileText className="w-8 h-8" />
              </div>
              <div className="text-base font-black text-slate-800 mb-2">
                选择左侧报告查看 PDF 预览
              </div>
              <div className="text-xs text-slate-400 max-w-sm leading-relaxed">
                在 QuantBot 中输入「深度分析某只股票」，分析完成后自动导出 md + PDF，
                这里就能预览完整分析内容（7 分析师 → 多空辩论 → 风控评估 → 最终决策）。
              </div>
            </div>
          )}
        </div>
        )}
      </div>
    </div>
  );
};

export default ReportManagerPage;
