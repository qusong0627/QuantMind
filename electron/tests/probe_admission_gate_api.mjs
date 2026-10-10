/**
 * T-MV-05 验收探针（API 面，2026-10-10）：入库闸门参数的活体接线。
 *
 * 前端半边（设置页文案 + localStorage 落档）见 probe_mining_quality_gate.mjs；
 * 后端判定半边（hard 拒不入池/soft 留痕/虚拟入池分位）见
 * backend/tests/test_pool_service.py TestAdmissionGate（真库集成）。
 * 本探针补的是**活体路由链**：真实 dispatch（evolve query / mining-batch body）
 * 收/拒 quality_gate_mode，派出的任务能被取消（不留跑着的探针任务）。
 *
 *  1) evolve?quality_gate_mode=banana → 400（在 LLM/市场校验之前拒）
 *  2) evolve?quality_gate_mode=off → 200 带 task_id（真派发）→ cancel → cancelled
 *  3) /mining/batch body quality_gate_mode=banana → 400 整包拒（一条不派）
 *  4) /mining/batch body quality_gate_mode=off → 200 逐条回执 → 全部 cancel
 *
 * 任务行的 quality_gate_mode 落值由 test_mining_task_store 真库钉死；本探针
 * 打印任务 id 供人工/脚本复核（任务行随后由调用方清理）。
 *
 * 用法：node electron/tests/probe_admission_gate_api.mjs
 */
const BASE = process.env.QM_BASE || 'http://localhost:3080';
const API = `${BASE}/api/v1`;
let pass = 0;
let fail = 0;
const ok = (cond, label, extra = '') => {
  if (cond) {
    pass++;
    console.log(`  ✓ ${label}${extra ? ` — ${extra}` : ''}`);
  } else {
    fail++;
    console.log(`  ✗ ${label}${extra ? ` — ${extra}` : ''}`);
  }
};

let token = '';
async function call(method, path, body) {
  const res = await fetch(`${API}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  let json = null;
  try {
    json = await res.json();
  } catch {
    /* 非 JSON 响应（如 204） */
  }
  return { status: res.status, json };
}

// ── 登录（本地开发实例 admin）──
const login = await call('POST', '/auth/login', {
  tenant_id: 'default',
  username: 'admin',
  password: 'admin123',
});
ok(login.status === 200 && !!login.json?.access_token, '登录成功', `status=${login.status}`);
if (!login.json?.access_token) {
  console.log(`\nRESULT FAILURES: 登录失败，后续无法执行`);
  process.exit(1);
}
token = login.json.access_token;

const cancelled = [];
const cancelTask = async (taskId) => {
  const res = await call('POST', `/alpha-agent/tasks/${taskId}/cancel`);
  cancelled.push({ taskId, status: res.status });
  return res;
};

// ── 1) evolve 非法模式 → 400（不烧 LLM、不派任务）──
const bad = await call('POST', '/alpha-agent/evolve?quality_gate_mode=banana&direction=%E6%8E%A2%E9%92%88');
ok(bad.status === 400, 'evolve 非法 quality_gate_mode=banana → 400', `status=${bad.status}`);
ok(
  String(bad.json?.detail || '').includes('quality_gate_mode'),
  '400 detail 点名 quality_gate_mode',
  String(bad.json?.detail || '').slice(0, 80),
);

// ── 2) evolve 显式 off → 真派发；取消收尾 ──
const dispatch = await call(
  'POST',
  '/alpha-agent/evolve?quality_gate_mode=off&direction=%E5%85%A5%E5%BA%93%E9%97%B8%E9%97%A8%E6%8E%A2%E9%92%88',
);
const taskId = dispatch.json?.data?.task_id;
ok(
  dispatch.status === 200 && typeof taskId === 'string' && taskId.length > 0,
  'evolve quality_gate_mode=off → 200 带 task_id',
  `status=${dispatch.status} task=${taskId || '(缺失)'}`,
);
if (taskId) {
  console.log(`  · 任务行待复核：quality_gate_mode 应为 'off'，task_id=${taskId}`);
  const c = await cancelTask(taskId);
  ok(c.status === 200, '取消探索任务 → 200', `status=${c.status}`);
  // 「取消」的权威面是 PG 历史（/tasks/history）：内存态按设计收尾为
  // failed+"Cancelled by user"（test_mining_task_center_launcher 钉死语义），
  // 拿 /tasks/{id} 判取消会误报——历史页必须显示 cancelled。
  const hist = await call('GET', '/alpha-agent/tasks/history?status=cancelled&limit=20');
  const row = (hist.json?.data?.tasks ?? []).find((r) => r.task_id === taskId);
  ok(row?.status === 'cancelled', '历史页（PG 权威）落 cancelled', `row=${row?.status ?? '(缺失)'}`);
}

// ── 3) mining-batch 非法模式 → 整包 400（一条不派）──
const badBatch = await call('POST', '/alpha-agent/mining/batch', {
  directions: ['探针方向一'],
  quality_gate_mode: 'banana',
});
ok(badBatch.status === 400, 'mining/batch 非法模式 → 400 整包拒', `status=${badBatch.status}`);

// ── 4) mining-batch 显式 off → 逐条回执；全部取消 ──
const batch = await call('POST', '/alpha-agent/mining/batch', {
  directions: ['入库闸门探针方向'],
  quality_gate_mode: 'off',
});
const items = batch.json?.data?.items ?? [];
ok(batch.status === 200 && items.length === 1, 'mining/batch off → 200 单条回执', `status=${batch.status}`);
const batchTaskId = items[0]?.task_id;
ok(!!batchTaskId, '回执带 task_id', String(batchTaskId || '(缺失)'));
if (batchTaskId) {
  console.log(`  · 任务行待复核：quality_gate_mode 应为 'off'，task_id=${batchTaskId}`);
  const c = await cancelTask(batchTaskId);
  ok(c.status === 200, '取消批量任务 → 200', `status=${c.status}`);
}

// ── 5) 清理审计：本探针取消的任务清单（供外部连根清任务行）──
console.log(`  · 探针任务（已取消，待清行）：${JSON.stringify(cancelled.map((c) => c.taskId))}`);

console.log(`\nRESULT ${fail === 0 ? 'ALL PASS' : `FAILURES: ${fail}`} (pass=${pass} fail=${fail})`);
process.exit(fail === 0 ? 0 : 1);
