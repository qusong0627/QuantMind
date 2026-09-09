/** 实盘券商接入配置卡：按市场可选，配置存 Trade Redis。 */
import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Input, Select, Tag, Typography, message } from 'antd';
import { BankOutlined, CheckCircleOutlined, ReloadOutlined } from '@ant-design/icons';
import { authService } from '../../../features/auth/services/authService';
import { SERVICE_URLS } from '../../../config/services';

const { Text } = Typography;

type BrokerKey = string;

const BROKERS_BY_MARKET: Record<string, { key: BrokerKey; label: string; desc: string }[]> = {
  CN: [
    { key: 'qmt_exec', label: '大 QMT(执行端)', desc: 'QMT 内置 Python 跑 big-convert RPC 服务端，需在 QMT 机器上常驻；下单走官方内置通道' },
    { key: 'tdx', label: '通达信(TDX 桥)', desc: 'Windows 常驻 TDX 桥（HTTP），需 token 一致且通达信已登录' },
  ],
  HK: [
    { key: 'futu', label: '富途证券', desc: '需本机常驻 FutuOpenD 网关；港股行情强；支持模拟环境' },
    { key: 'tiger', label: '老虎证券', desc: '纯云端 API，无需网关；SIM 模拟账户可直接演练' },
    { key: 'ib', label: '盈透 IB', desc: '需常驻 IB Gateway 容器（paper 4002 / real 4001）' },
  ],
  US: [
    { key: 'tiger', label: '老虎证券', desc: '纯云端 API，无需网关；美股主力' },
    { key: 'ib', label: '盈透 IB', desc: '需常驻 IB Gateway 容器；全球市场' },
    { key: 'futu', label: '富途证券', desc: '需本机常驻 FutuOpenD 网关' },
  ],
  FUTURES: [
    { key: 'ib', label: '盈透 IB', desc: '外盘期货（CME 等）经 IB 接入；内盘期货暂不支持' },
  ],
  CRYPTO: [],
};

interface FieldDef {
  name: string;
  label: string;
  sensitive?: boolean;
  placeholder: string;
  options?: { value: string; label: string }[];
}

const FIELD_DEFS: Record<BrokerKey, FieldDef[]> = {
  qmt_exec: [
    { name: 'enabled', label: '启用执行端', placeholder: '', options: [
      { value: 'true', label: 'true（启用）' },
      { value: 'false', label: 'false（停用）' },
    ] },
    { name: 'account_id', label: '资金账号', placeholder: 'QMT 登录的资金账号，如 8888' },
    { name: 'account_type', label: '账号类型', placeholder: '', options: [
      { value: 'STOCK', label: 'STOCK（普通）' },
      { value: 'CREDIT', label: 'CREDIT（信用）' },
    ] },
    { name: 'strategy_name', label: '策略名', placeholder: 'quantmind（用于识别本系统的委托）' },
    { name: 'redis_host', label: 'RPC Redis 地址', placeholder: 'big-convert 服务端所在机器的 IP，如 192.168.31.20' },
    { name: 'redis_port', label: 'RPC Redis 端口', placeholder: '6379' },
    { name: 'redis_db', label: 'RPC Redis 库', placeholder: '0' },
    { name: 'redis_password', label: 'RPC Redis 密码', sensitive: true, placeholder: 'big-convert 传输通道密码' },
  ],
  tdx: [
    { name: 'bridge_url', label: '桥地址', placeholder: '如 http://192.168.31.13:8550' },
    { name: 'bridge_token', label: '桥 Token', sensitive: true, placeholder: '与 Windows 桥一致的 token' },
    { name: 'account', label: '资金账号', placeholder: '通达信资金账号（可留空）' },
    { name: 'account_type', label: '账号类型', placeholder: '', options: [
      { value: 'stock', label: 'stock（普通）' },
      { value: 'credit', label: 'credit（信用）' },
    ] },
  ],
  tiger: [
    { name: 'tiger_id', label: 'Tiger ID', placeholder: '如 TQ12345（老虎 OpenAPI 平台获取）' },
    { name: 'rsa_private_key', label: 'RSA 私钥', sensitive: true, placeholder: 'PEM 文本（-----BEGIN 开头）或服务器上的文件路径' },
    { name: 'account', label: '交易账户', placeholder: '实盘 U 开头 / 模拟 SIM 开头，如 SIM123456' },
  ],
  futu: [
    { name: 'opend_host', label: 'FutuOpenD 地址', placeholder: 'OpenD 所在机器的局域网 IP，如 192.168.31.68' },
    { name: 'opend_port', label: 'FutuOpenD 端口', placeholder: '11111' },
    { name: 'trade_pwd_md5', label: '交易密码 MD5', sensitive: true, placeholder: '交易密码的 MD5（实盘下单前自动解锁）' },
    { name: 'trade_env', label: '交易环境', placeholder: '', options: [
      { value: 'SIMULATE', label: 'SIMULATE（模拟）' },
      { value: 'REAL', label: 'REAL（实盘）' },
    ] },
  ],
  ib: [
    { name: 'gateway_host', label: 'Gateway 地址', placeholder: '127.0.0.1' },
    { name: 'gateway_port', label: 'Gateway 端口', placeholder: '4002=模拟 / 4001=实盘' },
    { name: 'client_id', label: 'Client ID', placeholder: '7' },
  ],
};

