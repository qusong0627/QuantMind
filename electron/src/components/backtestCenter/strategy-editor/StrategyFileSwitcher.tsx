/**
 * 策略文件选择器（回测中心「策略编辑」头部下拉）。
 *
 * 旧的 280px 文件树在 1150px 内容区里只剩 222px 给 Monaco；这里把
 * 「工作空间 / 策略中心」收进一个 420px 弹层，编辑器因此拿回整块画布。
 * 目录导航、新建、重命名、删除、发布云端的能力全部保留。
 */
import React, { useMemo } from 'react';
import { Input, Popover, Tooltip } from 'antd';
import {
  ArrowUp,
  ChevronRight,
  CloudUpload,
  Edit2,
  FileCode2,
  FilePlus2,
  Folder,
  FolderPlus,
  HelpCircle,
  RefreshCw,
  Search,
  Trash2,
} from 'lucide-react';
import type { FileWorkspaceState } from './types';

interface Props {
  files: FileWorkspaceState;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  trigger: React.ReactNode;
}

const IconButton: React.FC<{
  title: string;
  onClick: (event: React.MouseEvent) => void;
  danger?: boolean;
  children: React.ReactNode;
}> = ({ title, onClick, danger, children }) => (
  <Tooltip title={title}>
    <button
      type="button"
      onClick={onClick}
      className={`flex h-7 w-7 items-center justify-center rounded-md border border-transparent transition-colors ${
        danger
          ? 'text-slate-400 hover:border-red-100 hover:bg-red-50 hover:text-red-500'
          : 'text-slate-400 hover:border-slate-200 hover:bg-slate-50 hover:text-slate-700'
      }`}
    >
      {children}
    </button>
  </Tooltip>
);

const LoadingRows: React.FC = () => (
  <div className="space-y-1.5 px-1 py-1">
    {[0, 1, 2, 3, 4].map((i) => (
      <div key={i} className="h-8 animate-pulse rounded-lg bg-slate-100" />
    ))}
  </div>
);

const EmptyState: React.FC<{ text: string }> = ({ text }) => (
  <div className="flex h-[180px] flex-col items-center justify-center gap-2 text-center">
    <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50">
      <FileCode2 className="h-4 w-4 text-slate-300" />
    </div>
    <div className="text-[11px] text-slate-400">{text}</div>
  </div>
);

