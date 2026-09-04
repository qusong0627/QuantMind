import React from 'react';
import { motion } from 'framer-motion';
import { useSearchParams } from 'react-router-dom';
import Editor from '@monaco-editor/react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import Prism from 'prismjs';
import 'prismjs/themes/prism.css'; // 使用基础主题，对比度高
import 'prismjs/components/prism-python';
import 'prismjs/components/prism-bash';
import 'prismjs/components/prism-json';
import {
    FolderTree,
    Play,
    Save,
    FileCode,
    Search,
    RefreshCw,
    FilePlus,
    FolderPlus,
    Edit2,
    ChevronRight,
    Trash2,
    Copy,
    Code2,
    Square,
    CheckCircle,
    Activity,
    Bot,
    AlertCircle,
    HelpCircle,
    CloudUpload,
    Sparkles,
    Bug,
    Search as SearchIcon,
} from 'lucide-react';
import { message, Modal, Input } from 'antd';
import { clsx } from 'clsx';
import { authService } from '../features/auth/services/authService';
import { strategyManagementService } from '../services/strategyManagementService';
import { modelTrainingService } from '../services/modelTrainingService';
import HelpCenterLink from '../components/common/HelpCenterLink';
import { SERVICE_ENDPOINTS } from '../config/services';
import { PAGE_LAYOUT } from '../config/pageLayout';
import { useAppSelector } from '../store';
import { selectCurrentMarket } from '../store/slices/uiSlice';
import { getMarketConfig } from '../config/marketConfig';

/**
 * AI-IDE Page Implementation
 * Features:
 * - Real-world File Management (via Python Backend)
 * - Code Execution with Streaming Logs
 * - LLM Chat Assistant with Streaming Response
 */

const normalizeApiBaseUrl = (url: string) => url.replace(/\/+$/, '');

const AI_IDE_GATEWAY_BASE_URL = normalizeApiBaseUrl(
    String((import.meta as any).env?.VITE_AI_IDE_API_BASE_URL || SERVICE_ENDPOINTS.API_GATEWAY)
);

interface FileItem {
    id: string;
    name: string;
    type: string;
    path: string;
    is_dir?: boolean;
}

const getPlatformGuideFileNames = (platform?: string | null) => {
    const normalized = String(platform || '').trim();
    if (normalized === 'win32') {
        return ['AI-IDE_完整依赖安装说明_Windows.md', 'AI-IDE_完整依赖安装说明.md'];
    }
    if (normalized === 'darwin') {
        return ['AI-IDE_完整依赖安装说明_macOS.md', 'AI-IDE_完整依赖安装说明.md'];
    }
    return ['AI-IDE_完整依赖安装说明_Linux.md', 'AI-IDE_完整依赖安装说明.md'];
};

interface Message {
    id: string;
    role: 'ai' | 'user';
    content: string;
}

interface RemoteStrategy {
    id: string;
    name: string;
    description?: string | null;
    tags?: string[];
    created_at?: string | null;
    updated_at?: string | null;
}

const AI_IDE_UNAVAILABLE_HINTS = [
    'ai-ide upstream unavailable',
    'upstream unavailable',
    'econnrefused',
    'failed to fetch',
    'networkerror',
    'fetch failed',
] as const;

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** AI 开发规则按市场注入（A 股规则逐字不变；港股给港股口径） */
const getAssistantRules = (market: string): string => {
  const cnRules = AI_ASSISTANT_DEVELOPMENT_RULES_CN;
  if (market !== 'HK') return cnRules;
  return [
    '1. 使用简体中文回答，先给结论，再给步骤。',
    '2. 涉及代码修改时，优先输出最小改动，并明确文件路径。',
    '3. 不要直接假设上下文不足；信息不全时先提问。',
    '4. 回答尽量简洁、可执行，避免长篇空话。',
    '5. 默认回测时间跨度设定为近 1 年；若信号覆盖不足，系统将自动自适应截断，请知悉并提示用户。',
    '6. 港股代码：4 位数字 + .HK（0001.HK / 0700.HK），8 开头创业板保留 5 位；QLib instrument 一律 hk_ 前缀（hk_0700.HK），池/信号对齐用带前缀名。',
    '7. 港股制度：T+0（当日可卖，T+2 交收不影响）；无涨跌停；每手按个股 board lot（平台按 1 股简化，不要写 100 股整手）。',
    '8. 港股费用：佣金约 0.03%（最低 3 HKD）、印花税 0.1% 卖出单边简化、无过户费；币种 HKD；默认基准 HSI。',
    '9. 港股因子 = l1_factors + ccass_factors + south_factors；禁止套用 A 股规则（T+1/整手/涨跌停/ST/过户费）。',
  ].join('\n');
};

const AI_ASSISTANT_DEVELOPMENT_RULES_CN = [
    '1. 使用简体中文回答，先给结论，再给步骤。',
    '2. 涉及代码修改时，优先输出最小改动，并明确文件路径。',
    '3. 不要直接假设上下文不足；信息不全时先提问。',
    '4. 回答尽量简洁、可执行，避免长篇空话。',
    '5. 默认回测时间跨度设定为近 1 年；若信号覆盖不足，系统将自动自适应截断，请知悉并提示用户。',
    '6. 股票代码双格式口径：平台内部（Redis 键、PG 表、API 参数）强制前缀式（如 SH600036）；直读 QuantDB parquet 时 symbol 是后缀式（600036.SH），查询前必须用 StockCodeUtil.to_suffix() 转换，不要混用。',
    '7. QuantDB 数据读取统一走 quantdb_hub.py；分区数据用整数 dt=YYYYMMDD 过滤；单位口径：个股 volume=股、amount=万元，指数 volume=手，市值=元。',
].join('\n');

type SetRootOptions = {
    silent?: boolean;
    retries?: number;
    source?: 'startup' | 'manual';
};

