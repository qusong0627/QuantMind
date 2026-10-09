/**
 * MinerU 解析设置（因子挖掘内，T-FM-21）：云端 / 本地局域网通道二选一。
 *
 * 四条纪律：
 * 1. **唯一配置入口**：MinerU 的 Token / 本地服务地址在这里设置（用户中心
 *    不再承载文档解析配置），保存即生效于下一次上传。
 * 2. **密钥只回掩码**：输入框留空 = 保留已存密钥（回存掩码会把真密钥写坏），
 *    彻底清除走「清除全部设置」。
 * 3. **生效来源如实展示**：本页设置 > 部署默认（服务器 .env）；部署固定为
 *    本地模式时云端配置不生效（隐私硬顶），界面必须明示，不让用户对着
 *    「保存成功」猜为什么还在走别的通道。
 * 4. **读故障 ≠ 没配**：readable=false（Redis 读不到）时显示读故障并锁定
 *    保存——把故障显示成空设置会诱导用户覆盖掉自己已存的密钥。
 */
import React, { useCallback, useEffect, useState } from 'react';
import { AlertCircle, CloudUpload, Loader2, Server, Trash2 } from 'lucide-react';
import type {
  MineruMode,
  MineruSettingsPayload,
  MineruSettingsView,
} from '../services-v2/docMiningApi';
import {
  MINERU_LOCAL_TIERS,
  clearMineruSettings,
  extractDetail,
  getMineruSettings,
  saveMineruSettings,
} from '../services-v2/docMiningApi';

const INPUT_CLS =
  'w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs text-slate-700 placeholder:text-slate-300 focus:outline-none focus:ring-1 focus:ring-blue-200 disabled:bg-slate-50 disabled:text-slate-400';

const MODE_LABELS: Record<MineruMode, string> = {
  cloud: '云端（mineru.net）',
  local: '本地 / 局域网',
};

export interface MineruSettingsSectionProps {
  /** 保存/清除成功后回调（父面板刷新配额条与通道披露） */
  onChanged?: () => void;
}