export const StrategyFileSwitcher: React.FC<Props> = ({ files, open, onOpenChange, trigger }) => {
  const query = files.searchQuery.trim().toLowerCase();
  const localItems = useMemo(
    () => files.localFiles.filter((item) => !query || item.name.toLowerCase().includes(query)),
    [files.localFiles, query],
  );
  const remoteItems = useMemo(
    () => files.remoteFiles.filter((item) => !query || item.name.toLowerCase().includes(query)),
    [files.remoteFiles, query],
  );
  const isLocal = files.tab === 'local';
  const list = isLocal ? localItems : remoteItems;
  const isLoading = isLocal ? files.isLoadingLocal : files.isLoadingRemote;

  const content = (
    <div className="w-[424px]">
      {/* 顶部：来源切换 + 搜索 */}
      <div className="space-y-2.5 border-b border-slate-100 p-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex rounded-lg bg-slate-100/80 p-0.5">
            {(['local', 'remote'] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                onClick={() => files.onTabChange(tab)}
                className={`rounded-md px-3 py-1 text-[11px] font-semibold transition-all ${
                  files.tab === tab
                    ? 'bg-white text-slate-900 shadow-sm'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
              >
                {tab === 'local' ? '工作空间' : '策略中心'}
              </button>
            ))}
          </div>
          <span className="text-[10px] text-slate-400">
            {isLoading ? '加载中…' : `${list.length} 个`}
          </span>
        </div>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-300" />
          <Input
            size="small"
            allowClear
            value={files.searchQuery}
            onChange={(event) => files.onSearchQueryChange(event.target.value)}
            placeholder={isLocal ? '搜索工作空间文件…' : '搜索云端策略…'}
            className="!h-8 !rounded-lg !border-slate-200 !bg-slate-50/70 !pl-8 !text-[11.5px]"
          />
        </div>
        {isLocal && (
          <div className="flex items-center justify-between gap-2">
            <button
              type="button"
              onClick={files.onGoParent}
              disabled={!files.parentDir && files.parentDir !== ''}
              className="inline-flex min-w-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-[10.5px] text-slate-500 transition-colors hover:bg-slate-50 hover:text-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
              title="返回上级目录"
            >
              <ArrowUp className="h-3 w-3" />
              <span className="truncate font-mono">
                {files.currentDir ? files.currentDir : '根目录'}
              </span>
            </button>
            <div className="flex items-center gap-0.5">
              <IconButton
                title="新建文件"
                onClick={() => {
                  onOpenChange(false);
                  files.onCreateFile();
                }}
              >
                <FilePlus2 className="h-3.5 w-3.5" />
              </IconButton>
              <IconButton
                title="新建文件夹"
                onClick={() => {
                  onOpenChange(false);
                  files.onCreateFolder();
                }}
              >
                <FolderPlus className="h-3.5 w-3.5" />
              </IconButton>
              <IconButton title="刷新" onClick={files.onRefresh}>
                <RefreshCw className="h-3.5 w-3.5" />
              </IconButton>
            </div>
          </div>
        )}
      </div>

      {/* 列表 */}
      <div className="se-scroll h-[286px] overflow-y-auto p-1.5">
        {isLoading ? (
          <LoadingRows />
        ) : list.length === 0 ? (
          <EmptyState
            text={
              query
                ? '没有匹配的策略文件'
                : isLocal
                  ? '当前目录是空的，点右上角新建一个 .py'
                  : '策略中心还没有策略'
            }
          />
        ) : isLocal ? (
          localItems.map((item) => {
            const isFolder = Boolean(item.is_dir) || item.type === 'folder' || item.type === 'dir';
            const selected = files.selectedFile?.path === item.path && !isFolder;
            return (
              <div
                key={item.id || item.path}
                className="group se-file-row"
                data-selected={selected}
              >
                <button
                  type="button"
                  className="flex min-w-0 flex-1 items-center gap-2 text-left"
                  onClick={() => {
                    if (isFolder) {
                      files.onEnterDirectory(item.path);
                      return;
                    }
                    files.onSelectFile(item);
                    onOpenChange(false);
                  }}
                  title={item.path}
                >
                  {isFolder ? (
                    <Folder className="h-3.5 w-3.5 flex-shrink-0 text-amber-400" />
                  ) : (
                    <FileCode2 className="h-3.5 w-3.5 flex-shrink-0 text-blue-500" />
                  )}
                  <span
                    className={`min-w-0 flex-1 truncate text-[11.5px] ${
                      selected ? 'font-semibold text-blue-700' : 'text-slate-600'
                    }`}
                  >
                    {item.name}
                  </span>
                  {isFolder && <ChevronRight className="h-3 w-3 flex-shrink-0 text-slate-300" />}
                </button>
                {!isFolder && (
                  <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                    <IconButton
                      title="重命名"
                      onClick={(event) => files.onRenameFile(item, event)}
                    >
                      <Edit2 className="h-3 w-3" />
                    </IconButton>
                    <IconButton
                      title="删除"
                      danger
                      onClick={(event) => files.onDeleteFile(item.path, event)}
                    >
                      <Trash2 className="h-3 w-3" />
                    </IconButton>
                  </div>
                )}
              </div>
            );
          })
        ) : (
          remoteItems.map((item) => {
            const selected = files.selectedRemote?.id === item.id;
            return (
              <button
                key={item.id}
                type="button"
                className="se-file-row"
                data-selected={selected}
                onClick={() => {
                  files.onSelectRemote(item);
                  onOpenChange(false);
                }}
                title={item.description || item.name}
              >
                <FileCode2 className="h-3.5 w-3.5 flex-shrink-0 text-indigo-500" />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span
                    className={`truncate text-[11.5px] ${
                      selected ? 'font-semibold text-blue-700' : 'text-slate-600'
                    }`}
                  >
                    {item.name}
                  </span>
                  {item.updated_at && (
                    <span className="font-mono text-[9.5px] text-slate-400">
                      更新 {String(item.updated_at).slice(0, 16).replace('T', ' ')}
                    </span>
                  )}
                </span>
              </button>
            );
          })
        )}
      </div>

      {/* 底部 */}
      <div className="flex items-center justify-between gap-2 border-t border-slate-100 bg-slate-50/50 px-3 py-2">
        <div className="min-w-0 truncate font-mono text-[9.5px] text-slate-400">
          {isLocal
            ? files.selectedFile?.path || '未选择文件'
            : files.selectedRemote?.name || '未选择云端策略'}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {isLocal && files.selectedFile && (
            <button
              type="button"
              onClick={() => {
                onOpenChange(false);
                files.onUploadToCloud();
              }}
              className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-medium text-indigo-600 transition-colors hover:bg-indigo-50"
            >
              <CloudUpload className="h-3 w-3" />
              发布云端
            </button>
          )}
          <button
            type="button"
            onClick={() => files.onRefresh()}
            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[10px] text-slate-500 transition-colors hover:bg-white hover:text-slate-800"
          >
            <RefreshCw className="h-3 w-3" />
            刷新
          </button>
          <a
            href="https://www.quantmindai.cn/help"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="帮助文档"
            title="帮助文档"
            className="inline-flex h-6 w-6 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-white hover:text-blue-600"
          >
            <HelpCircle className="h-3 w-3" />
          </a>
        </div>
      </div>
    </div>
  );

  return (
    <Popover
      content={content}
      trigger="click"
      placement="bottomLeft"
      open={open}
      onOpenChange={onOpenChange}
      arrow={false}
      overlayClassName="se-file-popover"
    >
      {trigger}
    </Popover>
  );
};

export default StrategyFileSwitcher;
