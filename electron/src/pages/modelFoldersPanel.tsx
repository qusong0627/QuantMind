/**
 * 模型资产左栏「文件夹」区 + 新建/重命名弹窗。
 *
 * 交互对齐文件管理器：文件夹行更大（图标 18 / 名称 12px），整行可点；
 * 展开后在文件夹下方直接列出模型（点击即打开右侧详情），同时下方模型列表
 * 过滤到该文件夹。状态与持久化仍由 ModelRegistryPage 持有。
 */
import React from 'react';
import { Dropdown, Input, Modal, Tooltip } from 'antd';
import { clsx } from 'clsx';
import {
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  FolderPlus,
  Inbox,
  Layers,
  ListChecks,
  MoreHorizontal,
  Pencil,
  Star,
  Trash2,
} from 'lucide-react';
import type { UserModelRecord } from '../services/modelTrainingService';
import { modelDisplayName } from './modelRegistryUtils';
import {
  FOLDER_NAME_MAX_LENGTH,
  UNFILED_FOLDER_KEY,
  type FolderCounts,
  type ModelFolder,
} from './modelRegistryFolders';

const STATUS_DOT: Record<string, string> = {
  active: 'bg-emerald-500',
  ready: 'bg-emerald-500',
  candidate: 'bg-amber-400',
  archived: 'bg-slate-300',
};

/** 左栏文件夹行：主区域是按钮，右侧挂管理菜单。 */
export const FolderRow: React.FC<{
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  /** 可展开的文件夹：左侧显示箭头；普通筛选项传空。 */
  expandable?: boolean;
  expanded?: boolean;
  actions?: React.ReactNode;
}> = ({ label, count, active, onClick, icon, expandable = false, expanded = false, actions }) => (
  <div
    className={clsx(
      'group flex items-center rounded-xl transition-colors select-none',
      active
        ? 'bg-white shadow-sm ring-1 ring-blue-100'
        : 'hover:bg-white/80',
    )}
  >
    <button
      type="button"
      onClick={onClick}
      aria-expanded={expandable ? expanded : undefined}
      aria-pressed={active}
      className="flex items-center gap-2 min-w-0 flex-1 px-2.5 py-2 text-left"
    >
      <span className="w-4 shrink-0 flex items-center justify-center text-slate-400">
        {expandable
          ? (expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />)
          : null}
      </span>
      <span className="shrink-0 flex items-center justify-center">{icon}</span>
      <span
        className={clsx(
          'text-xs font-bold truncate flex-1',
          active ? 'text-blue-700' : 'text-slate-700',
        )}
      >
        {label}
      </span>
      <span
        className={clsx(
          'shrink-0 min-w-[22px] text-center px-1.5 py-0.5 rounded-md text-[10px] font-mono font-black',
          active ? 'bg-blue-100 text-blue-600' : 'bg-slate-100 text-slate-500',
        )}
      >
        {count}
      </span>
    </button>
    {actions && (
      <span className="pr-1.5 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity shrink-0">
        {actions}
      </span>
    )}
  </div>
);

export interface ModelFoldersPanelProps {
  folders: ModelFolder[];
  counts: FolderCounts;
  activeFolder: string | null;
  expandedFolder: string | null;
  /** 展开文件夹内联展示的模型（已按搜索/排序处理，并由页面截断）。 */
  expandedModels: UserModelRecord[];
  /** 超出内联展示上限的模型数。 */
  expandedOverflow: number;
  selectedModelId: string | null;
  open: boolean;
  organizeMode: boolean;
  onToggleOpen: () => void;
  onToggleOrganize: () => void;
  onSelectFolder: (filter: string | null) => void;
  onSelectModel: (modelId: string) => void;
  onCreateFolder: () => void;
  onRenameFolder: (folderId: string) => void;
  onDeleteFolder: (folderId: string) => void;
}