export const MineruSettingsSection: React.FC<MineruSettingsSectionProps> = ({
  onChanged,
}) => {
  const [view, setView] = useState<MineruSettingsView | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const [mode, setMode] = useState<MineruMode>('cloud');
  const [apiToken, setApiToken] = useState('');
  const [localUrl, setLocalUrl] = useState('');
  const [localApiKey, setLocalApiKey] = useState('');
  const [localTier, setLocalTier] = useState('');

  /** 视图 → 表单：密钥输入框一律清空（回显的是掩码，留在框里会误导） */
  const applyView = useCallback((v: MineruSettingsView) => {
    setView(v);
    setMode(v.settings?.mode ?? v.env_mode ?? 'cloud');
    setLocalUrl(v.settings?.local_url ?? '');
    setLocalTier(v.settings?.local_tier ?? '');
    setApiToken('');
    setLocalApiKey('');
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const v = await getMineruSettings();
        if (!cancelled) applyView(v);
      } catch (e: unknown) {
        if (!cancelled) setError(`设置读取失败：${extractDetail(e)}`);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [applyView]);

  const ready = view !== null && view.readable;

  const handleSave = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    setConfirmClear(false);
    try {
      const payload: MineruSettingsPayload = {
        mode,
        // 空串 = 后端保留已存密钥（doc_mining_settings._merge_secret 语义）
        api_token: apiToken,
        local_url: localUrl,
        local_api_key: localApiKey,
        // '' = 清为服务端默认档
        local_tier: localTier,
      };
      applyView(await saveMineruSettings(payload));
      setNotice('已保存：后续上传的文档按新通道解析。');
      onChanged?.();
    } catch (e: unknown) {
      setError(extractDetail(e));
    } finally {
      setBusy(false);
    }
  }, [busy, mode, apiToken, localUrl, localApiKey, localTier, applyView, onChanged]);

  const handleClear = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      applyView(await clearMineruSettings());
      setConfirmClear(false);
      setNotice('已清除本页设置，回落到部署默认通道。');
      onChanged?.();
    } catch (e: unknown) {
      setError(extractDetail(e));
    } finally {
      setBusy(false);
    }
  }, [busy, applyView, onChanged]);

  const effectiveLabel = view?.effective_mode
    ? MODE_LABELS[view.effective_mode]
    : '未配置';
  const sourceLabel =
    view?.source === 'user'
      ? '本页设置'
      : view?.source === 'env'
        ? '部署默认（服务器 .env）'
        : null;

  return (
    <div
      id="mineru-settings-section"
      className="flex flex-col gap-3 rounded-xl border border-blue-100 bg-blue-50/30 px-4 py-3"
    >
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <span className="text-xs font-black text-slate-700">
          解析设置（MinerU）
        </span>
        {view && !view.readable ? (
          <span className="text-[11px] font-bold text-rose-600">
            设置存储读取失败
          </span>
        ) : (
          <span className="text-[11px] font-bold text-slate-500">
            当前生效：{sourceLabel ? `${sourceLabel} · ` : ''}
            {effectiveLabel}
          </span>
        )}
      </div>

      {loading && (
        <div className="flex items-center gap-2 text-[11px] font-bold text-slate-500">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          读取设置中…
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50/80 px-3 py-2 text-[11px] font-bold text-rose-600">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          <span className="min-w-0 whitespace-pre-wrap break-all">{error}</span>
        </div>
      )}
      {notice && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50/80 px-3 py-2 text-[11px] font-bold text-emerald-700">
          {notice}
        </div>
      )}

      {view && !view.readable && (
        <div className="rounded-lg border border-rose-200 bg-rose-50/70 px-3 py-2 text-[11px] font-bold text-rose-600">
          设置存储暂不可用（读取失败）：为避免覆盖已存密钥，修改与保存已锁定，请稍后重试。
        </div>
      )}

      {view?.env_mode === 'local' && (
        <div className="rounded-lg border border-sky-200 bg-sky-50/70 px-3 py-2 text-[11px] font-bold text-sky-700">
          本部署已固定为本地解析模式（服务器 .env）：文档始终在内网解析，
          云端配置在此部署不生效；你自己的本地 / 局域网服务地址仍然有效。
        </div>
      )}
      {view?.env_mode !== 'local' && view?.env_configured && (
        <div className="text-[11px] text-slate-500">
          部署默认已配置云端通道：本页未单独设置时，上传将使用它。
        </div>
      )}
      {view && view.readable && !view.env_configured && view.source === 'none' && (
        <div className="rounded-lg border border-amber-200 bg-amber-50/80 px-3 py-2 text-[11px] font-bold text-amber-700">
          尚未配置解析通道：上传文档会提示「解析服务未就绪」，
          请先保存下面的设置，或联系管理员配置服务器 .env。
        </div>
      )}

      <fieldset
        className="flex flex-col gap-2 m-0 p-0 border-0"
        disabled={!ready || busy}
      >
        <legend className="text-[11px] font-black text-slate-500 mb-1">
          解析通道
        </legend>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          <label
            className={`flex flex-col gap-0.5 rounded-xl border px-3 py-2 cursor-pointer transition-colors ${
              mode === 'cloud'
                ? 'border-blue-300 bg-white'
                : 'border-slate-200 bg-white/60 hover:border-blue-200'
            }`}
          >
            <span className="flex items-center gap-1.5 text-xs font-black text-slate-700">
              <input
                type="radio"
                name="mineru-mode"
                value="cloud"
                checked={mode === 'cloud'}
                onChange={() => setMode('cloud')}
                className="accent-blue-600"
              />
              <CloudUpload className="h-3.5 w-3.5 text-slate-400" />
              云端（mineru.net）
            </span>
            <span className="text-[10px] text-slate-500 pl-4">
              文档会上传至公有云解析，请勿上传涉密材料；按平台页数配额计。
            </span>
          </label>
          <label
            className={`flex flex-col gap-0.5 rounded-xl border px-3 py-2 cursor-pointer transition-colors ${
              mode === 'local'
                ? 'border-blue-300 bg-white'
                : 'border-slate-200 bg-white/60 hover:border-blue-200'
            }`}
          >
            <span className="flex items-center gap-1.5 text-xs font-black text-slate-700">
              <input
                type="radio"
                name="mineru-mode"
                value="local"
                checked={mode === 'local'}
                onChange={() => setMode('local')}
                className="accent-blue-600"
              />
              <Server className="h-3.5 w-3.5 text-slate-400" />
              本地 / 局域网
            </span>
            <span className="text-[10px] text-slate-500 pl-4">
              指向自建 MinerU 服务，文档不出网；不消耗平台页数配额。
            </span>
          </label>
        </div>

        {mode === 'cloud' ? (
          <div className="flex flex-col gap-1.5">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-black text-slate-600">
                API Token
              </span>
              <input
                id="mineru-cloud-token"
                type="password"
                autoComplete="off"
                value={apiToken}
                onChange={(e) => setApiToken(e.target.value)}
                placeholder={
                  view?.settings?.api_token_set
                    ? `已保存 ${view.settings.api_token_masked}（留空保持不变）`
                    : '粘贴 mineru.net 的 API Token'
                }
                className={INPUT_CLS}
              />
            </label>
            <span className="text-[10px] text-slate-400">
              Token 仅存于服务器，回显只给掩码；更换直接粘贴新 Token 保存。
            </span>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-black text-slate-600">
                服务地址
              </span>
              <input
                id="mineru-local-url"
                type="text"
                autoComplete="off"
                value={localUrl}
                onChange={(e) => setLocalUrl(e.target.value)}
                placeholder="http://192.168.1.10:8000"
                className={INPUT_CLS}
              />
            </label>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <label className="flex flex-col gap-1">
                <span className="text-[11px] font-black text-slate-600">
                  解析档位
                </span>
                <select
                  id="mineru-local-tier"
                  value={localTier}
                  onChange={(e) => setLocalTier(e.target.value)}
                  className={INPUT_CLS}
                >
                  {MINERU_LOCAL_TIERS.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[11px] font-black text-slate-600">
                  API Key（可选）
                </span>
                <input
                  id="mineru-local-key"
                  type="password"
                  autoComplete="off"
                  value={localApiKey}
                  onChange={(e) => setLocalApiKey(e.target.value)}
                  placeholder={
                    view?.settings?.local_api_key_set
                      ? `已保存 ${view.settings.local_api_key_masked}（留空保持不变）`
                      : '服务端若开启鉴权则填写'
                  }
                  className={INPUT_CLS}
                />
              </label>
            </div>
            <span className="text-[10px] text-slate-400">
              地址为 MinerU 4.x 服务的接口根地址（http/https）；文档不出网，
              失败重试与解析进度照常展示。
            </span>
          </div>
        )}

        <div className="flex items-center justify-between gap-2 flex-wrap mt-1">
          {confirmClear ? (
            <span className="flex items-center gap-2 text-[11px] font-bold text-rose-600">
              清除本页全部设置（含已存密钥）？
              <button
                type="button"
                onClick={() => void handleClear()}
                disabled={busy}
                className="rounded-full bg-rose-600 px-3 py-1 text-[11px] font-bold text-white hover:bg-rose-700 disabled:opacity-50 cursor-pointer"
              >
                确认清除
              </button>
              <button
                type="button"
                onClick={() => setConfirmClear(false)}
                className="rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-500 hover:text-slate-600 cursor-pointer"
              >
                取消
              </button>
            </span>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmClear(true)}
              disabled={!ready || busy}
              className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-500 hover:border-rose-300 hover:text-rose-600 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
            >
              <Trash2 className="h-3 w-3" />
              清除全部设置
            </button>
          )}
          <button
            type="button"
            onClick={() => void handleSave()}
            disabled={!ready || busy}
            className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-blue-600 to-indigo-600 px-4 py-1.5 text-xs font-black text-white shadow-sm hover:from-blue-700 hover:to-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
          >
            {busy ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                保存中…
              </>
            ) : (
              '保存设置'
            )}
          </button>
        </div>
      </fieldset>

      <span className="text-[10px] text-slate-400">
        本设置只作用于因子挖掘的文档解析（按账号保存）。
      </span>
    </div>
  );
};

export default MineruSettingsSection;