const AIIDEPage: React.FC = () => {
    const currentMarket = useAppSelector(selectCurrentMarket);
    const marketConfig = getMarketConfig(currentMarket);
    const [searchParams] = useSearchParams();

    // UI State
    const [activeTab, setActiveTab] = React.useState<'local' | 'remote'>('local');
    const [logTab, setLogTab] = React.useState<'result' | 'error'>('result');
    const [chatInput, setChatInput] = React.useState('');

    // Data State
    const [files, setFiles] = React.useState<FileItem[]>([]);
    const [remoteStrategies, setRemoteStrategies] = React.useState<RemoteStrategy[]>([]);
    const [selectedFile, setSelectedFile] = React.useState<FileItem | null>(null);
    const [selectedRemote, setSelectedRemote] = React.useState<RemoteStrategy | null>(null);
    const [editorContent, setEditorContent] = React.useState('# 请选择一个文件开始编辑');
    const [currentDir, setCurrentDir] = React.useState('');
    const [parentDir, setParentDir] = React.useState<string | null>(null);
    const [messages, setMessages] = React.useState<Message[]>([
        { id: '1', role: 'ai', content: '你好！我是你的策略开发帮手。服务器已连接，你可以开始导入或编写策略。' }
    ]);
    const [streamingMessageId, setStreamingMessageId] = React.useState<string | null>(null);
    const [logs, setLogs] = React.useState<string[]>([]);
    const [errors, setErrors] = React.useState<string[]>([]);

    // Status State
    const [isRunning, setIsRunning] = React.useState(false);
    const [jobId, setJobId] = React.useState<string | null>(null);
    const [isSaving, setIsSaving] = React.useState(false);
    const [isAITyping, setIsAITyping] = React.useState(false);
    const [assistantRole, setAssistantRole] = React.useState<'quant_analyst' | 'bug_fixer' | 'code_reviewer'>('quant_analyst');
    const [defaultModelName, setDefaultModelName] = React.useState<string>('');
    const [createMode, setCreateMode] = React.useState<'file' | 'folder' | null>(null);
    const [createName, setCreateName] = React.useState('');
    const [searchQuery, setSearchQuery] = React.useState('');
    const [apiKeyMasked, setApiKeyMasked] = React.useState('');
    const [isSavingConfig, setIsSavingConfig] = React.useState(false);
    const [isLoadingRemote, setIsLoadingRemote] = React.useState(false);
    const [isLoadingConfig, setIsLoadingConfig] = React.useState(false);
    const [isLoadingFiles, setIsLoadingFiles] = React.useState(false);

    const logEndRef = React.useRef<HTMLDivElement>(null);
    const chatTextareaRef = React.useRef<HTMLTextAreaElement>(null);
    const eventSourceRef = React.useRef<EventSource | null>(null);
    const runCancelRef = React.useRef<(() => void) | null>(null);
    const executeResultRef = React.useRef<{
        annual_return?: number;
        sharpe_ratio?: number;
        max_drawdown?: number;
        total_return?: number;
        win_rate?: number;
        total_trades?: number;
        profit_factor?: number;
        avg_win?: number;
        execution_time?: number;
        status?: 'completed' | 'failed';
    } | null>(null);
    const lastSetRootFailureRef = React.useRef<'invalid_path' | 'unavailable' | 'other' | null>(null);
    const editorRef = React.useRef<any>(null);
    const monacoRef = React.useRef<any>(null);
    const preferredBaseUrlRef = React.useRef<string>(AI_IDE_GATEWAY_BASE_URL);

    const getTenantId = () => {
        const storedUser = authService.getStoredUser() as any;
        const userTenant = String(storedUser?.tenant_id || '').trim();
        const cachedTenant = String(localStorage.getItem('tenant_id') || '').trim();
        const envTenant = String((import.meta as any).env?.VITE_TENANT_ID || '').trim();
        return userTenant || cachedTenant || envTenant || 'default';
    };

    const buildRequestHeaders = (extraHeaders?: HeadersInit, withJsonContentType = false) => {
        const headers = new Headers(extraHeaders || {});
        const token = authService.getAccessToken();
        if (token && !headers.has('Authorization')) {
            headers.set('Authorization', `Bearer ${token}`);
        }
        if (!headers.has('X-Tenant-Id') && !headers.has('x-tenant-id')) {
            headers.set('X-Tenant-Id', getTenantId());
        }
        if (withJsonContentType && !headers.has('Content-Type')) {
            headers.set('Content-Type', 'application/json');
        }
        return headers;
    };

    const apiFetch = async (path: string, init?: RequestInit, withJsonContentType = false) => {
        // AI-IDE 路径重定向：由原有的根路径转为 engine 服务下的云端子路径
        const cloudPath = path.startsWith('/files') ? `/ai-ide${path}` : 
                         path.startsWith('/execute') ? `/ai-ide${path}` : 
                         path.startsWith('/ai') ? `/ai-ide${path}` : 
                         path.startsWith('/config') ? `/ai-ide${path}` : path;
                         
        return fetch(`${AI_IDE_GATEWAY_BASE_URL}${cloudPath}`, {
            ...init,
            headers: buildRequestHeaders(init?.headers, withJsonContentType)
        });
    };

    const buildStreamUrl = (path: string) => {
        // AI-IDE 路径重定向：由原有的根路径转为 engine 服务下的云端子路径
        const cloudPath = path.startsWith('/execute') ? `/ai-ide${path}` : 
                         path.startsWith('/ai') ? `/ai-ide${path}` : path;
                         
        const url = new URL(`${preferredBaseUrlRef.current}${cloudPath}`);
        const token = authService.getAccessToken();
        if (token) {
            url.searchParams.set('access_token', token);
        }
        url.searchParams.set('tenant_id', getTenantId());
        return url.toString();
    };

    const promptForText = (
        title: string,
        initialValue = '',
        placeholder = '请输入内容'
    ): Promise<string | null> =>
        new Promise((resolve) => {
            let currentValue = initialValue;
            Modal.confirm({
                title,
                icon: null,
                okText: '确认',
                cancelText: '取消',
                content: (
                    <Input
                        autoFocus
                        defaultValue={initialValue}
                        placeholder={placeholder}
                        onChange={(e) => {
                            currentValue = e.target.value;
                        }}
                        onPressEnter={() => {
                            const finalValue = currentValue.trim();
                            if (!finalValue) {
                                message.warning('输入不能为空');
                                return;
                            }
                            resolve(finalValue);
                            Modal.destroyAll();
                        }}
                    />
                ),
                onOk: () => {
                    const finalValue = currentValue.trim();
                    if (!finalValue) {
                        message.warning('输入不能为空');
                        return Promise.reject(new Error('empty_input'));
                    }
                    resolve(finalValue);
                    return Promise.resolve();
                },
                onCancel: () => resolve(null),
            });
        });

    // 清理 SSE 连接
    React.useEffect(() => {
        return () => {
            if (eventSourceRef.current) {
                eventSourceRef.current.close();
                eventSourceRef.current = null;
            }
        };
    }, []);

    // Syntax Check
    const [syntaxError, setSyntaxError] = React.useState<string | null>(null);
    const [isCheckingSyntax, setIsCheckingSyntax] = React.useState(false);
    const [progress, setProgress] = React.useState<{ percent: number; message: string } | null>(null);
    const [finalResultSummary, setFinalResultSummary] = React.useState<{
        backtestId?: string;
        strategyName?: string;
        symbol?: string;
        benchmarkSymbol?: string;
        status?: string;
        executionTime?: string;
        metrics: Array<{ label: string; value: string }>;
        tradeStats: Array<{ label: string; value: string }>;
        points: Array<{ label: string; value: string }>;
    } | null>(null);

    React.useEffect(() => {
        if (!editorContent || activeTab !== 'local') return;

        const timer = setTimeout(async () => {
            setIsCheckingSyntax(true);
            try {
                const res = await apiFetch('/execute/check-syntax', {
                    method: 'POST',
                    body: JSON.stringify({ content: editorContent, filename: 'check.py' })
                }, true);
                const data = await res.json();
                if (data.valid) {
                    setSyntaxError(null);
                } else {
                    setSyntaxError(`Line ${data.line}: ${data.msg}`);
                }
            } catch (err) {
                console.error('Syntax check failed', err);
            } finally {
                setIsCheckingSyntax(false);
            }
        }, 800);

        return () => clearTimeout(timer);
    }, [editorContent, activeTab]);

    const openPreferredGuideFile = React.useCallback(async (
        items?: FileItem[],
        preferredNames?: string[]
    ) => {
        const list = Array.isArray(items) ? items : [];
        const platform = (window as any).electronAPI?.getPlatform
            ? (window as any).electronAPI.getPlatform()
            : null;
        const candidateNames = preferredNames && preferredNames.length > 0
            ? preferredNames
            : getPlatformGuideFileNames(platform);
        const guideFile = candidateNames
            .map((name) => list.find((item) => item.type !== 'folder' && item.name === name))
            .find(Boolean) || null;
        if (guideFile) {
            setSelectedFile(guideFile);
        }
    }, []);

    // Initial load: Fetch file list from cloud
    React.useEffect(() => {
        const init = async () => {
             // 只需要加载文件列表（云端模式下）
             await fetchLocalFileList();
             // 加载默认模型名，供执行上下文提示
             try {
                 const defaultModel = await modelTrainingService.getDefaultModel();
                 const mid = String(defaultModel?.model_id || '').trim();
                 if (mid) {
                     const meta = defaultModel?.metadata_json || {};
                     const display = String(
                         (meta as any)?.display_name || (meta as any)?.name || (meta as any)?.model_name || ''
                     ).trim();
                     setDefaultModelName(display || mid);
                 }
             } catch (err) {
                 console.warn('[AI-IDE] 默认模型解析失败', err);
             }
        };
        init();
        return () => {
            if (eventSourceRef.current) eventSourceRef.current.close();
        };
    }, []);

    React.useEffect(() => {
        if (activeTab === 'local') {
            setSelectedRemote(null);
            fetchLocalFileList();
        } else {
            setSelectedFile(null);
            setEditorContent('# 请选择一个远程策略');
            fetchRemoteStrategies();
        }
    }, [activeTab]);

    // 加载全局配置
    React.useEffect(() => {
        fetchSystemConfig();
    }, []);

    // 市场切换时切换到对应的策略文件夹
    React.useEffect(() => {
        const marketDir = `strategies/${currentMarket.toLowerCase()}`;
        setRootDirectory(marketDir).then((ok) => {
            if (ok) {
                fetchLocalFileList();
            }
        });
    }, [currentMarket]);

    // 处理 URL 参数自动加载策略
    React.useEffect(() => {
        const strategyId = searchParams.get('strategyId');
        if (strategyId) {
            // 切换到远程策略标签
            setActiveTab('remote');
            // 等待远程策略列表加载完成后自动选择
            const timer = setTimeout(() => {
                const strategy = remoteStrategies.find(s => s.id === strategyId);
                if (strategy) {
                    setSelectedRemote(strategy);
                }
            }, 500);
            return () => clearTimeout(timer);
        }
    }, [searchParams, remoteStrategies]);

    const fetchSystemConfig = async () => {
        setIsLoadingConfig(true);
        try {
            // 1. 获取 LLM API Key (通过后端接口)
            try {
                const res = await apiFetch('/config/llm');
                const data = await res.json();
                if (data?.success) {
                    setApiKeyMasked(data.masked_key || '');
                }
            } catch (e) {
                console.warn('Backend config fetch failed (possibly backend not running)', e);
            }

        } catch (err) {
            console.error('Failed to fetch system configuration', err);
        } finally {
            setIsLoadingConfig(false);
        }
    };

    const isAiIdeUnavailableError = (errMsg: string | null | undefined) => {
        const text = String(errMsg || '').toLowerCase();
        return AI_IDE_UNAVAILABLE_HINTS.some((hint) => text.includes(hint));
    };




    const normalizeChatError = (raw: string) => {
        const msg = String(raw || '').trim();
        const lower = msg.toLowerCase();
        if (lower.includes('api key not configured')) {
            return '尚未配置 AI Key，请在”个人中心 → 其他设置”中填写后重试。';
        }
        if (lower.includes('api returned status 401')) {
            return 'AI 服务鉴权失败（401），请检查 API Key 是否正确，或是否与当前模型/服务商匹配。';
        }
        if (lower.includes('404') || lower.includes('not found')) {
            return 'AI-IDE 服务暂不可用，请检查服务是否启动或联系管理员。';
        }
        if (lower.includes('403') || lower.includes('forbidden')) {
            return 'AI-IDE 服务访问被拒绝，请检查登录状态或权限配置。';
        }
        if (lower.includes('500') || lower.includes('internal server error')) {
            return 'AI-IDE 服务内部错误，请稍后重试或联系管理员。';
        }
        return msg || 'LLM 请求失败';
    };

    // Selection Effect: Load file content
    React.useEffect(() => {
        if (activeTab === 'local' && selectedFile) {
            loadFileContent(selectedFile.path);
        }
    }, [selectedFile, activeTab]);

    React.useEffect(() => {
        if (activeTab !== 'remote' || !selectedRemote) return;
        loadRemoteStrategy(selectedRemote.id);
    }, [selectedRemote, activeTab]);

    const fetchRemoteStrategies = async () => {
        setIsLoadingRemote(true);
        try {
            const items = await strategyManagementService.loadStrategies(
                userId,
                currentMarket,
              );
            // 适配字段映射：StrategyFile 转换为 RemoteStrategy (主要是 name 字段)
            const remoteItems: RemoteStrategy[] = items.map(item => ({
                id: item.id,
                name: item.name,
                description: item.description,
                tags: item.tags,
                created_at: item.created_at,
                updated_at: item.updated_at
            }));
            setRemoteStrategies(remoteItems);
        } catch (err: any) {
            console.error('Failed to fetch remote strategies', err);
            message.error(`远程策略获取失败: ${err.message}`);
            setRemoteStrategies([]);
        } finally {
            setIsLoadingRemote(false);
        }
    };

    const loadFileContent = async (filePath: string) => {
        try {
            const res = await apiFetch(`/files/${encodeURI(filePath)}`);
            const data = await res.json();
            setEditorContent(data.content);
        } catch (err) {
            console.error('Failed to load file content', err);
        }
    };

    const loadRemoteStrategy = async (strategyId: string) => {
        try {
            const strategy = await strategyManagementService.getStrategy(strategyId);
            setEditorContent(strategy.code || '# 远程策略内容为空');
        } catch (err: any) {
            console.error('Failed to load remote strategy', err);
            message.error(`远程策略加载失败: ${err.message}`);
        }
    };

    // Scroll logs to bottom
    React.useEffect(() => {
        logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [logs, errors]);

    // Keyboard Shortcuts
    React.useEffect(() => {
        const handleKeyDown = (e: KeyboardEvent) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 's') {
                e.preventDefault();
                handleSave();
            }
            if (e.key === 'F5') {
                e.preventDefault();
                handleRun();
            }
        };
        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [editorContent, selectedFile, isRunning, isSaving]);

    // 固定浅色主题，不跟随系统深色模式
    const editorTheme = 'light';

    const fetchLocalFileList = async (): Promise<FileItem[]> => {
        setIsLoadingFiles(true);
        try {
            // CN/A 也要传 market：后端按 parameters.market 排除港股（缺省则全量混列）
            const mktParam = currentMarket ? `?market=${currentMarket}` : '';
            const res = await apiFetch(`/files/list${mktParam}`);
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                const errMsg = formatError(err) || `文件列表加载失败(${res.status})`;
                message.error(`文件列表加载失败: ${errMsg}`);
                setFiles([]);
                return [];
            }
            const data = await res.json();
            const items = Array.isArray(data?.items) ? data.items : (Array.isArray(data) ? data : []);
            setFiles(items);
            if (typeof data?.parent === 'string' || data?.parent === null) setParentDir(data.parent);
            return items;
        } catch (err) {
            console.error('Failed to fetch file list', err);
            message.error('请求文件列表失败');
            return [];
        } finally {
            setIsLoadingFiles(false);
        }
    };

    const handleSave = async () => {
        if (activeTab === 'local') {
            if (!selectedFile) return;
            setIsSaving(true);
            try {
                await apiFetch(`/files/${encodeURI(selectedFile.path)}`, {
                    method: 'POST',
                    body: JSON.stringify({
                        content: editorContent,
                        parameters: { market: currentMarket === 'CN' ? 'A' : currentMarket },
                    })
                }, true);
                message.success('本地文件已保存');
                setTimeout(() => setIsSaving(false), 500);
            } catch (err) {
                console.error('Failed to save file', err);
                message.error('本地保存失败');
                setIsSaving(false);
            }
        } else {
            // 远程策略保存逻辑
            if (!selectedRemote) return;
            setIsSaving(true);
            try {
                await strategyManagementService.updateStrategy(selectedRemote.id, {
                    code: editorContent
                });
                message.success('云端策略已更新');
                setTimeout(() => setIsSaving(false), 500);
            } catch (err: any) {
                console.error('Failed to update remote strategy', err);
                message.error(`云端保存失败: ${err.message}`);
                setIsSaving(false);
            }
        }
    };

    const handleUploadToCloud = async () => {
        if (!selectedFile) return;
        const name = await promptForText('请输入策略名称', selectedFile.name.replace('.py', ''), '请输入策略名称');
        if (!name) return;

        try {
            message.loading({ content: '正在上传...', key: 'upload' });
            await strategyManagementService.saveStrategy({
                name: name,
                code: editorContent,
                description: `Created from AI-IDE: ${selectedFile.name}`,
                tags: ['AI-IDE', 'Local-Import'],
                source: 'personal',
                parameters: { market: currentMarket === 'CN' ? 'A' : currentMarket },
            });
            message.success({ content: '已保存到云端策略库', key: 'upload' });
        } catch (err: any) {
            console.error('Upload failed', err);
            message.error({ content: `上传失败: ${err.message}`, key: 'upload' });
        }
    };

    const handleCreateFile = async () => {
        setCreateMode('file');
        setCreateName('');
    };

    const handleCreateFolder = async () => {
        setCreateMode('folder');
        setCreateName('');
    };

    const handleConfirmCreate = async () => {
        const name = createName.trim();
        if (!name) {
            message.warning('名称不能为空');
            return;
        }
        if (activeTab !== 'local') {
            message.warning('远程策略不支持新建');
            return;
        }

        // 自动补全 .py 后缀
        let finalName = name;
        if (createMode === 'file' && !name.endsWith('.py')) {
            finalName = `${name}.py`;
        }

        try {
            const createPath = createMode === 'folder'
                ? '/files/create/folder'
                : '/files/create/file';

            const payload = createMode === 'folder'
                ? { name: finalName, dir: currentDir || undefined }
                : {
                    name: finalName,
                    dir: currentDir || undefined,
                    parameters: { market: currentMarket === 'CN' ? 'A' : currentMarket },
                  };

            const res = await apiFetch(createPath, {
                method: 'POST',
                body: JSON.stringify(payload)
            }, true);
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                const errMsg = formatError(err) || (createMode === 'folder' ? '创建文件夹失败' : '创建文件失败');
                Modal.error({ title: '创建失败', content: errMsg });
            } else {
                setCreateMode(null);
            }
            fetchLocalFileList();
        } catch (err) {
            console.error('Failed to create item', err);
        }
    };

    const handleCancelCreate = () => {
        setCreateMode(null);
        setCreateName('');
    };

    const handleDeleteFile = async (path: string, e: React.MouseEvent) => {
        e.stopPropagation();
        if (activeTab !== 'local') return;
        const shouldDelete = await new Promise<boolean>((resolve) => {
            Modal.confirm({
                title: '确认删除',
                content: `确定要删除 ${path} 吗？`,
                okText: '删除',
                okButtonProps: { danger: true },
                cancelText: '取消',
                onOk: () => resolve(true),
                onCancel: () => resolve(false),
            });
        });
        if (!shouldDelete) return;
        try {
            await apiFetch(`/files/${encodeURI(path)}`, { method: 'DELETE' });
            if (selectedFile?.path === path) {
                setSelectedFile(null);
                setEditorContent('# 请选择一个文件开始编辑');
            }
            message.success('删除成功');
            fetchLocalFileList();
        } catch (err) {
            console.error('Failed to delete file', err);
            message.error('删除失败');
        }
    };

    const handleRenameItem = async (oldPath: string, e: React.MouseEvent) => {
        e.stopPropagation();
        if (activeTab !== 'local') return;
        const oldBaseName = oldPath.split('/').pop() || oldPath;
        const newName = await promptForText('请输入新名称', oldBaseName, '请输入新名称');
        if (!newName || newName === oldBaseName) return;
        const parent = oldPath.includes('/') ? oldPath.split('/').slice(0, -1).join('/') : '';
        const newPath = parent ? `${parent}/${newName}` : newName;
        try {
            const res = await apiFetch('/files/rename', {
                method: 'POST',
                body: JSON.stringify({ old_path: oldPath, new_path: newPath })
            }, true);
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                const errMsg = formatError(err) || '重命名失败';
                Modal.error({ title: '重命名失败', content: errMsg });
            }
            if (selectedFile?.path === oldPath) {
                setSelectedFile(null); // Force reload
            }
            message.success('重命名成功');
            fetchLocalFileList();
        } catch (err) {
            console.error('Failed to rename item', err);
            message.error('重命名失败');
        }
    };

    const setRootDirectory = async (root: string, options: SetRootOptions = {}): Promise<boolean> => {
        lastSetRootFailureRef.current = null;
        const retries = Math.max(1, options.retries || 1);
        for (let attempt = 1; attempt <= retries; attempt++) {
            try {
                const res = await apiFetch('/files/set-root', {
                    method: 'POST',
                    body: JSON.stringify({ path: root })
                }, true);

                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    const errMsg = formatError(err) || '设置根目录失败';
                    const isInvalidPath = errMsg.toLowerCase().includes('invalid directory path');
                    const isLastAttempt = attempt >= retries;
                    const isUnavailable = isAiIdeUnavailableError(errMsg) || res.status >= 500 || res.status === 404;

                    if (!isLastAttempt && isUnavailable) {
                        await wait(300 * attempt);
                        continue;
                    }

                    if (isInvalidPath) {
                        lastSetRootFailureRef.current = 'invalid_path';
                    } else if (isUnavailable) {
                        lastSetRootFailureRef.current = 'unavailable';
                    } else {
                        lastSetRootFailureRef.current = 'other';
                    }

                    if (!options.silent) {
                        Modal.error({ title: '设置工作空间失败', content: errMsg });
                    }
                    return false;
                }

                const data = await res.json().catch(() => ({}));
                setCurrentDir('');
                setSelectedFile(null);
                setEditorContent('# 请选择一个文件开始编辑');
                localStorage.setItem('ai_ide_root_dir', String(data?.current_root || root));
                lastSetRootFailureRef.current = null;
                return true;
            } catch (err) {
                const isLastAttempt = attempt >= retries;
                if (!isLastAttempt) {
                    await wait(300 * attempt);
                    continue;
                }
                lastSetRootFailureRef.current = 'unavailable';
                console.error('Failed to set root directory', err);
                message.error('设置工作空间失败');
                return false;
            }
        }
        return false;
    };

    const formatError = (err: any): string | null => {
        if (!err) return null;
        if (typeof err === 'string') return err;
        if (typeof err.detail === 'string') return err.detail;
        if (Array.isArray(err.detail)) {
            return err.detail.map((d) => (typeof d === 'string' ? d : JSON.stringify(d))).join('\n');
        }
        if (typeof err.message === 'string') return err.message;
        try {
            return JSON.stringify(err);
        } catch {
            return null;
        }
    };

    const resolveAiIdeExecutionContext = async () => {
        const strategyId =
            activeTab === 'remote' ? String(selectedRemote?.id || '').trim() || undefined : undefined;

        let modelId: string | undefined;
        let runId: string | undefined;

        try {
            const defaultModel = await modelTrainingService.getDefaultModel();
            const defaultModelId = String(defaultModel?.model_id || '').trim();
            if (defaultModelId) {
                modelId = defaultModelId;
            }
            // 解析默认模型显示名：优先 metadata.display_name，其次 model_id
            const meta = defaultModel?.metadata_json || {};
            const displayName = String(
                (meta as any)?.display_name || (meta as any)?.name || ''
            ).trim();
            setDefaultModelName(displayName || defaultModelId || '');
        } catch (error) {
            console.warn('[AI-IDE] Failed to resolve default model', error);
        }

        if (!modelId && strategyId) {
            try {
                const binding = await modelTrainingService.getStrategyBinding(strategyId);
                const boundModelId = String(binding?.model_id || '').trim();
                if (boundModelId) {
                    modelId = boundModelId;
                }
            } catch (error) {
                console.warn('[AI-IDE] Failed to resolve strategy model binding', error);
            }
        }

        try {
            const latestRun = await modelTrainingService.getLatestInferenceRun(modelId);
            const resolvedModelId = String(latestRun?.model_id || '').trim();
            const latestRunId = String(latestRun?.run_id || '').trim();
            if (!modelId && resolvedModelId) {
                modelId = resolvedModelId;
            }
            if (latestRunId) {
                runId = latestRunId;
            }
        } catch (error) {
            console.warn('[AI-IDE] Failed to resolve latest inference run', error);
        }

        return {
            strategy_id: strategyId,
            model_id: modelId,
            run_id: runId,
            qlib_provider_uri: marketConfig.qlibProviderUri,
            qlib_region: marketConfig.qlibRegion,
            benchmark: marketConfig.benchmark,
        };
    };

    const resetExecuteResultSummary = () => {
        executeResultRef.current = null;
    };

    const syncExecuteResultSummary = () => {
        const current = executeResultRef.current;
        if (!current) return;

        const metrics: Array<{ label: string; value: string }> = [];
        const tradeStats: Array<{ label: string; value: string }> = [];
        const points: Array<{ label: string; value: string }> = [];
        const pushMetric = (label: string, value: unknown, formatter?: (v: number) => string) => {
            if (typeof value !== 'number' || !Number.isFinite(value)) return;
            metrics.push({ label, value: formatter ? formatter(value) : value.toFixed(4) });
        };

        pushMetric('总收益率', current.total_return, (v) => `${(v * 100).toFixed(2)}%`);
        pushMetric('年化收益率', current.annual_return, (v) => `${(v * 100).toFixed(2)}%`);
        pushMetric('夏普比率', current.sharpe_ratio);
        pushMetric('最大回撤', current.max_drawdown, (v) => `${(v * 100).toFixed(2)}%`);

        if (typeof current.win_rate === 'number' && Number.isFinite(current.win_rate)) {
            tradeStats.push({ label: '胜率', value: `${(current.win_rate * 100).toFixed(2)}%` });
        }
        if (typeof current.total_trades === 'number' && Number.isFinite(current.total_trades)) {
            tradeStats.push({ label: '交易数', value: `${Math.round(current.total_trades)}` });
        }
        if (typeof current.profit_factor === 'number' && Number.isFinite(current.profit_factor)) {
            tradeStats.push({ label: '盈亏比', value: `${current.profit_factor.toFixed(4)}` });
        }
        if (typeof current.avg_win === 'number' && Number.isFinite(current.avg_win)) {
            tradeStats.push({ label: '平均盈利', value: `${current.avg_win.toFixed(2)}` });
        }

        if (typeof current.total_trades === 'number' && Number.isFinite(current.total_trades)) {
            points.push({ label: '交易点数', value: `${Math.round(current.total_trades)}` });
        }

        setFinalResultSummary({
            backtestId: jobId || undefined,
            strategyName: activeTab === 'remote' ? selectedRemote?.name : selectedFile?.name,
            benchmarkSymbol: marketConfig.benchmark,
            status: current.status || 'completed',
            executionTime:
                typeof current.execution_time === 'number' && Number.isFinite(current.execution_time)
                    ? `${current.execution_time.toFixed(2)}s`
                    : undefined,
            metrics,
            tradeStats,
            points,
        });
    };

    const ingestExecuteResultLine = (line: string) => {
        const trimmed = String(line || '').trim();
        if (!trimmed) return;

        const metricMatch = trimmed.match(/^\[RESULT\]\s+([a-z_]+):\s+(-?\d+(?:\.\d+)?)$/i);
        if (metricMatch) {
            const [, key, rawValue] = metricMatch;
            const numericValue = Number(rawValue);
            if (Number.isFinite(numericValue)) {
                executeResultRef.current = {
                    ...(executeResultRef.current || {}),
                    [key]: numericValue,
                };
                syncExecuteResultSummary();
            }
            return;
        }

        const timeMatch = trimmed.match(/^\[RESULT\]\s+回测完成\s+\(耗时:\s+(-?\d+(?:\.\d+)?)s\)$/);
        if (timeMatch) {
            const numericValue = Number(timeMatch[1]);
            executeResultRef.current = {
                ...(executeResultRef.current || {}),
                execution_time: Number.isFinite(numericValue) ? numericValue : undefined,
                status: 'completed',
            };
            syncExecuteResultSummary();
            return;
        }

        if (trimmed === '[RESULT] 回测成功') {
            executeResultRef.current = {
                ...(executeResultRef.current || {}),
                status: 'completed',
            };
            syncExecuteResultSummary();
        }
    };

    const coreMetricLabels = new Set(['总收益率', '年化收益率', '夏普比率', '最大回撤', '波动率']);
    const referenceMetricLabels = new Set(['基准收益率', '超额收益 (Alpha)', '贝塔 (Beta)', '信息比率 (IR)']);
    const isReasonableMetricValue = (value: string, absLimit = 1000) => {
        const normalized = String(value || '').trim().replace(/%/g, '');
        const numeric = Number(normalized);
        return Number.isFinite(numeric) && Math.abs(numeric) <= absLimit;
    };
    const getVisibleMetrics = (items: Array<{ label: string; value: string }>, labels: Set<string>, absLimit = 1000) =>
        items.filter((item) => labels.has(item.label) && isReasonableMetricValue(item.value, absLimit));

    const renderResultSummaryCard = () => {
        if (logTab !== 'result' || !finalResultSummary) return null;

        const badgeClass =
            finalResultSummary.status === 'completed'
                ? 'bg-green-100 text-green-700'
                : finalResultSummary.status === 'failed'
                    ? 'bg-red-100 text-red-700'
                    : 'bg-blue-100 text-blue-700';

        return (
            <div className="mb-3 rounded-2xl border border-blue-100 bg-gradient-to-br from-blue-50 via-white to-white shadow-sm overflow-hidden">
                <div className="flex items-center justify-between px-4 py-3 border-b border-blue-100/80">
                    <div className="flex items-center gap-3">
                        <div className={`px-2.5 py-1 rounded-full text-[10px] font-bold ${badgeClass}`}>
                            {finalResultSummary.status === 'completed' ? '已完成' : finalResultSummary.status || 'completed'}
                        </div>
                        <div className="text-sm font-bold text-gray-800">运行结果</div>
                    </div>
                    <div className="text-[10px] text-gray-400 font-mono">
                        {finalResultSummary.backtestId ? `job_id=${finalResultSummary.backtestId}` : ''}
                    </div>
                </div>
                <div className="px-4 py-3 space-y-3">
                    <div className="grid grid-cols-2 gap-2 text-[11px] text-gray-600">
                        {finalResultSummary.strategyName && (
                            <div className="flex items-center justify-between rounded-xl bg-gray-50 px-3 py-2">
                                <span className="text-gray-400">策略</span>
                                <span className="font-medium text-gray-700 truncate ml-3">{finalResultSummary.strategyName}</span>
                            </div>
                        )}
                        {finalResultSummary.symbol && (
                            <div className="flex items-center justify-between rounded-xl bg-gray-50 px-3 py-2">
                                <span className="text-gray-400">标的</span>
                                <span className="font-medium text-gray-700">{finalResultSummary.symbol}</span>
                            </div>
                        )}
                        {finalResultSummary.benchmarkSymbol && (
                            <div className="flex items-center justify-between rounded-xl bg-gray-50 px-3 py-2">
                                <span className="text-gray-400">基准</span>
                                <span className="font-medium text-gray-700">{finalResultSummary.benchmarkSymbol}</span>
                            </div>
                        )}
                        {finalResultSummary.executionTime && (
                            <div className="flex items-center justify-between rounded-xl bg-gray-50 px-3 py-2">
                                <span className="text-gray-400">耗时</span>
                                <span className="font-medium text-gray-700">{finalResultSummary.executionTime}</span>
                            </div>
                        )}
                    </div>

                    {finalResultSummary.tradeStats.length > 0 && (
                        <div className="flex flex-wrap gap-2">
                            {finalResultSummary.tradeStats.map((item) => (
                                <span
                                    key={item.label}
                                    className="inline-flex items-center gap-1 rounded-full bg-green-50 px-3 py-1 text-[11px] text-green-700 border border-green-100"
                                >
                                    <span className="text-green-400">{item.label}</span>
                                    <span className="font-semibold">{item.value}</span>
                                </span>
                            ))}
                        </div>
                    )}

                    {getVisibleMetrics(finalResultSummary.metrics, coreMetricLabels).length > 0 && (
                        <div className="grid grid-cols-2 gap-2">
                            {getVisibleMetrics(finalResultSummary.metrics, coreMetricLabels).map((item) => (
                                <div key={item.label} className="rounded-xl border border-blue-100 bg-white px-3 py-2">
                                    <div className="text-[10px] text-gray-400 mb-0.5">{item.label}</div>
                                    <div className="text-sm font-semibold text-gray-800">{item.value}</div>
                                </div>
                            ))}
                        </div>
                    )}

                    {getVisibleMetrics(finalResultSummary.metrics, referenceMetricLabels).length > 0 && (
                        <div className="rounded-xl border border-dashed border-gray-200 bg-gray-50 px-3 py-2">
                            <div className="text-[10px] font-semibold text-gray-500 mb-2">参考指标</div>
                            <div className="flex flex-wrap gap-2">
                                {getVisibleMetrics(finalResultSummary.metrics, referenceMetricLabels).map((item) => (
                                    <span key={item.label} className="inline-flex items-center gap-1 rounded-full bg-white px-3 py-1 text-[11px] text-gray-600 border border-gray-200">
                                        <span className="text-gray-400">{item.label}</span>
                                        <span className="font-semibold text-gray-800">{item.value}</span>
                                    </span>
                                ))}
                            </div>
                        </div>
                    )}

                    {finalResultSummary.points.length > 0 && (
                        <div className="flex flex-wrap gap-2 text-[10px] text-gray-500">
                            {finalResultSummary.points.map((item) => (
                                <span key={item.label} className="rounded-full bg-gray-100 px-2.5 py-1">
                                    {item.label}: {item.value}
                                </span>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        );
    };

    const appendLogLine = (line: string) => {
        (setLogs as React.Dispatch<React.SetStateAction<string[]>>)((curr) => {
            const updated = [...curr, line];
            return updated.length > 1000 ? updated.slice(-1000) : updated;
        });
    };

    const appendErrorLine = (line: string) => {
        (setErrors as React.Dispatch<React.SetStateAction<string[]>>)((curr) => {
            const updated = [...curr, line];
            return updated.length > 1000 ? updated.slice(-1000) : updated;
        });
        setLogTab('error');
    };

    const handleRun = async () => {
        if (isRunning) return;
        if (activeTab === 'local' && !selectedFile) return;
        if (activeTab === 'remote' && !selectedRemote) return;

        // Save first if local
        if (activeTab === 'local') {
            await handleSave();
        }

        setLogs([]);
        setErrors([]);
        setProgress(null);
        setFinalResultSummary(null);
        resetExecuteResultSummary();
        setLogTab('result');
        setIsRunning(true);
        if (eventSourceRef.current) {
            eventSourceRef.current.close();
            eventSourceRef.current = null;
        }
        if (runCancelRef.current) {
            runCancelRef.current();
        }

        try {
            const executionContext = await resolveAiIdeExecutionContext();
            let res;
            if (activeTab === 'local' && selectedFile) {
                const normalizedFilePath = String(selectedFile.path || selectedFile.name || '').trim();
                const executeFilename = (() => {
                    if (!normalizedFilePath) return selectedFile.name;
                    if (/^(\/|[A-Za-z]:[\\/])/.test(normalizedFilePath)) return normalizedFilePath;
                    const base = '';
                    const rel = normalizedFilePath.replace(/^[\\/]+/, '');
                    return `${base}/${rel}`;
                })();
                res = await apiFetch('/execute/start', {
                    method: 'POST',
                    body: JSON.stringify({ 
                        filename: executeFilename,
                        content: editorContent,
                        ...executionContext,
                    })
                }, true);
            } else {
                // Remote or unsaved local code
                res = await apiFetch('/execute/run-tmp', {
                    method: 'POST',
                    body: JSON.stringify({
                        content: editorContent,
                        filename: activeTab === 'remote' ? selectedRemote?.name : 'unsaved_local.py',
                        ...executionContext,
                    })
                }, true);
            }

            if (!res.ok) {
                const errData = await res.json().catch(() => ({}));
                const errMsg = formatError(errData.detail || errData) || '启动执行失败';
                throw new Error(errMsg);
            }

            const data = await res.json();
            const job_id = data.job_id;
            setJobId(job_id);

            // Setup SSE for logs
            const es = new EventSource(buildStreamUrl(`/execute/logs/${job_id}`));
            runCancelRef.current = () => {
                es.close();
            };

            es.addEventListener('progress', (e: any) => {
                try {
                    const data = JSON.parse(e.data);
                    setProgress(data);
                } catch (err) { console.error('Parse progress error', err); }
            });

            es.addEventListener('report', (e: any) => {
                try {
                    const data = JSON.parse(e.data);
                    appendLogLine(`[REPORT] ${data.path || '报告已生成'}`);
                } catch (err) { console.error('Parse report error', err); }
            });
            eventSourceRef.current = es;

            es.onmessage = (event) => {
                const text = event.data;
                if (text === '[PROCESS_FINISHED]') {
                    syncExecuteResultSummary();
                    setIsRunning(false);
                    es.close();
                    return;
                }

                if (text.startsWith('[REPORT_LINK] ')) {
                    const path = text.replace('[REPORT_LINK] ', '').trim();
                    appendLogLine(`[REPORT_LINK] ${path}`);
                    return;
                }

                if (text.startsWith('[ERROR]')) {
                    const errorMsg = text.replace('[ERROR] ', '');
                    appendErrorLine(errorMsg);
                } else {
                    ingestExecuteResultLine(text);
                    appendLogLine(text);
                }
            };

            es.onerror = () => {
                appendErrorLine('执行日志流已中断，请检查后端执行器日志或网络连接');
                setIsRunning(false);
                es.close();
            };

        } catch (err: any) {
            console.error('Execution failed', err);
            setErrors([err.message || '执行请求失败']);
            setLogTab('error');
            setIsRunning(false);
        }
    };

    const handleStop = async () => {
        if (!jobId) return;
        try {
            await apiFetch(`/execute/stop/${jobId}`, { method: 'POST' });
            if (runCancelRef.current) {
                runCancelRef.current();
            }
            setIsRunning(false);
            if (eventSourceRef.current) eventSourceRef.current.close();
        } catch (err) {
            console.error('Stop failed', err);
        }
    };

    const handleEnterDirectory = async (path: string) => {
        if (activeTab !== 'local') return;
        setCurrentDir(path);
        setSelectedFile(null);
        setEditorContent('# 请选择一个文件开始编辑');
        await fetchLocalFileList();
    };

    const handleGoParent = async () => {
        if (activeTab !== 'local') return;
        if (!parentDir && parentDir !== '') return;
        setCurrentDir(parentDir || '');
        setSelectedFile(null);
        setEditorContent('# 请选择一个文件开始编辑');
        await fetchLocalFileList();
    };

    // Session State
    const [userId] = React.useState(() => {
        const storedUser = authService.getStoredUser() as any;
        const preferredUserId = String(
            storedUser?.user_id ||
            storedUser?.id ||
            storedUser?.username ||
            localStorage.getItem('ai_ide_user_id') ||
            ''
        ).trim();
        if (preferredUserId) {
            localStorage.setItem('ai_ide_user_id', preferredUserId);
        }
        return preferredUserId;
    });
    const [conversationId] = React.useState(() => 'conv_' + Date.now());

    const sendMessage = async (
        content: string,
        context?: { error_msg?: string; current_code?: string; selection?: string; file_path?: string }
    ) => {
        if (!content.trim() || isAITyping) return;
        if (!userId) {
            message.error('未获取到用户身份，请重新登录后重试');
            return;
        }

        const userMsg: Message = { id: Date.now().toString(), role: 'user', content };
        (setMessages as React.Dispatch<React.SetStateAction<Message[]>>)((curr) => [...curr, userMsg]);
        setChatInput('');
        setIsAITyping(true);

        const aiMsgId = (Date.now() + 1).toString();
        const initialAiMsg: Message = { id: aiMsgId, role: 'ai', content: '' };
        (setMessages as React.Dispatch<React.SetStateAction<Message[]>>)((curr) => [...curr, initialAiMsg]);
        setStreamingMessageId(aiMsgId);

        try {
            // Prepare history for backend
            const history = messages.slice(-10).map(m => ({
                role: m.role === 'ai' ? 'assistant' : 'user',
                content: m.content
            }));

            const res = await apiFetch('/ai/chat', {
                method: 'POST',
                body: JSON.stringify({
                    message: content,
                    user_id: userId,
                    conversation_id: conversationId,
                    history: history,
                    role: assistantRole,
                    extra_context: {
                        assistant_rules: getAssistantRules(currentMarket),
                        assistant_mode: 'strict',
                        market: currentMarket,
                        market_name: marketConfig.label,
                    },
                    ...(context || {})
                })
            }, true);

            if (!res.ok) {
                const text = await res.text();
                let detail = text;
                try {
                    const parsed = JSON.parse(text || '{}');
                    detail = parsed.detail || parsed.message || parsed.error || text;
                } catch {
                    // Keep raw text when response is not JSON.
                }
                throw new Error(detail || `LLM 请求失败 (HTTP ${res.status})`);
            }

            const reader = res.body?.getReader();
            if (!reader) throw new Error('No reader availabe');

            let accumulatedContent = '';
            const currentMode: 'text' | 'code' = 'text';
            const decoder = new TextDecoder();
            let buffer = '';
            let finished = false;
            const applyContent = (value: string) => {
                accumulatedContent = value;
                (setMessages as React.Dispatch<React.SetStateAction<Message[]>>)((curr) => curr.map((m) =>
                    m.id === aiMsgId ? { ...m, content: accumulatedContent } : m
                ));
            };
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                const chunk = decoder.decode(value, { stream: true });
                buffer += chunk;

                const lines = buffer.split(/\r?\n/);
                // 保留最后一个可能不完整的行
                buffer = lines.pop() || '';

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const data = line.slice(6);

                        if (data === '[DONE]') {
                            finished = true;
                            break;
                        }

                        try {
                            const parsed = JSON.parse(data);
                            if (parsed && typeof parsed.delta === 'string') {
                                accumulatedContent += parsed.delta;
                                applyContent(accumulatedContent);
                            }
                        } catch (e) {
                            // 忽略解析错误，或记录日志
                            console.warn('Failed to parse SSE JSON:', data);
                        }
                    }
                }
                if (finished) break;
            }
            applyContent(accumulatedContent);
            setStreamingMessageId(null);
            setIsAITyping(false);
        } catch (err) {
            console.error('Chat failed', err);
            const rawErr = err instanceof Error ? err.message : 'LLM 请求失败';
            const errMsg = normalizeChatError(rawErr);
            message.error(errMsg);
            (setMessages as React.Dispatch<React.SetStateAction<Message[]>>)((curr) => curr.map((m) =>
                m.id === aiMsgId ? { ...m, content: `❌ ${errMsg}` } : m
            ));
            setStreamingMessageId(null);
            setIsAITyping(false);
        }
    };

    const handleSendMessage = async () => {
        await sendMessage(chatInput);
    };

    const buildSelectionDraft = (selectedCode: string, filePath?: string) => {
        const parts = [
            '请基于以下选中代码进行分析或修改，先补充你的需求，再发送。',
            filePath ? `文件路径：${filePath}` : '',
            '选中代码：',
            '```python',
            selectedCode,
            '```',
            '',
            '补充需求：',
        ];
        return parts.filter((line) => line !== '').join('\n');
    };

    const handleSendSelectedCode = async () => {
        const editor = editorRef.current;
        if (!editor) return;

        const selection = editor.getSelection();
        if (!selection || selection.isEmpty()) {
            message.info('请先在编辑器中选择一段代码');
            return;
        }

        const model = editor.getModel();
        if (!model) return;

        const selectedCode = model.getValueInRange(selection).trim();
        if (!selectedCode) {
            message.info('选中内容为空，请重新选择代码');
            return;
        }

        const filePath = activeTab === 'local'
            ? selectedFile?.path
            : (selectedRemote?.name || selectedRemote?.id);
        const draft = buildSelectionDraft(selectedCode, filePath);
        setChatInput(draft);
        window.setTimeout(() => {
            chatTextareaRef.current?.focus();
            const pos = draft.length;
            try {
                chatTextareaRef.current?.setSelectionRange(pos, pos);
            } catch {
                // ignore
            }
        }, 0);
        message.success('已填入输入框，请补充需求后发送');
    };

    const handleApplyDiff = (search: string, replace: string) => {
        const editor = editorRef.current;
        if (!editor) return;

        const fullCode = editor.getValue();
        if (fullCode.includes(search)) {
            const newCode = fullCode.replace(search, replace);
            editor.setValue(newCode);
            message.success('已应用修改');
        } else {
            // 模糊匹配逻辑（可选，目前先提示未找到）
            message.error('无法自动应用：未能在当前代码中找到匹配的片段');
        }
    };

    const handleInsertCode = (code: string) => {
        const editor = editorRef.current;
        const monaco = monacoRef.current;
        if (!editor || !monaco) return;

        const selection = editor.getSelection();
        const position = editor.getPosition();
        const range = selection && !selection.isEmpty()
            ? selection
            : (position ? new monaco.Range(position.lineNumber, position.column, position.lineNumber, position.column) : null);
        if (!range) return;

        editor.executeEdits('ai-ide', [{ range, text: code, forceMoveMarkers: true }]);
        editor.focus();
    };

    const looksLikeCode = (text: string) => {
        const trimmed = text.trim();
        if (!trimmed) return false;
        const codeHints = ['def ', 'class ', 'import ', 'from ', 'if __name__', 'for ', 'while ', 'return '];
        return codeHints.some((hint) => trimmed.includes(hint));
    };

    const parseMarkedSegments = (content: string) => {
        const segments: Array<{ type: 'text' | 'code' | 'diff'; text: string; search?: string; replace?: string }> = [];
        let cursor = 0;

        while (cursor < content.length) {
            // Find next Diff start
            const diffRegex = /<<<<\s*SEARCH/g;
            diffRegex.lastIndex = cursor;
            const diffMatch = diffRegex.exec(content);

            // Find next Code Block start
            const codeRegex = /```(\w*)/g;
            codeRegex.lastIndex = cursor;
            const codeMatch = codeRegex.exec(content);

            // Determine which comes first
            const diffIdx = diffMatch ? diffMatch.index : Infinity;
            const codeIdx = codeMatch ? codeMatch.index : Infinity;

            if (diffIdx === Infinity && codeIdx === Infinity) {
                // No more blocks
                segments.push({ type: 'text', text: content.slice(cursor) });
                break;
            }

            if (diffIdx < codeIdx) {
                // --- Process Diff Block ---
                // Push text before
                if (diffIdx > cursor) {
                    segments.push({ type: 'text', text: content.slice(cursor, diffIdx) });
                }

                const startMatch = diffMatch!;
                const startIdx = diffIdx;
                const contentStart = startIdx + startMatch[0].length;

                // Find separator
                const sepRegex = /====/g;
                sepRegex.lastIndex = contentStart;
                const sepMatch = sepRegex.exec(content);

                if (!sepMatch) {
                    // Loading Search
                    segments.push({
                        type: 'diff',
                        text: 'LOADING_SEARCH',
                        search: content.slice(contentStart),
                        replace: '...'
                    });
                    break;
                }

                const sepIdx = sepMatch.index;
                const searchContent = content.slice(contentStart, sepIdx).trim();
                const contentMid = sepIdx + sepMatch[0].length;

                // Find End
                const endRegex = />>>>/g;
                endRegex.lastIndex = contentMid;
                const endMatch = endRegex.exec(content);

                if (!endMatch) {
                    // Loading Replace
                    segments.push({
                        type: 'diff',
                        text: 'LOADING_REPLACE',
                        search: searchContent,
                        replace: content.slice(contentMid)
                    });
                    break;
                }

                const endIdx = endMatch.index;
                const replaceContent = content.slice(contentMid, endIdx).trim();

                segments.push({
                    type: 'diff',
                    text: content.slice(startIdx, endIdx + endMatch[0].length),
                    search: searchContent,
                    replace: replaceContent
                });

                cursor = endIdx + endMatch[0].length;

            } else {
                // --- Process Code Block ---
                // Push text before
                if (codeIdx > cursor) {
                    segments.push({ type: 'text', text: content.slice(cursor, codeIdx) });
                }

                const startMatch = codeMatch!;
                const startIdx = codeIdx;
                const lang = startMatch[1] || 'text'; // Capture language
                const contentStart = startIdx + startMatch[0].length;

                // Find Code Block End
                const endRegex = /```/g;
                endRegex.lastIndex = contentStart;
                const endMatch = endRegex.exec(content);

                if (!endMatch) {
                    // Unclosed Code Block (Streaming)
                    // Treat everything until end as code
                    segments.push({
                        type: 'code',
                        text: content.slice(contentStart), // The raw code content
                        search: lang // Hack: store lang in search field or text field?
                        // Wait, renderMessageContent expects segments.type === 'code' to reuse existing logic?
                        // Existing logic: segments.type === 'code' is NOT handled in renderMessageContent switch explicitly?
                        // Let's check renderMessageContent.
                    });
                    // For now store lang in text for temporary storage, or use a specific structure.
                    // Actually best to render it immediately.
                    // Let's correct this.
                    // renderCodeBlock expects raw code.
                    // I need to reuse renderCodeBlock.

                    // But wait, renderMessageContent handles 'diff' and 'text'.
                    // It does NOT handle 'code' types explicitly in the top level loop if I recall correctly.
                    // Wait, `parseMarkedSegments` signature: Array<{ type: 'text' | 'code' | 'diff' ... }>
                    // But `renderMessageContent` implementation?

                    break;
                }

                const endIdx = endMatch.index;
                const codeContent = content.slice(contentStart, endIdx); // Raw code

                segments.push({
                    type: 'code',
                    text: codeContent,
                    search: lang // Store lang here
                });

                cursor = endIdx + endMatch[0].length;
            }
        }
        return segments;
    };

    const hasMarkedSegments = (content: string) =>
        /<<<<\s*SEARCH/.test(content) || /```/.test(content);

    const normalizeContent = (content: string, allowWrap: boolean) => {
        if (allowWrap && !content.includes('```') && looksLikeCode(content)) {
            return `\`\`\`python\n${content}\n\`\`\``;
        }
        return content;
    };

    const renderDiffBlock = (search: string, replace: string) => {
        return (
            <div className="bg-white border border-indigo-200 rounded-xl overflow-hidden my-2 shadow-sm max-w-full w-full min-w-0 grid grid-cols-[minmax(0,1fr)]">
                <div className="flex items-center justify-between px-3 py-2 text-[10px] bg-indigo-50 border-b border-indigo-100">
                    <span className="font-bold text-indigo-600 uppercase flex items-center gap-1">
                        <Code2 className="h-3 w-3" /> 修改建议
                    </span>
                    <button
                        onClick={() => handleApplyDiff(search, replace)}
                        className="px-3 py-1 bg-indigo-600 text-white rounded-full text-[10px] font-bold hover:bg-indigo-700 transition-all shadow-sm active:scale-95"
                    >
                        一键应用
                    </button>
                </div>
                <div className="p-0 text-[10px] grid grid-cols-[minmax(0,1fr)] divide-y divide-gray-100 min-w-0 w-full">
                    <div className="p-3 bg-red-50/30 overflow-hidden min-w-0 w-full">
                        <div className="text-[9px] text-red-400 font-bold mb-1">移除:</div>
                        <pre className="text-red-700 line-through opacity-70 overflow-x-auto custom-scrollbar pb-1 max-w-full w-full"><code>{search}</code></pre>
                    </div>
                    <div className="p-3 bg-green-50/30 overflow-hidden min-w-0 w-full">
                        <div className="text-[9px] text-green-500 font-bold mb-1">新增:</div>
                        <pre className="text-green-800 overflow-x-auto custom-scrollbar pb-1 max-w-full w-full"><code>{replace}</code></pre>
                    </div>
                </div>
            </div>
        );
    };

    const renderCodeBlock = (raw: string, lang = 'python') => {
        const code = raw.replace(/^\n+|\n+$/g, '');
        if (!code.trim()) return null;

        // 语法高亮
        let highlighted = code;
        try {
            const grammar = Prism.languages[lang] || Prism.languages.python;
            highlighted = Prism.highlight(code, grammar, lang);
        } catch (e) {
            console.warn('Prism highlight failed:', e);
        }

        return (
            <div className="bg-white border border-gray-200 rounded-lg overflow-hidden my-2 max-w-full min-w-0 grid grid-cols-[minmax(0,1fr)]">
                <div className="flex items-center justify-between px-3 py-2 text-[10px] bg-gray-50 border-b border-gray-200">
                    <span className="font-bold text-gray-500 uppercase">{lang}</span>
                    <div className="flex items-center gap-2">
                        <button
                            onClick={() => handleInsertCode(code)}
                            className="px-2 py-1 bg-blue-600 text-white rounded-md text-[10px] font-bold hover:bg-blue-700 transition-all"
                        >
                            插入编辑器
                        </button>
                        <button
                            onClick={() => handleCopyCode(code)}
                            className="px-2 py-1 bg-white border border-gray-200 text-gray-600 rounded-md text-[10px] font-bold hover:bg-gray-50 transition-all"
                        >
                            复制
                        </button>
                    </div>
                </div>

                <pre className="p-3 text-[12px] bg-white whitespace-pre overflow-x-auto custom-scrollbar !m-0 !bg-white">
                    <code
                        className={`language-${lang} !text-black !bg-white !shadow-none`}
                        dangerouslySetInnerHTML={{ __html: highlighted }}
                    />
                </pre>
            </div>
        );
    };

    const renderMessageContent = (msg: Message) => {
        // 如果包含结构化段落（#代码 或 SEARCH/REPLACE）
        if (hasMarkedSegments(msg.content)) {
            return (
                <div className="space-y-3 min-w-0">
                    {parseMarkedSegments(msg.content).map((seg, idx) => (
                        <div key={`${msg.id}-seg-${idx}`} className="min-w-0">
                            {seg.type === 'text' ? (
                                <div className="prose prose-sm max-w-none prose-p:my-1 prose-headings:my-1.5 prose-li:my-0.5 prose-code:before:content-none prose-code:after:content-none prose-pre:bg-transparent prose-pre:p-0 overflow-x-hidden break-words min-w-0">
                                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                        {seg.text}
                                    </ReactMarkdown>
                                </div>
                            ) : seg.type === 'diff' ? (
                                renderDiffBlock(seg.search!, seg.replace!)
                            ) : (
                                renderCodeBlock(seg.text, seg.search || 'python')
                            )}
                        </div>
                    ))}
                </div>
            );
        }

        // 默认 Markdown 渲染
        return (
            <div className="prose prose-sm max-w-none prose-p:my-1 prose-headings:my-1.5 prose-li:my-0.5 prose-code:before:content-none prose-code:after:content-none prose-pre:bg-transparent prose-pre:p-0 overflow-x-hidden break-words min-w-0">
                <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                        // 移除 ReactMarkdown 默认的 pre 包裹，避免 prose 样式（深色背景/浅色文字）干扰自定义代码块
                        pre: ({ children }) => <>{children}</>,
                        code(props: any) {
                            const { className, children, inline } = props;
                            const raw = String(children || '');
                            // 如果没有指定语言，默认为 text，renderCodeBlock 中会处理
                            const lang = className?.replace('language-', '') || 'text';

                            // 判断是否为行内代码：
                            // 1. 如果 props.inline 存在，直接使用
                            // 2. 否则，如果包含换行符，则视为代码块；否则视为行内
                            const isInline = inline !== undefined ? inline : !raw.includes('\n');

                            // 修复行内代码样式
                            if (isInline) {
                                return <code className="bg-gray-100 px-1.5 py-0.5 rounded text-[12px] font-mono break-words whitespace-pre-wrap text-gray-800">{raw}</code>;
                            }

                            // 渲染代码块 (如果是 text 类型且是大块代码，renderCodeBlock 会尝试高亮或显示为纯文本但保留格式)
                            // 为了体验，如果没有语言但被判定为 block，我们可以默认尝试 python 高亮，或者 bash
                            const finalLang = lang === 'text' ? 'python' : lang; // 智能默认 Python
                            return renderCodeBlock(raw, finalLang);
                        },
                        // Optimization for images to prevent overflow
                        img: ({ node: _node, ...props }) => <img {...props} className="max-w-full h-auto rounded-lg" />
                    }}
                >
                    {normalizeContent(msg.content, true)}
                </ReactMarkdown>
            </div>
        );
    };

    const handleCopyCode = async (code: string) => {
        try {
            await navigator.clipboard.writeText(code);
            message.success('代码已复制');
        } catch (err) {
            console.error('Failed to copy code', err);
            message.error('复制失败');
        }
    };

    const handleCopyLogs = async () => {
        const content = logTab === 'result' ? logs.join('\n') : errors.join('\n');
        if (!content.trim()) {
            message.info(logTab === 'result' ? '暂无可复制的运行日志' : '暂无可复制的错误日志');
            return;
        }
        try {
            await navigator.clipboard.writeText(content);
            message.success('日志已复制到剪贴板');
        } catch (err) {
            console.error('Failed to copy logs', err);
            message.error('日志复制失败');
        }
    };

    return (
        <div className="flex h-full bg-[#f8fafc] overflow-hidden p-6 gap-6">
            {createMode && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
                    <div className="w-[360px] bg-white rounded-2xl shadow-xl border border-gray-200 p-5">
                        <div className="text-sm font-bold text-gray-800 mb-3">
                            {createMode === 'folder' ? '新建文件夹' : '新建文件'}
                        </div>
                        <input
                            autoFocus
                            value={createName}
                            onChange={(e) => setCreateName(e.target.value)}
                            onKeyDown={(e) => {
                                if (e.key === 'Enter') handleConfirmCreate();
                                if (e.key === 'Escape') handleCancelCreate();
                            }}
                            placeholder={createMode === 'folder' ? '请输入文件夹名' : '请输入文件名（可省略 .py）'}
                            className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs focus:ring-2 focus:ring-blue-500/20"
                        />
                        <div className="mt-4 flex justify-end gap-2">
                            <button
                                onClick={handleCancelCreate}
                                className="px-3 py-1.5 text-xs rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50"
                            >
                                取消
                            </button>
                            <button
                                onClick={handleConfirmCreate}
                                className="px-3 py-1.5 text-xs rounded-lg bg-blue-600 text-white hover:bg-blue-700"
                            >
                                确认
                            </button>
                        </div>
                    </div>
                </div>
            )}
            {/* 1. File Management Panel */}
            <aside className="w-[280px] flex-shrink-0 bg-white border border-gray-200 flex flex-col rounded-[32px] shadow-sm transition-all duration-500 overflow-hidden">
                <div className="p-4 border-b border-gray-100">
                    <div className="flex bg-gray-100 p-1 rounded-lg mb-4">
                        <button
                            onClick={() => setActiveTab('local')}
                            className={clsx(
                                "flex-1 py-1.5 text-xs font-medium rounded-md transition-all",
                                activeTab === 'local' ? "bg-white text-blue-600 shadow-sm" : "text-gray-500 hover:text-gray-700"
                            )}
                        >
                            工作空间
                        </button>
                        <button
                            onClick={() => setActiveTab('remote')}
                            className={clsx(
                                "flex-1 py-1.5 text-xs font-medium rounded-md transition-all",
                                activeTab === 'remote' ? "bg-white text-blue-600 shadow-sm" : "text-gray-500 hover:text-gray-700"
                            )}
                        >
                            策略中心
                        </button>
                    </div>
                    <div className="relative mb-4">
                        <Search className="absolute left-3 top-2.5 h-3.5 w-3.5 text-gray-400" />
                        <input
                            type="text"
                            placeholder="搜索文件..."
                            value={searchQuery}
                            onChange={(e) => setSearchQuery(e.target.value)}
                            className="w-full bg-gray-50 border-none rounded-lg py-2 pl-9 pr-4 text-xs focus:ring-2 focus:ring-blue-500/10 transition-all"
                        />
                    </div>

                    {activeTab === 'local' && (
                        <>

                            <div className="flex items-center justify-between mb-3">
                                <div className="text-[10px] text-gray-500 truncate max-w-[160px]" title={currentDir || '/'}>
                                    当前: {currentDir || '/'}
                                </div>
                                <button
                                    onClick={handleGoParent}
                                    disabled={!parentDir && parentDir !== ''}
                                    className={clsx(
                                        "text-[10px] px-2 py-1 rounded transition-all",
                                        !parentDir && parentDir !== ''
                                            ? "bg-gray-100 text-gray-300 cursor-not-allowed"
                                            : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                                    )}
                                >
                                    返回上级
                                </button>
                            </div>
                        </>
                    )}

                    <div className="flex items-center justify-between">
                        <h2 className="text-[10px] font-bold text-gray-400 uppercase tracking-widest flex items-center gap-2">
                            <FolderTree className="w-3.5 h-3.5" />
                            策略库
                        </h2>
                        {activeTab === 'local' ? (
                            <div className="flex items-center gap-1">
                                <button
                                    onClick={handleCreateFile}
                                    className="p-1 hover:bg-gray-100 rounded transition-colors text-gray-500 hover:text-blue-600"
                                    title="新建文件"
                                >
                                    <FilePlus className="w-3.5 h-3.5" />
                                </button>
                                <button
                                    onClick={handleCreateFolder}
                                    className="p-1 hover:bg-gray-100 rounded transition-colors text-gray-500 hover:text-blue-600"
                                    title="新建文件夹"
                                >
                                    <FolderPlus className="w-3.5 h-3.5" />
                                </button>
                            </div>
                        ) : (
                            <button
                                onClick={fetchRemoteStrategies}
                                className="p-1 hover:bg-gray-100 rounded transition-colors text-gray-500 hover:text-blue-600"
                                title="刷新列表"
                            >
                                <RefreshCw className="w-3.5 h-3.5" />
                            </button>
                        )}
                    </div>
                </div>

                <div className="flex-1 overflow-y-auto p-4 space-y-2 custom-scrollbar">
                    {isLoadingFiles && activeTab === 'local' ? (
                        <div className="space-y-2">
                            {[1, 2, 3, 4, 5].map((i) => (
                                <div key={i} className="h-10 bg-gray-100 rounded-lg animate-pulse" />
                            ))}
                        </div>
                    ) : isLoadingRemote && activeTab === 'remote' ? (
                        <div className="space-y-2">
                            {[1, 2, 3, 4, 5].map((i) => (
                                <div key={i} className="h-10 bg-gray-100 rounded-lg animate-pulse" />
                            ))}
                        </div>
                    ) : activeTab === 'local' ? (
                        files.filter(f => f.name.toLowerCase().includes(searchQuery.toLowerCase())).map((file) => (
                            <div
                                key={file.id}
                                className="flex items-center justify-between group"
                            >
                                <div
                                    onClick={() => {
                                        if (file.is_dir || file.type === 'dir') {
                                            handleEnterDirectory(file.path);
                                        } else {
                                            setSelectedFile(file);
                                        }
                                    }}
                                    className={clsx(
                                        "flex items-center gap-3 p-2 rounded-xl cursor-pointer flex-1 transition-all",
                                        selectedFile?.id === file.id ? "bg-blue-50" : "hover:bg-blue-50/50"
                                    )}
                                >
                                    {file.is_dir || file.type === 'dir' ? (
                                        <FolderTree className={clsx(
                                            "h-4 w-4",
                                            selectedFile?.id === file.id ? "text-blue-600" : "text-blue-500/70"
                                        )} />
                                    ) : (
                                        <FileCode className={clsx(
                                            "h-4 w-4",
                                            selectedFile?.id === file.id ? "text-blue-600" : "text-blue-500/70"
                                        )} />
                                    )}
                                    <span className={clsx(
                                        "text-xs uppercase tracking-tight transition-colors",
                                        selectedFile?.id === file.id ? "text-blue-600 font-bold" : "text-gray-600 group-hover:text-blue-600"
                                    )}>
                                        {file.name}
                                    </span>
                                </div>
                                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-all">
                                    <button
                                        onClick={(e) => handleRenameItem(file.path, e)}
                                        className="p-1 hover:bg-blue-100 rounded text-gray-400 hover:text-blue-600 transition-all"
                                        title="重命名"
                                    >
                                        <Edit2 className="h-3 w-3" />
                                    </button>
                                    <button
                                        onClick={(e) => handleDeleteFile(file.path, e)}
                                        className="p-1 hover:bg-red-50 rounded text-gray-400 hover:text-red-500 transition-all"
                                        title="删除文件"
                                    >
                                        <Trash2 className="h-3.5 w-3.5" />
                                    </button>
                                </div>
                            </div>
                        ))
                    ) : (
                        remoteStrategies.filter(s => (s.name || '').toLowerCase().includes(searchQuery.toLowerCase())).map((item) => (
                            <div
                                key={item.id}
                                className="flex items-center justify-between group"
                            >
                                <div
                                    onClick={() => {
                                        setSelectedRemote(item);
                                        setSelectedFile(null);
                                    }}
                                    className={clsx(
                                        "flex items-center gap-3 p-2 rounded-xl cursor-pointer flex-1 transition-all",
                                        selectedRemote?.id === item.id ? "bg-blue-50" : "hover:bg-blue-50/50"
                                    )}
                                >
                                    <FileCode className={clsx(
                                        "h-4 w-4",
                                        selectedRemote?.id === item.id ? "text-blue-600" : "text-blue-500/70"
                                    )} />
                                    <div className="flex flex-col">
                                        <span className={clsx(
                                            "text-xs uppercase tracking-tight transition-colors",
                                            selectedRemote?.id === item.id ? "text-blue-600 font-bold" : "text-gray-600 group-hover:text-blue-600"
                                        )}>
                                            {item.name || item.id}
                                        </span>
                                        {item.updated_at && (
                                            <span className="text-[10px] text-gray-400">更新: {item.updated_at}</span>
                                        )}
                                    </div>
                                </div>
                            </div>
                        ))
                    )}
                </div>

                {/* Status Bar Left Part */}
                <div className="p-4 border-t border-gray-200 shrink-0">
                    <HelpCenterLink className="w-full" />
                </div>
            </aside>

            {/* 2. Main Editor & Log Area */}
            <main className="flex-1 flex flex-col min-w-0 gap-4">
                {/* Editor Toolbar - Double Row Structure */}
                <div className="flex-shrink-0 bg-white border border-gray-200 rounded-[24px] shadow-sm flex flex-col justify-between p-3 px-5 gap-2.5 relative z-10">
                    {/* Row 1: File & Environment Meta */}
                    <div className="flex items-center justify-between min-w-0 gap-3 border-b border-gray-100 pb-2">
                        <div className="flex items-center gap-2 min-w-0">
                            <div className="p-1 rounded-lg bg-blue-50 text-blue-600 shrink-0">
                                <FileCode className="h-3.5 w-3.5" />
                            </div>
                            <div className="flex items-center gap-2 min-w-0">
                                <span
                                    className="font-bold text-gray-800 text-xs truncate max-w-[260px]"
                                    title={activeTab === 'local' ? (selectedFile?.name || '未选择文件') : (selectedRemote?.name || '未选择策略')}
                                >
                                    {activeTab === 'local'
                                        ? (selectedFile?.name || '未选择文件')
                                        : (selectedRemote?.name || '未选择策略')}
                                </span>
                                <span className="shrink-0 px-2 py-0.5 rounded-full text-[10px] font-medium bg-gray-100 text-gray-500 border border-gray-200/60">
                                    {activeTab === 'local' ? '本地工作区' : '云端策略库'}
                                </span>
                                <span className="shrink-0 px-1.5 py-0.5 rounded text-[10px] font-mono text-emerald-600 bg-emerald-50 border border-emerald-100">
                                    Python
                                </span>
                                {defaultModelName && (
                                    <span
                                        className="shrink-0 px-1.5 py-0.5 rounded text-[10px] text-blue-600 bg-blue-50 border border-blue-100 truncate max-w-[180px]"
                                        title={`执行默认使用的模型：${defaultModelName}`}
                                    >
                                        执行模型: {defaultModelName}
                                    </span>
                                )}
                            </div>
                        </div>

                        <div className="flex items-center gap-2 shrink-0">
                            {isCheckingSyntax ? (
                                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-gray-50 text-gray-400 border border-gray-100 text-[10px] whitespace-nowrap">
                                    <RefreshCw className="h-2.5 w-2.5 animate-spin text-gray-400" />
                                    校验中
                                </span>
                            ) : syntaxError ? (
                                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-red-50 text-red-600 border border-red-100 text-[10px] whitespace-nowrap" title={syntaxError}>
                                    <AlertCircle className="h-2.5 w-2.5 text-red-500" />
                                    语法异常
                                </span>
                            ) : (
                                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-emerald-50 text-emerald-600 border border-emerald-100 text-[10px] whitespace-nowrap">
                                    <CheckCircle className="h-2.5 w-2.5 text-emerald-500" />
                                    语法正常
                                </span>
                            )}
                        </div>
                    </div>

                    {/* Row 2: Action Toolbar */}
                    <div className="flex items-center justify-between min-w-0 gap-3">
                        <div className="flex items-center gap-2">
                            <button
                                onClick={handleSendSelectedCode}
                                disabled={activeTab === 'local' ? !selectedFile : !selectedRemote}
                                className="flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-medium bg-gray-50 border border-gray-200 text-gray-700 rounded-full hover:bg-gray-100 hover:text-blue-600 transition-all hover:scale-[1.02] active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap shadow-2xs"
                                title="将编辑器中选中的代码注入右侧 AI 对话框"
                            >
                                <Code2 className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                                <span>填入选中代码</span>
                            </button>

                            {activeTab === 'local' && selectedFile && (
                                <button
                                    onClick={handleUploadToCloud}
                                    className="flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-medium bg-white border border-gray-200 text-gray-700 rounded-full hover:bg-gray-50 hover:text-blue-600 transition-all hover:scale-[1.02] active:scale-95 whitespace-nowrap shadow-2xs"
                                    title="保存到云端策略库"
                                >
                                    <CloudUpload className="h-3.5 w-3.5 text-indigo-500 shrink-0" />
                                    <span>发布云端</span>
                                </button>
                            )}
                        </div>

                        <div className="flex items-center gap-2 shrink-0">
                            {/* Run / Stop Button */}
                            {isRunning ? (
                                <button
                                    onClick={handleStop}
                                    className="flex items-center gap-1.5 px-4 py-1.5 text-xs font-medium rounded-full transition-all hover:scale-[1.02] active:scale-95 whitespace-nowrap border shadow-2xs bg-red-600 text-white border-red-600 hover:bg-red-700"
                                    title="停止当前执行任务"
                                >
                                    <Square className="h-3.5 w-3.5 shrink-0" />
                                    <span>停止</span>
                                </button>
                            ) : (
                                <button
                                    onClick={handleRun}
                                    disabled={syntaxError !== null || (activeTab === 'local' && !selectedFile)}
                                    className={clsx(
                                        "flex items-center gap-1.5 px-4 py-1.5 text-xs font-medium rounded-full transition-all hover:scale-[1.02] active:scale-95 whitespace-nowrap border shadow-2xs",
                                        syntaxError !== null || (activeTab === 'local' && !selectedFile)
                                            ? "bg-gray-100 text-gray-400 border-gray-200 cursor-not-allowed opacity-60"
                                            : "bg-emerald-600 text-white border-emerald-600 hover:bg-emerald-700"
                                    )}
                                    title="运行当前代码 (F5)"
                                >
                                    <Play className="h-3.5 w-3.5 shrink-0" />
                                    <span>运行</span>
                                </button>
                            )}
                            {/* Save Button */}
                            <button
                                onClick={handleSave}
                                disabled={isSaving || (activeTab === 'local' && !selectedFile)}
                                className={clsx(
                                    "flex items-center gap-1.5 px-4 py-1.5 text-xs font-medium rounded-full transition-all hover:scale-[1.02] active:scale-95 whitespace-nowrap border shadow-2xs",
                                    isSaving || (activeTab === 'local' && !selectedFile)
                                        ? "bg-gray-100 text-gray-400 border-gray-200 cursor-not-allowed opacity-60"
                                        : "bg-white border-gray-200 text-gray-700 hover:bg-gray-50 hover:border-gray-300"
                                )}
                                title="保存代码 (Ctrl+S / Cmd+S)"
                            >
                                {isSaving ? <CheckCircle className="h-3.5 w-3.5 text-green-500 shrink-0" /> : <Save className="h-3.5 w-3.5 text-gray-500 shrink-0" />}
                                <span>{isSaving ? '已保存' : '保存'}</span>
                            </button>
                        </div>
                    </div>
                </div>

                {/* Monaco Editor Container */}
                <div className="flex-1 bg-white border border-gray-200 rounded-[32px] shadow-sm overflow-hidden relative">
                    <Editor
                        height="100%"
                        defaultLanguage="python"
                        theme={editorTheme}
                        value={editorContent}
                        onChange={(value) => setEditorContent(value || '')}
                        onMount={(editor, monaco) => {
                            editorRef.current = editor;
                            monacoRef.current = monaco;
                        }}
                        options={{
                            minimap: { enabled: true },
                            fontSize: 13,
                            scrollBeyondLastLine: false,
                            automaticLayout: true,
                            padding: { top: 24, bottom: 16 },
                            readOnly: activeTab === 'local' ? !selectedFile : !selectedRemote
                        }}
                    />
                </div>

                {/* 3. Log Output Area */}
                <div className="h-[330px] flex-shrink-0 bg-white border border-gray-200 rounded-[32px] shadow-sm flex flex-col overflow-hidden relative">
                    {/* Progress Bar */}
                    {isRunning && progress && progress.percent >= 0 && (
                        <div className="absolute top-0 left-0 right-0 h-1 bg-gray-100 z-10">
                            <div
                                className="h-full bg-blue-600 transition-all duration-300 ease-out"
                                style={{ width: `${progress.percent}%` }}
                            />
                        </div>
                    )}
                    <div className="flex items-center justify-between px-4 h-10 border-b border-gray-100 bg-gray-50/50">
                        <div className="flex gap-4 items-center">
                            <button
                                onClick={() => setLogTab('result')}
                                className={clsx(
                                    "text-xs font-medium border-b-2 transition-all h-10 flex items-center gap-1.5",
                                    logTab === 'result' ? "border-blue-600 text-blue-600 font-semibold" : "border-transparent text-gray-500 hover:text-gray-700"
                                )}
                            >
                                运行结果
                                {logs.length > 0 && <span className="w-4 h-4 rounded-full bg-blue-100 text-[10px] flex items-center justify-center font-bold">{logs.length}</span>}
                            </button>
                            {isRunning && progress && (
                                <span className="inline-flex items-center h-10 text-[10px] text-blue-600 font-mono animate-pulse leading-none self-center">
                                    {progress.message}
                                </span>
                            )}
                            <button
                                onClick={() => setLogTab('error')}
                                className={clsx(
                                    "text-xs font-medium border-b-2 transition-all h-10 flex items-center gap-1.5",
                                    logTab === 'error' ? "border-red-500 text-red-500" : "border-transparent text-gray-500 hover:text-gray-700"
                                )}
                            >
                                错误信息
                                {errors.length > 0 && <span className="w-4 h-4 rounded-full bg-red-100 text-[10px] flex items-center justify-center font-bold">{errors.length}</span>}
                            </button>
                            {errors.length > 0 && (
                                <button
                                    onClick={() => {
                                        if (!isRunning) setIsAITyping(false); // Make sure we can send
                                        sendMessage('我遇到了这些错误，请帮我修复代码。', {
                                            error_msg: errors.join('\n'),
                                            current_code: editorContent
                                        });
                                    }}
                                    className="px-2 py-0.5 text-xs bg-purple-100 text-purple-700 rounded-md hover:bg-purple-200 flex items-center gap-1 transition-colors"
                                >
                                    <Bot className="w-3 h-3" />
                                    AI Fix
                                </button>
                            )}
                        </div>
                        <div className="flex gap-2">
                            <button
                                onClick={handleStop}
                                disabled={!isRunning}
                                className={clsx(
                                    "p-1 rounded transition-all hover:scale-110",
                                    isRunning ? "text-red-500 hover:bg-red-50" : "text-gray-300 cursor-not-allowed"
                                )}
                                title="停止执行"
                            >
                                <Square className="h-3.5 w-3.5 fill-current" />
                            </button>
                            <button
                                onClick={() => { setLogs([]); setErrors([]); setProgress(null); setFinalResultSummary(null); resetExecuteResultSummary(); }}
                                className="p-1 hover:bg-gray-100 rounded text-gray-400 transition-all hover:scale-110"
                                title="清除日志"
                            >
                                <Trash2 className="h-3.5 w-3.5" />
                            </button>
                            <button
                                onClick={handleCopyLogs}
                                className="p-1 hover:bg-gray-100 rounded text-gray-400 transition-all hover:scale-110"
                                title="复制当前日志"
                            >
                                <Copy className="h-3.5 w-3.5" />
                            </button>
                        </div>
                    </div>
                    <div className="flex-1 p-4 font-mono text-xs overflow-auto text-gray-600 custom-scrollbar bg-gray-50/30">
                        {logTab === 'result' ? (
                            <div className="space-y-1">
                                {renderResultSummaryCard()}
                                {logs.length === 0 && !isRunning && <p className="text-gray-400 italic">暂无运行数据</p>}
                                {logs.map((log, i) => (
                                    <div key={i} className="flex gap-3 px-2 py-0.5 hover:bg-gray-100/50 rounded transition-colors group">
                                        <span className="text-gray-300 text-[10px] w-6 text-right font-mono mt-0.5 select-none">{i + 1}</span>
                                        <span className="text-[11px] text-gray-600 font-mono whitespace-pre-wrap flex-1 leading-relaxed">{log}</span>
                                    </div>
                                ))}
                                {isRunning && (
                                    <div className="flex items-center gap-2 p-2 text-blue-600 text-[11px] font-bold animate-pulse">
                                        <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                                        <span>正在运行中...</span>
                                    </div>
                                )}
                                <div ref={logEndRef} />
                            </div>
                        ) : (
                            <div className="space-y-1 text-red-500">
                                {errors.length === 0 && <p className="text-gray-400 italic">暂无错误记录</p>}
                                {errors.map((error, i) => (
                                    <div key={i} className="flex gap-2 p-2 bg-red-50 rounded-lg">
                                        <AlertCircle className="h-3.5 w-3.5 flex-shrink-0 mt-0.5" />
                                        <p className="whitespace-pre-wrap">{error}</p>
                                    </div>
                                ))}
                                <div ref={logEndRef} />
                            </div>
                        )}
                    </div>
                </div>

            </main>

            {/* 4. LLM Chat Panel */}
            <aside className="w-[440px] flex-shrink-0 bg-white border border-gray-200 flex flex-col rounded-[32px] shadow-sm transition-all duration-500 overflow-hidden">
                <div className="p-4 border-b border-gray-100 flex items-center justify-between bg-gray-50/50">
                    <div className="flex flex-col gap-1">
                        <div className="flex items-center gap-2 text-sm font-bold text-gray-800 tracking-tight">
                            <Bot className="h-4 w-4 text-blue-600" />
                            AI 智能助手
                        </div>
                        <div className="flex gap-1.5 text-[10px]">
                            <button
                                onClick={() => setAssistantRole('quant_analyst')}
                                className={clsx(
                                    "px-2 py-0.5 rounded-full border transition-all flex items-center gap-1",
                                    assistantRole === 'quant_analyst'
                                        ? "bg-blue-50 text-blue-700 border-blue-200 font-bold"
                                        : "bg-gray-50 text-gray-400 border-gray-100 hover:text-gray-600"
                                )}
                                title="量化策略设计、因子推荐、回测分析"
                            >
                                <Sparkles size={10} />量化分析师
                            </button>
                            <button
                                onClick={() => setAssistantRole('bug_fixer')}
                                className={clsx(
                                    "px-2 py-0.5 rounded-full border transition-all flex items-center gap-1",
                                    assistantRole === 'bug_fixer'
                                        ? "bg-amber-50 text-amber-700 border-amber-200 font-bold"
                                        : "bg-gray-50 text-gray-400 border-gray-100 hover:text-gray-600"
                                )}
                                title="错误排查、堆栈分析、根因定位"
                            >
                                <Bug size={10} />Bug修复
                            </button>
                            <button
                                onClick={() => setAssistantRole('code_reviewer')}
                                className={clsx(
                                    "px-2 py-0.5 rounded-full border transition-all flex items-center gap-1",
                                    assistantRole === 'code_reviewer'
                                        ? "bg-violet-50 text-violet-700 border-violet-200 font-bold"
                                        : "bg-gray-50 text-gray-400 border-gray-100 hover:text-gray-600"
                                )}
                                title="代码审查、性能优化、安全检查"
                            >
                                <SearchIcon size={10} />代码审查
                            </button>
                        </div>
                    </div>
                    <button
                        onClick={() => setMessages([{ id: '1', role: 'ai', content: '对话已重置。' }])}
                        className="p-1.5 hover:bg-gray-200 rounded-lg text-gray-400 transition-all hover:rotate-90"
                    >
                        <RefreshCw className="h-4 w-4" />
                    </button>
                </div>

                <div className="flex-1 overflow-y-auto overflow-x-hidden p-4 space-y-4 custom-scrollbar">
                    {messages.map((msg) => (
                        <div key={msg.id} className={clsx(
                            "flex flex-col max-w-[90%] gap-2 animate-in fade-in slide-in-from-bottom-2",
                            msg.role === 'user' ? "ml-auto items-end" : "items-start"
                        )}>
                            <div className={clsx(
                                "p-3 rounded-2xl text-xs leading-relaxed",
                                msg.role === 'user'
                                    ? "bg-blue-600 text-white rounded-tr-none shadow-sm"
                                    : "bg-gray-100 text-gray-800 rounded-tl-none border border-gray-200/50"
                            )}>
                                {renderMessageContent(msg)}
                            </div>
                            {isAITyping && msg.id === messages[messages.length - 1].id && (
                                <span className="inline-block w-1.5 h-3.5 bg-blue-400 animate-pulse ml-1 align-middle" />
                            )}
                        </div>
                    ))}
                </div>

                <div className="p-4 border-t border-gray-100 bg-gray-50/50">
                    <div className="relative">
                        <textarea
                            ref={chatTextareaRef}
                            value={chatInput}
                            onChange={(e) => setChatInput(e.target.value)}
                            onKeyDown={(e) => {
                                if (e.key === 'Enter' && !e.shiftKey) {
                                    e.preventDefault();
                                    handleSendMessage();
                                }
                            }}
                            placeholder="补充需求、问题或修改说明...(Enter 发送)"
                            className="w-full bg-white border border-gray-200 rounded-xl p-3 pr-20 text-xs focus:ring-4 focus:ring-blue-500/10 resize-none min-h-[110px] transition-all"
                        />
                        <button
                            onClick={handleSendMessage}
                            disabled={!chatInput.trim() || isAITyping}
                            className="absolute right-2 bottom-2 px-4 py-2 bg-blue-600 text-white text-[10px] font-bold rounded-full hover:bg-blue-700 disabled:opacity-50 transition-all shadow-md active:scale-95"
                        >
                            {isAITyping ? '思考中...' : '发送'}
                        </button>
                    </div>

                    {/* Status Bar Right Part */}
                    <div className="mt-4 text-[10px] text-gray-500 font-medium px-1 flex flex-col items-end gap-1">
                        <div className="flex items-center gap-3 justify-end w-full">
                            <span className="bg-gray-100 px-1.5 py-0.5 rounded text-gray-400">UTF-8</span>
                            {isCheckingSyntax ? (
                                <div className="flex items-center gap-1 text-gray-400">
                                    <RefreshCw className="h-2.5 w-2.5 animate-spin" />
                                    检查中...
                                </div>
                            ) : syntaxError ? (
                                <div className="flex items-center gap-1 text-red-500 font-bold" title={syntaxError}>
                                    <AlertCircle className="h-2.5 w-2.5" />
                                    {syntaxError}
                                </div>
                            ) : (
                                <div className="flex items-center gap-1 text-green-600">
                                    <CheckCircle className="h-2.5 w-2.5" />
                                    语法检查通过
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            </aside>
        </div>
    );
};

export default AIIDEPage;
