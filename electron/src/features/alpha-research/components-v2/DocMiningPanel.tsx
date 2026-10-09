/**
 * 文档挖掘面板（T-FM-11）：上传 → 解析 → 整理 → 确认挖掘 四步 Stepper。
 *
 * 三条纪律：
 * 1. **人工确认是默认**：整理完的草稿落在可编辑 direction 里，用户看过才提交；
 *    「整理后直通」开关默认关（打开 = 整理完立即开始挖掘，跳过确认步）。
 * 2. **状态只信后端**：解析进度来自 3s 轮询 GET /docs/{id}，不做本地乐观推断；
 *    轮询失败照常重试并明示，解析失败带后端 error 原文。
 * 3. **解析通道明示**：上传区固定披露当前生效通道——云端（数据出网，
 *    mineru.net）或本地 / 局域网（数据不出网）；通道在「解析设置」内按
 *    账号配置，披露不藏在折叠里。
 *
 * 开始挖掘走 TaskContext.startMining（携带 docId 记录血统），
 * 提交后由 AppRoot 的 miningStartSeq 效应自动进入演化台（既有机制）。
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  UploadCloud, FileText, Loader2, CheckCircle2, AlertCircle,
  RotateCcw, Wand2, Play, CloudUpload, ArrowUp, ArrowDown, X,
  Server, Settings,
} from 'lucide-react';
import type { DocMiningResume, TaskConfig } from '../types-v2';
import type { DocQuotaStatus, DocRow, OrganizeResult } from '../services-v2/docMiningApi';
import {
  DOC_MAX_DIRECTION_CHARS,
  DOC_MAX_FILES,
  DOC_MAX_TOTAL_UPLOAD_BYTES,
  DOC_MAX_UPLOAD_BYTES,
  DOC_STATUS_LABELS,
  DOC_UPLOAD_ACCEPT,
  ORGANIZE_KIND_LABELS,
  extractDetail,
  getDoc,
  getDocFileText,
  getDocQuota,
  organizeDoc,
  uploadDocs,
} from '../services-v2/docMiningApi';
import { MineruSettingsSection } from './MineruSettingsSection';

type Step = 'upload' | 'parse' | 'organize' | 'confirm';

const STEP_ORDER: Array<{ key: Step; label: string }> = [
  { key: 'upload', label: '上传文档' },
  { key: 'parse', label: '解析' },
  { key: 'organize', label: '整理' },
  { key: 'confirm', label: '确认挖掘' },
];

/** 解析轮询间隔：MinerU 通常 10~60s 出结果，3s 足够灵敏且不打爆后端。 */
export const DOC_POLL_INTERVAL_MS = 3000;

const KIND_HINTS: Record<string, string> = {
  free: '提炼摘要、可挖掘假设与数据要求，适合研报/资讯/书籍',
  paper: '按「方法—因子—复现要点—数据需求」拆解，适合论文复现',
};