const apiBase = `${SERVICE_URLS.API_GATEWAY}/api/v1`;

export const BrokerConfigCard: React.FC<{ market: string }> = ({ market }) => {
  const brokers = BROKERS_BY_MARKET[market.toUpperCase()] ?? [];
  const [selected, setSelected] = useState<BrokerKey | undefined>(brokers[0]?.key);
  const [values, setValues] = useState<Record<string, string>>({});
  const [configured, setConfigured] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);

  const authHeaders = () => {
    const token = authService.getAccessToken();
    return token ? { Authorization: `Bearer ${token}` } : undefined;
  };

  const load = useCallback(async () => {
    if (!selected) return;
    setLoading(true);
    try {
      const resp = await fetch(`${apiBase}/broker-config/${selected}`, { headers: authHeaders() });
      const data = await resp.json();
      const fields = data?.fields ?? {};
      const next: Record<string, string> = {};
      const conf: Record<string, boolean> = {};
      Object.entries(fields).forEach(([key, value]) => {
        if (key.endsWith('_configured')) {
          conf[key.replace('_configured', '')] = Boolean(value);
        } else {
          next[key] = String(value ?? '');
        }
      });
      setValues(next);
      setConfigured(conf);
    } catch {
      message.error('加载券商配置失败');
    } finally {
      setLoading(false);
    }
  }, [selected]);

  useEffect(() => {
    setValues({});
    setConfigured({});
    setSelected(brokers[0]?.key);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!selected) return;
    setSaving(true);
    try {
      const payload: Record<string, string> = {};
      FIELD_DEFS[selected].forEach(({ name }) => {
        if (values[name] !== undefined && values[name] !== '') payload[name] = values[name];
      });
      const resp = await fetch(`${apiBase}/broker-config/${selected}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ values: payload }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => null);
        throw new Error(err?.detail || `HTTP ${resp.status}`);
      }
      message.success('券商配置已保存');
      await load();
    } catch (e: any) {
      message.error(e?.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const testConnection = async () => {
    if (!selected) return;
    setTesting(true);
    setTestResult(null);
    try {
      const resp = await fetch(`${apiBase}/broker-config/${selected}/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ values, trade_env: values.trade_env }),
      });
      const data = await resp.json();
      setTestResult({ ok: Boolean(data?.success), message: String(data?.message || '') });
    } catch (e: any) {
      setTestResult({ ok: false, message: e?.message || '测试请求失败' });
    } finally {
      setTesting(false);
    }
  };

  if (brokers.length === 0) {
    return (
      <Alert
        type="info"
        showIcon
        message="当前市场暂无支持的实盘券商通道"
        description="加密货币市场暂未接入实盘交易；A 股使用通达信/QMT 通道。"
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-bold text-gray-900 flex items-center gap-1.5">
            <BankOutlined className="text-indigo-500" /> 券商实盘接入
          </div>
          <p className="text-xs text-gray-500 mt-1">
            配置保存在服务器（敏感字段只写不回显）。下单前请确认已在券商侧开通 OpenAPI 权限；
            富途需先人工登录 FutuOpenD；IB 需先启动 IB Gateway；A 股需先启动 QMT 执行端或 TDX 桥。
          </p>
        </div>
        <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
      </div>

      <div className="flex flex-wrap gap-2">
        {brokers.map(({ key, label, desc }) => (
          <button
            key={key}
            onClick={() => setSelected(key)}
            className={`px-3 py-2 rounded-xl text-xs font-bold border transition-colors text-left ${
              selected === key
                ? 'bg-indigo-50 text-indigo-700 border-indigo-300 shadow-sm'
                : 'bg-white text-gray-600 border-gray-200 hover:border-gray-300'
            }`}
          >
            <div className="flex items-center gap-1.5">
              <BankOutlined className={selected === key ? 'text-indigo-500' : 'text-gray-400'} />
              {label}
              {key === 'futu' && <Tag className="!text-[10px] !mr-0">需 FutuOpenD</Tag>}
              {key === 'ib' && <Tag className="!text-[10px] !mr-0">需 IB Gateway</Tag>}
              {key === 'tiger' && <Tag color="green" className="!text-[10px] !mr-0">免网关</Tag>}
            </div>
            <div className="text-[10px] font-normal text-gray-400 mt-0.5 max-w-[240px]">{desc}</div>
          </button>
        ))}
      </div>

      {selected && (
        <div className="rounded-2xl border border-gray-200 bg-gray-50/50 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <Text strong>{brokers.find((b) => b.key === selected)?.label}</Text>
            <Text type="secondary" className="text-xs">
              {brokers.find((b) => b.key === selected)?.desc}
            </Text>
          </div>
          {selected === 'futu' && (
            <div className="text-[11px] leading-5 text-amber-700 bg-amber-50 border border-amber-200 rounded-xl p-2.5">
              配置步骤：① 在本地或局域网机器上安装并启动 <b>FutuOpenD</b>（富途官网下载，支持 Windows/Mac/Linux）；② 在 OpenD 客户端<b>扫码登录</b>富途账号（首次需设备验证）；③ 上方填写 OpenD 所在机器的<b>局域网 IP 和端口</b>（默认 11111）；④ 填写交易密码 MD5 后点「测试连接」。
            </div>
          )}
          {selected === 'ib' && (
            <div className="text-[11px] leading-5 text-amber-700 bg-amber-50 border border-amber-200 rounded-xl p-2.5">
              配置步骤：① 启动 <b>IB Gateway</b>（或 TWS），端口 4002=模拟账户 / 4001=实盘；② Gateway 启动时用 IB 账号密码登录（每次会话需重新登录，可用自动化容器托管）；③ 上方填写 Gateway 所在机器的<b>局域网 IP 和端口</b>；④ 点「测试连接」。需在 IB 端开通对应市场行情与交易权限。
            </div>
          )}
          {selected === 'tiger' && (
            <div className="text-[11px] leading-5 text-amber-700 bg-amber-50 border border-amber-200 rounded-xl p-2.5">
              配置步骤：① 在老虎证券 OpenAPI 开放平台创建应用，获得 <b>Tiger ID</b> 并生成 <b>RSA 密钥对</b>（公钥绑定账户，私钥粘贴到下方）；② 填写交易账户号（U 开头=实盘，SIM 开头=模拟，模拟账户可直接演练）；③ 无需网关，保存后点「测试连接」即可。
            </div>
          )}
          {selected === 'qmt_exec' && (
            <div className="text-[11px] leading-5 text-amber-700 bg-amber-50 border border-amber-200 rounded-xl p-2.5">
              配置步骤：① 在 QMT 那台 Windows 上安装 <b>xtquant-big-convert</b> 并常驻运行 RPC 服务端（QMT 内置 Python）；② 服务端需开启 <b>rpc_allow_order_methods</b> 才会真正下单；③ 上方填写该机器上的 <b>RPC Redis 地址/端口/密码</b>（本系统经此通道下发委托并轮询成交）；④ 保存后点「测试连接」应返回真实资金与持仓。成交无推送，靠 1–3 秒轮询回收。
            </div>
          )}
          {selected === 'tdx' && (
            <div className="text-[11px] leading-5 text-amber-700 bg-amber-50 border border-amber-200 rounded-xl p-2.5">
              配置步骤：① 在 Windows 上启动 TDX 桥（HTTP 服务）并确保通达信已登录；② 上方填写桥的 <b>局域网地址</b> 与 <b>Token</b>（两侧一致）；③ 保存后点「测试连接」应返回账户资金。注意：TDX 桥与 QMT 执行端是两条独立通道，同一市场只能选其一。
            </div>
          )}
          {FIELD_DEFS[selected].map(({ name, label, sensitive, placeholder, options }) => {
            const isConfigured = configured[name];
            return (
              <div key={name}>
                <div className="text-xs font-medium text-gray-600 mb-1 flex items-center gap-2">
                  {label}
                  {sensitive && isConfigured && (
                    <Tag color="green" className="!text-[10px] !mr-0">
                      <CheckCircleOutlined /> 已配置
                    </Tag>
                  )}
                </div>
                {options ? (
                  <Select
                    value={values[name] || options[0].value}
                    onChange={(v) => setValues({ ...values, [name]: v })}
                    style={{ width: 200 }}
                    options={options}
                  />
                ) : (
                  <Input
                    value={values[name] ?? ''}
                    placeholder={sensitive ? `${placeholder}（已配置则留空保持不变）` : placeholder}
                    onChange={(e) => setValues({ ...values, [name]: e.target.value })}
                  />
                )}
              </div>
            );
          })}
          <div className="flex items-center justify-between gap-2 pt-1">
            <Button icon={<ReloadOutlined />} loading={testing} onClick={testConnection}>
              测试连接
            </Button>
            <div className="flex gap-2">
              <Button onClick={() => void load()}>还原</Button>
              <Button type="primary" loading={saving} onClick={save}>保存配置</Button>
            </div>
          </div>
          {testResult && (
            <Alert
              type={testResult.ok ? 'success' : 'error'}
              showIcon
              message={testResult.message}
              className="!text-xs"
            />
          )}
        </div>
      )}
    </div>
  );
};

export default BrokerConfigCard;