export const ModelFoldersPanel: React.FC<ModelFoldersPanelProps> = ({
  folders,
  counts,
  activeFolder,
  expandedFolder,
  expandedModels,
  expandedOverflow,
  selectedModelId,
  open,
  organizeMode,
  onToggleOpen,
  onToggleOrganize,
  onSelectFolder,
  onSelectModel,
  onCreateFolder,
  onRenameFolder,
  onDeleteFolder,
}) => (
  <div className="px-4 pb-2 border-b border-slate-50">
    <div className="flex items-center justify-between mb-1.5">
      <button
        type="button"
        onClick={onToggleOpen}
        aria-expanded={open}
        className="flex items-center gap-1 text-[10px] font-black tracking-widest text-slate-500 hover:text-slate-700 transition-colors"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        文件夹
        <span className="text-slate-300 font-mono">{folders.length}</span>
      </button>
      <div className="flex items-center gap-0.5">
        <Tooltip title={organizeMode ? '退出整理模式' : '整理模式（多选模型批量移动）'}>
          <button
            type="button"
            aria-label={organizeMode ? '退出整理模式' : '整理模式'}
            onClick={onToggleOrganize}
            className={clsx(
              'w-7 h-7 flex items-center justify-center rounded-lg transition-all',
              organizeMode
                ? 'text-blue-600 bg-blue-50 ring-1 ring-blue-200'
                : 'text-slate-400 hover:text-blue-600 hover:bg-blue-50',
            )}
          >
            <ListChecks size={14} />
          </button>
        </Tooltip>
        <Tooltip title="新建文件夹">
          <button
            type="button"
            aria-label="新建文件夹"
            onClick={onCreateFolder}
            className="w-7 h-7 flex items-center justify-center rounded-lg text-slate-400 hover:text-blue-600 hover:bg-blue-50 transition-all"
          >
            <FolderPlus size={14} />
          </button>
        </Tooltip>
      </div>
    </div>
    {open && (
      <div className="space-y-0.5 max-h-[42vh] overflow-y-auto custom-scrollbar rounded-xl bg-slate-50/70 p-1">
        <FolderRow
          label="全部模型"
          count={counts.total}
          active={activeFolder === null}
          onClick={() => onSelectFolder(null)}
          icon={<Layers size={16} className="text-blue-400" />}
        />
        <FolderRow
          label="未分组"
          count={counts.unfiled}
          active={activeFolder === UNFILED_FOLDER_KEY}
          onClick={() => onSelectFolder(UNFILED_FOLDER_KEY)}
          icon={<Inbox size={16} className="text-slate-400" />}
        />
        {folders.map((folder) => {
          const expanded = expandedFolder === folder.id;
          return (
            <div key={folder.id}>
              <FolderRow
                label={folder.name}
                count={counts.byFolder[folder.id] ?? 0}
                active={activeFolder === folder.id}
                onClick={() => onSelectFolder(folder.id)}
                expandable
                expanded={expanded}
                icon={expanded
                  ? <FolderOpen size={17} className="text-amber-500" />
                  : <Folder size={17} className="text-amber-400" fill="currentColor" fillOpacity={0.18} />}
                actions={(
                  <Dropdown
                    trigger={['click']}
                    placement="bottomRight"
                    menu={{
                      items: [
                        { key: 'rename', label: '重命名', icon: <Pencil size={12} /> },
                        { key: 'delete', label: '删除文件夹', icon: <Trash2 size={12} />, danger: true },
                      ],
                      onClick: ({ key }) => {
                        if (key === 'rename') onRenameFolder(folder.id);
                        else onDeleteFolder(folder.id);
                      },
                    }}
                  >
                    <button
                      type="button"
                      aria-label={`管理文件夹 ${folder.name}`}
                      className="w-6 h-6 flex items-center justify-center rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-200/70 transition-colors"
                    >
                      <MoreHorizontal size={14} />
                    </button>
                  </Dropdown>
                )}
              />
              {expanded && (
                <div className="ml-[30px] mr-1.5 mt-0.5 mb-1 border-l border-slate-200 pl-2 space-y-0.5">
                  {expandedModels.map((model) => (
                    <button
                      key={model.model_id}
                      type="button"
                      onClick={() => onSelectModel(model.model_id)}
                      title={model.model_id}
                      className={clsx(
                        'w-full flex items-center gap-1.5 px-2 py-1 rounded-lg text-left transition-colors',
                        selectedModelId === model.model_id
                          ? 'bg-blue-50 text-blue-700'
                          : 'text-slate-600 hover:bg-white hover:text-slate-800',
                      )}
                    >
                      <span
                        className={clsx(
                          'w-1.5 h-1.5 rounded-full shrink-0',
                          STATUS_DOT[model.status] ?? 'bg-slate-400',
                        )}
                      />
                      <span className="text-[11px] font-bold truncate flex-1">
                        {modelDisplayName(model)}
                      </span>
                      {model.is_default && (
                        <Star size={10} fill="currentColor" className="text-amber-400 shrink-0" />
                      )}
                    </button>
                  ))}
                  {expandedModels.length === 0 && (
                    <p className="px-2 py-1 text-[10px] text-slate-400">这个文件夹还是空的</p>
                  )}
                  {expandedOverflow > 0 && (
                    <p className="px-2 py-1 text-[10px] text-slate-400">
                      还有 {expandedOverflow} 个，见下方模型列表
                    </p>
                  )}
                </div>
              )}
            </div>
          );
        })}
        {folders.length === 0 && (
          <p className="px-2.5 py-2 text-[10px] text-slate-400 leading-relaxed">
            点右上角 + 新建文件夹，把模型按用途/周期分组。
          </p>
        )}
      </div>
    )}
  </div>
);

export interface ModelFolderModalProps {
  mode: 'create' | 'rename' | null;
  value: string;
  /** 新建后要自动移入的模型数（从卡片/整理模式触发时 >0）。 */
  moveCount: number;
  onChange: (value: string) => void;
  onCancel: () => void;
  onSubmit: () => void;
}

export const ModelFolderModal: React.FC<ModelFolderModalProps> = ({
  mode,
  value,
  moveCount,
  onChange,
  onCancel,
  onSubmit,
}) => (
  <Modal
    title={null}
    open={mode !== null}
    onCancel={onCancel}
    onOk={onSubmit}
    okText={mode === 'rename' ? '保存' : '创建'}
    cancelText="取消"
    width={460}
    centered
    okButtonProps={{ className: 'rounded-xl font-bold' }}
    cancelButtonProps={{ className: 'rounded-xl' }}
    styles={{ mask: { backdropFilter: 'blur(4px)', backgroundColor: 'rgba(0,0,0,0.2)' } }}
  >
    <div className="pt-2">
      <h3 className="text-lg font-black text-slate-800 m-0 tracking-tight flex items-center gap-2 mb-1">
        <FolderPlus size={18} className="text-blue-500" />
        {mode === 'rename' ? '重命名文件夹' : '新建文件夹'}
      </h3>
      <p className="text-[11px] text-slate-400 m-0 mb-4">
        仅用于左栏归类，不影响模型训练、推理与台账；删除文件夹不会删除模型。
      </p>
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onPressEnter={onSubmit}
        maxLength={FOLDER_NAME_MAX_LENGTH}
        showCount
        placeholder="如：T+5 日频 / 港股 / 实验组"
        className="rounded-xl"
        autoFocus
      />
      {mode === 'create' && moveCount > 0 && (
        <p className="text-[10px] text-blue-500 mt-2 mb-0">
          创建后自动移入 {moveCount} 个模型。
        </p>
      )}
    </div>
  </Modal>
);