/** 文件大小展示（列表行用，只求可读不精确到字节）。 */
function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)}KB`;
  return `${bytes}B`;
}

export interface DocMiningPanelProps {
  /** 开始挖掘（TaskContext.startMining）；docId 一并下发记录文档血统 */
  onStartMining: (config: TaskConfig) => void;
  /** 任务提交中/运行中：锁提交 */
  isRunning: boolean;
  /** 「文档解析 → 继续挖掘」带回的文档（key 为代次：连点两次也要重新应用） */
  resume?: DocMiningResume | null;
}

export const DocMiningPanel: React.FC<DocMiningPanelProps> = ({
  onStartMining,
  isRunning,
  resume,
}) => {
  const [step, setStep] = useState<Step>('upload');
  const [doc, setDoc] = useState<DocRow | null>(null);
  const [quota, setQuota] = useState<DocQuotaStatus | null>(null);
  const [uploadPct, setUploadPct] = useState<number | null>(null);
  const [rawText, setRawText] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);
  const [kind, setKind] = useState('free');
  const [extra, setExtra] = useState('');
  const [organize, setOrganize] = useState<OrganizeResult | null>(null);
  const [directionDraft, setDirectionDraft] = useState('');
  const [autoDirect, setAutoDirect] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  /** 待上传清单（多文件先攒后传：顺序=合并顺序，可调可删，配额只烧一次） */
  const [stagedFiles, setStagedFiles] = useState<File[]>([]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  /** 解析设置折叠区（通道未配置时自动展开一次，之后由用户控制） */
  const [settingsOpen, setSettingsOpen] = useState(false);
  const autoOpenedSettingsRef = useRef(false);

  const refreshQuota = useCallback(async () => {
    try {
      setQuota(await getDocQuota());
    } catch {
      // 余量是辅助信息：拿不到就显「未知」，不拦主流程（上传时后端会真闸）
      setQuota(null);
    }
  }, []);

  useEffect(() => {
    void refreshQuota();
  }, [refreshQuota]);

  // 通道未配置：自动展开设置区一次——只自动一次，用户手动收起后不再抢焦点
  useEffect(() => {
    if (quota && !quota.token_configured && !autoOpenedSettingsRef.current) {
      autoOpenedSettingsRef.current = true;
      setSettingsOpen(true);
    }
  }, [quota]);

  /** 解析完成 → 进入整理步：拉原文对照（增强，失败不拦）；已整理过的预填草稿。 */
  const enterOrganize = useCallback(async (fresh: DocRow) => {
    setDoc(fresh);
    setStep('organize');
    if (fresh.organized_text) {
      setDirectionDraft(fresh.organized_text);
      if (fresh.organize_kind) setKind(fresh.organize_kind);
    }
    try {
      setRawText(await getDocFileText(fresh.doc_id));
    } catch {
      setRawText(null);
    }
  }, []);

  // ── 解析轮询（step === 'parse' 期间）────────────────────────────
  const docId = doc?.doc_id;
  useEffect(() => {
    if (step !== 'parse' || !docId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    const tick = async () => {
      try {
        const fresh = await getDoc(docId);
        if (cancelled) return;
        if (fresh.status === 'parsed' || fresh.status === 'organized') {
          if (timer) clearInterval(timer);
          await enterOrganize(fresh);
        } else if (fresh.status === 'parse_failed' || fresh.status === 'expired') {
          if (timer) clearInterval(timer);
          setDoc(fresh);
          setError(fresh.error || '文档解析失败，请重新上传');
        } else {
          setDoc(fresh);
          setNotice(null);
        }
      } catch (e: unknown) {
        if (cancelled) return;
        // 瞬态失败照常重试：把原因明示出来，不做静默重试
        setNotice(`状态查询失败（自动重试中）：${extractDetail(e)}`);
      }
    };
    timer = setInterval(() => void tick(), DOC_POLL_INTERVAL_MS);
    void tick(); // 进入解析步立刻打一拍，不让用户空等一个间隔
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [step, docId, enterOrganize]);

  // ── 「继续挖掘」带回：按 key 代次应用 ───────────────────────────
  const resumeKey = resume?.key;
  useEffect(() => {
    if (!resume) return;
    let cancelled = false;
    (async () => {
      setError(null);
      setNotice(null);
      setOrganize(null);
      try {
        const fresh = await getDoc(resume.docId);
        if (cancelled) return;
        if (fresh.status === 'parsed' || fresh.status === 'organized') {
          await enterOrganize(fresh);
        } else {
          setDoc(fresh);
          setStep('parse');
          if (fresh.status === 'parse_failed' || fresh.status === 'expired') {
            setError(fresh.error || '文档解析失败，请重新上传');
          }
        }
        void refreshQuota();
      } catch (e: unknown) {
        if (!cancelled) setError(extractDetail(e));
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resumeKey]);

  const reset = useCallback(() => {
    setStep('upload');
    setDoc(null);
    setUploadPct(null);
    setRawText(null);
    setShowRaw(false);
    setOrganize(null);
    setDirectionDraft('');
    setError(null);
    setNotice(null);
    setStagedFiles([]);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }, []);

  /** 选择文件 → 进待上传清单：前端先闸（白名单/单件/合计/件数），省一次白传。 */
  const stageFiles = useCallback(
    (picked: File[]) => {
      if (!picked.length) return;
      setError(null);
      setNotice(null);
      const next = [...stagedFiles, ...picked];
      if (next.length > DOC_MAX_FILES) {
        setError(`单次最多上传 ${DOC_MAX_FILES} 个文件（已选 ${next.length} 个）`);
        return;
      }
      for (const f of picked) {
        const ext = f.name.includes('.')
          ? `.${f.name.split('.').pop()!.toLowerCase()}`
          : '';
        if (!DOC_UPLOAD_ACCEPT.split(',').includes(ext)) {
          setError(
            `「${f.name}」不支持的文件类型 ${ext || '（无后缀）'}：仅支持 PDF / Word / PPT / 图片`,
          );
          return;
        }
        if (f.size > DOC_MAX_UPLOAD_BYTES) {
          setError(
            `「${f.name}」文件超过 ${Math.round(DOC_MAX_UPLOAD_BYTES / 1024 / 1024)}MB 上限，请拆分后上传`,
          );
          return;
        }
      }
      const total = next.reduce((sum, f) => sum + f.size, 0);
      if (total > DOC_MAX_TOTAL_UPLOAD_BYTES) {
        setError(
          `文件合计超过 ${Math.round(DOC_MAX_TOTAL_UPLOAD_BYTES / 1024 / 1024)}MB 上限：请减少文件或分两次上传`,
        );
        return;
      }
      setStagedFiles(next);
    },
    [stagedFiles],
  );

  /** 调序（合并顺序=清单顺序；diff 越界为 no-op）。 */
  const moveStaged = useCallback((index: number, delta: number) => {
    setStagedFiles((prev) => {
      const target = index + delta;
      if (target < 0 || target >= prev.length) return prev;
      const next = [...prev];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  }, []);

  const removeStaged = useCallback((index: number) => {
    setStagedFiles((prev) => prev.filter((_, i) => i !== index));
  }, []);

  /** 提交清单：一次 multipart 多文件（数组顺序=合并顺序），进度按整批算。 */
  const handleUpload = useCallback(async () => {
    if (!stagedFiles.length || busy) return;
    setError(null);
    setNotice(null);
    setOrganize(null);
    setRawText(null);
    setDirectionDraft('');
    setBusy(true);
    setUploadPct(0);
    try {
      const { doc: fresh, reused } = await uploadDocs(stagedFiles, (pct) =>
        setUploadPct(pct),
      );
      setStagedFiles([]);
      setDoc(fresh);
      if (reused) setNotice('该文件此前已解析过，直接复用已有的解析结果。');
      if (fresh.status === 'parsed' || fresh.status === 'organized') {
        await enterOrganize(fresh);
      } else {
        setStep('parse');
      }
      void refreshQuota();
    } catch (e: unknown) {
      // 失败保留清单：清掉的那件是「哪一件坏了」无从知道，整批原样重试
      setError(extractDetail(e));
    } finally {
      setBusy(false);
      setUploadPct(null);
    }
  }, [stagedFiles, busy, enterOrganize, refreshQuota]);

  const startWith = useCallback(
    (draft: string) => {
      if (!doc) return;
      const text = draft.trim();
      if (!text) {
        setError('方向草稿为空：请先整理文档或手动填写方向');
        return;
      }
      if (text.length > DOC_MAX_DIRECTION_CHARS) {
        setError(
          `方向过长（${text.length} 字，上限 ${DOC_MAX_DIRECTION_CHARS} 字），请精简后重试`,
        );
        return;
      }
      setError(null);
      onStartMining({ userInput: text, docId: doc.doc_id });
    },
    [doc, onStartMining],
  );

  const handleOrganize = useCallback(async () => {
    if (!doc) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await organizeDoc(doc.doc_id, {
        kind,
        extra: extra.trim() || undefined,
      });
      setOrganize(result);
      setDoc(result.doc);
      setDirectionDraft(result.markdown);
      if (result.truncated) {
        setNotice('文档超长：已按块采样整理（长文只取代表块），草稿可能不含全文细节。');
      }
      if (autoDirect) {
        startWith(result.markdown);
      } else {
        setStep('confirm');
      }
    } catch (e: unknown) {
      setError(extractDetail(e));
    } finally {
      setBusy(false);
    }
  }, [doc, kind, extra, autoDirect, startWith]);

  const stepIndex = STEP_ORDER.findIndex((s) => s.key === step);
  const overLimit = directionDraft.trim().length > DOC_MAX_DIRECTION_CHARS;

  return (
    <div
      id="doc-mining-panel"
      className="w-full max-w-4xl mx-auto flex flex-col gap-3 select-none"
    >
      {/* ============ Stepper 头 ============ */}
      <ol className="flex items-center justify-center gap-1.5 m-0 p-0 list-none">
        {STEP_ORDER.map((s, i) => {
          const state = i < stepIndex ? 'done' : i === stepIndex ? 'current' : 'todo';
          return (
            <React.Fragment key={s.key}>
              {i > 0 && (
                <span
                  className={`h-px w-6 sm:w-10 ${i <= stepIndex ? 'bg-blue-300' : 'bg-slate-200'}`}
                />
              )}
              <li className="flex items-center gap-1.5">
                <span
                  className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-black ${
                    state === 'done'
                      ? 'bg-blue-500 text-white'
                      : state === 'current'
                        ? 'bg-blue-100 text-blue-700 ring-2 ring-blue-200'
                        : 'bg-slate-100 text-slate-400'
                  }`}
                >
                  {state === 'done' ? <CheckCircle2 className="h-3.5 w-3.5" /> : i + 1}
                </span>
                <span
                  className={`text-[11px] font-bold ${
                    state === 'current' ? 'text-blue-700' : state === 'done' ? 'text-slate-600' : 'text-slate-400'
                  }`}
                >
                  {s.label}
                </span>
              </li>
            </React.Fragment>
          );
        })}
      </ol>

      {/* ============ 横幅 ============ */}
      {error && (
        <div className="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50/80 px-4 py-2.5 text-xs font-bold text-rose-600">
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span className="flex-1 min-w-0 whitespace-pre-wrap break-all">{error}</span>
          {(step === 'parse') && (
            <button
              type="button"
              onClick={reset}
              className="shrink-0 rounded-full border border-rose-200 bg-white px-3 py-1 text-[11px] font-bold text-rose-600 hover:bg-rose-50 cursor-pointer"
            >
              重新上传
            </button>
          )}
        </div>
      )}
      {notice && (
        <div className="rounded-xl border border-amber-200 bg-amber-50/80 px-4 py-2 text-[11px] font-bold text-amber-700">
          {notice}
        </div>
      )}

      <div className="rounded-2xl border border-white/90 bg-white/85 backdrop-blur-xl shadow-xs p-4 sm:p-5">
        {/* ============ Step 1 上传 ============ */}
        {step === 'upload' && (
          <div className="flex flex-col gap-3">
            <input
              ref={fileInputRef}
              id="doc-mining-file"
              type="file"
              accept={DOC_UPLOAD_ACCEPT}
              multiple
              className="hidden"
              onChange={(e) => {
                const picked = Array.from(e.target.files ?? []);
                stageFiles(picked);
                // 清 value：移除后重选同一文件也要能触发 change
                e.target.value = '';
              }}
            />
            <label
              htmlFor="doc-mining-file"
              className={`flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors ${
                busy
                  ? 'border-blue-200 bg-blue-50/40 cursor-wait'
                  : 'border-slate-200 bg-slate-50/60 hover:border-blue-300 hover:bg-blue-50/40 cursor-pointer'
              }`}
            >
              {busy ? (
                <>
                  <Loader2 className="h-7 w-7 text-blue-500 animate-spin" />
                  <span className="text-xs font-bold text-blue-700">
                    上传中 {uploadPct ?? 0}%
                  </span>
                  <span className="w-56 h-1.5 rounded-full bg-blue-100 overflow-hidden">
                    <span
                      className="block h-full rounded-full bg-gradient-to-r from-blue-500 to-indigo-500 transition-all duration-300"
                      style={{ width: `${uploadPct ?? 0}%` }}
                    />
                  </span>
                </>
              ) : (
                <>
                  <UploadCloud className="h-7 w-7 text-slate-400" />
                  <span className="text-sm font-black text-slate-700">
                    点击选择文件，或拖入此区域
                  </span>
                  <span className="text-[11px] text-slate-500">
                    支持 PDF / Word（doc、docx）/ PPT（ppt、pptx）/ 图片（png、jpg），单文件 ≤ 200MB / 200 页；
                    可多选一次上传（正文+附录、多图自动合并解析，合计 ≤ 200MB）
                  </span>
                </>
              )}
            </label>

            {/* 待上传清单：合并顺序=列表顺序（正文在前、附录/附图在后） */}
            {stagedFiles.length > 0 && !busy && (
              <div className="flex flex-col gap-2 rounded-xl border border-blue-100 bg-blue-50/40 px-3 py-2.5">
                <span className="text-[11px] font-black text-slate-600">
                  待上传 {stagedFiles.length} 个文件 · 顺序即合并顺序（正文在前，附录 / 附图在后）
                </span>
                <ol className="m-0 flex list-none flex-col gap-1.5 p-0">
                  {stagedFiles.map((f, i) => (
                    <li
                      key={`${f.name}-${f.size}-${f.lastModified}-${i}`}
                      className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-[11px]"
                    >
                      <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[10px] font-black text-slate-500">
                        {i + 1}
                      </span>
                      <span
                        className="min-w-0 flex-1 truncate font-bold text-slate-700"
                        title={f.name}
                      >
                        {f.name}
                      </span>
                      <span className="shrink-0 font-mono text-slate-400">
                        {formatSize(f.size)}
                      </span>
                      <button
                        type="button"
                        aria-label={`上移 ${f.name}`}
                        title="上移（提前合并）"
                        disabled={i === 0}
                        onClick={() => moveStaged(i, -1)}
                        className="shrink-0 rounded p-0.5 text-slate-400 hover:text-blue-600 disabled:opacity-30 disabled:hover:text-slate-400 cursor-pointer disabled:cursor-not-allowed"
                      >
                        <ArrowUp className="h-3.5 w-3.5" />
                      </button>
                      <button
                        type="button"
                        aria-label={`下移 ${f.name}`}
                        title="下移（延后合并）"
                        disabled={i === stagedFiles.length - 1}
                        onClick={() => moveStaged(i, 1)}
                        className="shrink-0 rounded p-0.5 text-slate-400 hover:text-blue-600 disabled:opacity-30 disabled:hover:text-slate-400 cursor-pointer disabled:cursor-not-allowed"
                      >
                        <ArrowDown className="h-3.5 w-3.5" />
                      </button>
                      <button
                        type="button"
                        aria-label={`移除 ${f.name}`}
                        title="移除"
                        onClick={() => removeStaged(i)}
                        className="shrink-0 rounded p-0.5 text-slate-400 hover:text-rose-600 cursor-pointer"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </li>
                  ))}
                </ol>
                <div className="flex items-center justify-between gap-2">
                  <button
                    type="button"
                    onClick={() => setStagedFiles([])}
                    className="rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-500 hover:border-slate-300 hover:text-slate-600 cursor-pointer"
                  >
                    清空
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleUpload()}
                    className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-blue-600 to-indigo-600 px-4 py-1.5 text-xs font-black text-white shadow-sm hover:from-blue-700 hover:to-indigo-700 cursor-pointer"
                  >
                    <UploadCloud className="h-3.5 w-3.5" />
                    上传并解析
                  </button>
                </div>
              </div>
            )}

            <div className="flex items-center justify-between gap-2 flex-wrap">
              <div className="flex items-center gap-1.5 text-[11px] text-slate-500 min-w-0">
                {quota?.mineru_mode === 'local' ? (
                  <>
                    <Server className="h-3.5 w-3.5 text-slate-400 shrink-0" />
                    <span>文档将由本地 / 局域网 MinerU 服务解析（数据不出网）</span>
                  </>
                ) : (
                  // 通道未知（quota 拿不到）时按云端披露：宁可多提醒一句数据出网
                  <>
                    <CloudUpload className="h-3.5 w-3.5 text-slate-400 shrink-0" />
                    <span>
                      文档将上传至 MinerU 云端服务（mineru.net）解析，请勿上传涉密材料
                    </span>
                  </>
                )}
              </div>
              <button
                type="button"
                onClick={() => setSettingsOpen((v) => !v)}
                className="inline-flex shrink-0 items-center gap-1 rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-500 hover:border-blue-300 hover:text-blue-600 cursor-pointer"
              >
                <Settings className="h-3 w-3" />
                {settingsOpen ? '收起设置' : '解析设置'}
              </button>
            </div>

            {settingsOpen && (
              <MineruSettingsSection onChanged={() => void refreshQuota()} />
            )}

            {/* 配额条：本地通道不计云配额，展示口径随生效通道切换 */}
            {quota && (
              <div
                className={`flex items-center justify-center gap-3 text-[11px] font-bold ${
                  quota.warning || !quota.token_configured ? 'text-amber-600' : 'text-slate-500'
                }`}
              >
                {!quota.token_configured ? (
                  <span>
                    解析服务未配置：请在「解析设置」配置 MinerU 通道（云端 Token 或本地 / 局域网服务地址），或联系管理员配置服务器
                  </span>
                ) : quota.mineru_mode === 'local' ? (
                  <span>本地 / 局域网解析通道：不消耗平台页数配额</span>
                ) : (
                  <>
                    <span>
                      今日剩余 {quota.user_remaining}/{quota.user_limit} 页
                    </span>
                    <span className="text-slate-300">|</span>
                    <span>平台剩余 {quota.platform_remaining} 页</span>
                    {quota.exhausted && <span>· 今日额度已用尽</span>}
                  </>
                )}
              </div>
            )}
          </div>
        )}

        {/* ============ Step 2 解析 ============ */}
        {step === 'parse' && doc && (
          <div className="flex flex-col items-center gap-3 py-6 text-center">
            <Loader2 className="h-7 w-7 text-blue-500 animate-spin" />
            <div className="flex items-center gap-1.5 text-sm font-black text-slate-700">
              <FileText className="h-4 w-4 text-slate-400" />
              <span className="max-w-[28rem] truncate" title={doc.filename}>
                {doc.filename}
              </span>
            </div>
            <span className="text-xs font-bold text-blue-600">
              {DOC_STATUS_LABELS[doc.status] ?? doc.status}
              {doc.page_count ? ` · 已识别 ${doc.page_count} 页` : ''}
            </span>
            <span className="text-[11px] text-slate-400">
              {doc.mineru_mode === 'local'
                ? '本地 MinerU 解析通常需要 10~60 秒（视服务性能），页面会自动刷新进度'
                : 'MinerU 云端解析通常需要 10~60 秒，页面会自动刷新进度'}
            </span>
            <button
              type="button"
              onClick={reset}
              className="mt-1 inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-500 hover:border-rose-300 hover:text-rose-600 cursor-pointer"
            >
              <RotateCcw className="h-3 w-3" />
              取消，换一个文件
            </button>
          </div>
        )}

        {/* ============ Step 3 整理 ============ */}
        {step === 'organize' && doc && (
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-2 text-xs font-bold text-slate-600">
              <FileText className="h-3.5 w-3.5 text-slate-400 shrink-0" />
              <span className="truncate" title={doc.filename}>
                {doc.filename}
              </span>
              <span className="text-slate-300">·</span>
              <span className="shrink-0">{doc.page_count ? `${doc.page_count} 页` : '页数未知'}</span>
              {rawText !== null && (
                <>
                  <span className="text-slate-300">·</span>
                  <button
                    type="button"
                    onClick={() => setShowRaw((v) => !v)}
                    className="shrink-0 text-blue-600 hover:text-blue-700 cursor-pointer"
                  >
                    {showRaw ? '收起原文' : `查看原文（${rawText.length} 字）`}
                  </button>
                </>
              )}
            </div>

            {showRaw && rawText !== null && (
              <pre className="max-h-56 overflow-auto rounded-xl bg-slate-50 border border-slate-100 p-3 text-[11px] leading-relaxed text-slate-600 whitespace-pre-wrap break-all m-0">
                {rawText}
              </pre>
            )}

            {doc.status === 'organized' && !organize && (
              <div className="flex items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50/70 px-3 py-2 text-[11px] font-bold text-emerald-700">
                <span>
                  本文档已整理过
                  {doc.organize_prompt_version ? `（模板 ${doc.organize_prompt_version}）` : ''}
                  ，可直接进入下一步
                </span>
                <button
                  type="button"
                  onClick={() => setStep('confirm')}
                  className="shrink-0 rounded-full bg-emerald-600 px-3 py-1 text-[11px] font-bold text-white hover:bg-emerald-700 cursor-pointer"
                >
                  继续
                </button>
              </div>
            )}

            <fieldset className="flex flex-col gap-2 m-0 p-0 border-0">
              <legend className="text-[11px] font-black text-slate-500 mb-1">
                整理口径
              </legend>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {Object.entries(ORGANIZE_KIND_LABELS).map(([value, label]) => (
                  <label
                    key={value}
                    className={`flex flex-col gap-0.5 rounded-xl border px-3 py-2 cursor-pointer transition-colors ${
                      kind === value
                        ? 'border-blue-300 bg-blue-50/70'
                        : 'border-slate-200 bg-white hover:border-blue-200'
                    }`}
                  >
                    <span className="flex items-center gap-1.5 text-xs font-black text-slate-700">
                      <input
                        type="radio"
                        name="organize-kind"
                        value={value}
                        checked={kind === value}
                        onChange={() => setKind(value)}
                        className="accent-blue-600"
                      />
                      {label}
                    </span>
                    <span className="text-[10px] text-slate-500 pl-4">
                      {KIND_HINTS[value] ?? ''}
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>

            <textarea
              value={extra}
              onChange={(e) => setExtra(e.target.value)}
              rows={2}
              maxLength={2000}
              placeholder="可选：补充关注点（如「只看动量类因子」「复现时按 A 股口径」）"
              className="w-full resize-none rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs text-slate-700 placeholder:text-slate-300 focus:outline-none focus:ring-1 focus:ring-blue-200"
            />

            <div className="flex items-center justify-between gap-3 flex-wrap">
              <label className="flex items-center gap-2 text-[11px] font-bold text-slate-500 cursor-pointer">
                <input
                  type="checkbox"
                  checked={autoDirect}
                  onChange={(e) => setAutoDirect(e.target.checked)}
                  className="accent-blue-600"
                />
                整理后直通：整理完成立即开始挖掘（跳过人工确认）
              </label>
              <button
                type="button"
                onClick={() => void handleOrganize()}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-blue-600 to-indigo-600 px-4 py-1.5 text-xs font-black text-white shadow-sm hover:from-blue-700 hover:to-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
              >
                {busy ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    整理中（长文可达数分钟）…
                  </>
                ) : (
                  <>
                    <Wand2 className="h-3.5 w-3.5" />
                    {doc.status === 'organized' ? '重新整理' : '开始整理'}
                  </>
                )}
              </button>
            </div>
          </div>
        )}

        {/* ============ Step 4 确认挖掘 ============ */}
        {step === 'confirm' && doc && (
          <div className="flex flex-col gap-3">
            {organize && (
              <details className="rounded-xl border border-slate-100 bg-slate-50/60 px-3 py-2">
                <summary className="cursor-pointer text-[11px] font-bold text-slate-500">
                  整理结果预览（模板 {organize.prompt_version}
                  {organize.truncated ? ' · 长文采样' : ''}）
                </summary>
                <div className="mt-2 max-h-64 overflow-auto prose-sm text-xs text-slate-700">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {organize.markdown}
                  </ReactMarkdown>
                </div>
              </details>
            )}

            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] font-black text-slate-500">
                挖掘方向草稿（可直接编辑，将原样下发 RD-Agent）
              </span>
              <span
                className={`text-[11px] font-mono ${
                  overLimit ? 'text-rose-600 font-bold' : 'text-slate-400'
                }`}
              >
                {directionDraft.trim().length}/{DOC_MAX_DIRECTION_CHARS}
              </span>
            </div>
            <textarea
              value={directionDraft}
              onChange={(e) => setDirectionDraft(e.target.value)}
              rows={12}
              className={`w-full resize-y rounded-xl border bg-white px-3 py-2 font-mono text-xs leading-relaxed text-slate-700 focus:outline-none focus:ring-1 ${
                overLimit
                  ? 'border-rose-300 focus:ring-rose-200'
                  : 'border-slate-200 focus:ring-blue-200'
              }`}
            />
            {overLimit && (
              <span className="text-[11px] font-bold text-rose-600">
                超出 {DOC_MAX_DIRECTION_CHARS} 字上限，请精简后再开始挖掘
              </span>
            )}

            <div className="flex items-center justify-between gap-3">
              <button
                type="button"
                onClick={() => setStep('organize')}
                className="text-[11px] font-bold text-slate-500 hover:text-blue-600 cursor-pointer"
              >
                ← 返回整理
              </button>
              <button
                type="button"
                onClick={() => startWith(directionDraft)}
                disabled={isRunning || busy || overLimit || !directionDraft.trim()}
                className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-blue-600 to-indigo-600 px-5 py-2 text-xs font-black text-white shadow-sm hover:from-blue-700 hover:to-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
              >
                {isRunning ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    任务进行中…
                  </>
                ) : (
                  <>
                    <Play className="h-3.5 w-3.5" />
                    开始挖掘
                  </>
                )}
              </button>
            </div>
            <span className="text-[10px] text-slate-400 text-right">
              市场 / 股票池 / 数据源按「设置」中的默认值执行；本次任务会记录文档出处
            </span>
          </div>
        )}
      </div>
    </div>
  );
};

export default DocMiningPanel;
