// 仅供 UI 预览的合成数据，不读取仓库、数据库、供应商或主机状态。
export function demoData(name, days = 30) {
  const now = Math.floor(Date.now() / 1000);
  const GiB = 1024 ** 3;
  const account = {
    userId: "demo-owner", name, createdAt: now - 30 * 86400, isLocal: false,
    via: "session", role: "admin", title: "执炬者", earlyMemberNo: null, userNo: 1,
    capabilities: { canCustomKey: true, quotaBytes: 5 * GiB, earlyMemberNo: null, isAdmin: true },
  };
  const projects = [
    ["demo-workspace", "main", 248, 1864, 42],
    ["api-service", "develop", 136, 972, 26],
    ["agent-client", "main", 89, 615, 18],
  ].map(([repo, branch, files, chunks, size], i) => ({
    projectId: `demo-project-${i + 1}`, displayName: `${repo}@${branch}`, branch,
    createdAt: now - 20 * 86400, attachedRoot: null, diskBytes: size * 1024 ** 2,
    indexProgress: { state: "done", startedAt: now - 900 - i * 300, finishedAt: now - 880 - i * 300, processedFiles: files, totalFiles: files, error: null },
    sync: { filesIndexed: files, chunks, symbols: chunks * 2, edges: chunks * 3, pendingJobs: 0, lastIndexedAt: now - 880 - i * 300, branch, commit: "demo-preview", blobs: { count: files, bytes: size * 1024 ** 2 } },
  }));
  const runs = projects.map((project, i) => ({
    runId: i + 1, projectId: project.projectId, state: "done", startedAt: now - 900 - i * 300,
    finishedAt: now - 880 - i * 300, durationMs: 20300 + i * 1200,
    filesTotal: project.sync.filesIndexed, filesProcessed: project.sync.filesIndexed,
    chunks: project.sync.chunks, errors: 0, error: null, callId: `demo-init-${i + 1}`,
  }));
  const recent = [
    ["登录请求经过哪些校验？", "search", "high"],
    ["解释项目索引更新的流程", "ask", "high"],
    ["查找 API 请求重试逻辑", "search", "medium"],
    ["哪些测试覆盖了配置加载？", "search", "high"],
  ].map(([query, mode, confidence], i) => ({
    queryId: i + 1, projectId: projects[i % 3].projectId, mode, query,
    answerable: true, confidence, degraded: false, latencyMs: 137 + i * 41,
    evidenceCount: 4, docsCount: 1, usedTokens: 1840 + i * 160, citationCoverage: mode === "ask" ? 1 : null,
    requestId: `demo-trace-${i + 1}`, createdAt: now - 120 - i * 180,
    answerStatus: mode === "ask" ? "answered" : null,
    answerText: mode === "ask" ? "这是界面演示数据。项目索引流程依次包含文件扫描、内容解析和检索索引更新。" : null,
    evidence: [{ id: "E1", path: "src/example.ts", lines: [12, 38], tier: 1, score: 0.92, symbol: "handleRequest", group: "Core", reason: "演示证据" }],
  }));
  const usage = {
    days, total: 126, succeeded: 124, insufficient: 0, failed: 2, avgLatencyMs: 137,
    p95LatencyMs: 420, confidenceDistribution: { high: 98, medium: 26 }, citationCoverageAvg: null,
    topQueries: recent.map(item => ({ query: item.query, count: 3 })), recent,
  };
  const index = { total: 18, succeeded: 18, failed: 0, avgDurationMs: 24300, minDurationMs: 800,
    maxDurationMs: 61200, lastRunAt: now - 880, lastState: "done", recent: runs, diskBytes: 86 * 1024 ** 2 };
  const meta = {
    version: "0.0.1-demo", localMode: false, authRequired: true, registerOpen: false, needsBootstrap: false, userCount: 3,
    config: {
      embedding: { mode: "api", configured: true, missingEnv: [], model: "voyage-4-lite", provider: "Voyage AI", dim: 1024, tpm: 1000000, rpm: 2000, maxInputTokens: 32000, baseUrl: "https://embedding.example.com/v1", offline: false },
      llm: { configured: true, apiKeyConfigured: true, missingEnv: [], missingKeys: [], model: "context-model", provider: "OpenAI Compatible", maxContextTokens: 128000, baseUrl: "https://llm.example.com/v1", timeoutS: 60, maxTokens: 4096, temperature: 0.2, source: "server", protocol: "openai", protocolLabel: "OpenAI Chat Completions", supportedProtocols: ["openai", "responses", "anthropic"] },
      storage: { perProjectBytes: GiB, perUserBytes: 5 * GiB, warnRatio: 0.8, enabled: true },
    },
  };
  const quota = (usedBytes, limitBytes) => ({ usedBytes, limitBytes, ratio: usedBytes / limitBytes, status: "ok", unlimited: false });
  const overview = {
    account: { ...account, projectCount: projects.length }, index, usage, projects, days,
    storage: { status: "ok", warnRatio: 0.8, projectId: projects[0].projectId, user: quota(index.diskBytes, 5 * GiB), project: quota(projects[0].diskBytes, GiB) },
  };
  const users = [
    { ...account, quotaBytes: null, effectiveQuotaBytes: 5 * GiB, bannedAt: null, lastSeenAt: now - 60, projectCount: 3, usedBytes: index.diskBytes, usedText: "86.00 MiB", queryCount: 126 },
    { ...account, userId: "demo-beta", name: "Preview Member", role: "beta", title: "拓荒者", earlyMemberNo: 27, userNo: 2, quotaBytes: null, effectiveQuotaBytes: GiB, bannedAt: null, lastSeenAt: now - 1800, projectCount: 1, usedBytes: 12 * 1024 ** 2, usedText: "12.00 MiB", queryCount: 24 },
    { ...account, userId: "demo-public", name: "Example User", role: "public", title: "旅人", earlyMemberNo: null, userNo: 3, quotaBytes: null, effectiveQuotaBytes: GiB / 2, bannedAt: null, lastSeenAt: now - 3600, projectCount: 0, usedBytes: 0, usedText: "0 B", queryCount: 0 },
  ];
  const owners = users.map(({ userId, name, userNo, role }) => ({ userId, name, userNo, role }));
  const routes = {
    "/api/meta": meta,
    "/api/auth/me": account,
    "/api/account/overview": overview,
    "/api/projects": projects,
    "/api/usage/summary": usage,
    "/api/auth/tokens": [
      { id: "demo-key-1", name: "Laptop · Codex", prefix: "zace_demo01", createdAt: now - 7 * 86400, lastUsedAt: now - 120, isCustom: false },
      { id: "demo-key-2", name: "Workstation · Claude", prefix: "zace_demo02", createdAt: now - 3 * 86400, lastUsedAt: now - 600, isCustom: true },
    ],
    "/api/admin/users": { users, limit: 200 },
    "/api/admin/invites": {
      kinds: { A: "管理员", B: "内测", C: "公测" }, quotaByRole: { admin: 5 * GiB, beta: GiB, public: GiB / 2 },
      invites: [
        { code: "BDEMO1", kind: "B", createdBy: account.userId, createdAt: now - 86400, expiresAt: now + 7 * 86400, maxUses: 10, usedCount: 1, revokedAt: null, uses: [{ userId: "demo-beta", userName: "Preview Member", usedAt: now - 1800 }] },
        { code: "CDEMO2", kind: "C", createdBy: account.userId, createdAt: now - 3600, expiresAt: null, maxUses: 20, usedCount: 0, revokedAt: null, uses: [] },
      ],
    },
    "/api/admin/projects": {
      owners, totalBytes: index.diskBytes,
      projects: projects.map(project => ({ ...project, ownerId: account.userId, ownerName: name, ownerNo: 1, history: { total: 6, succeeded: 6, failed: 0, lastState: "done", lastRunAt: now - 880 }, lastError: null, lastErrors: 0, lastSkipped: 0 })),
    },
    "/api/admin/stats": { days, userId: null, projectCount: 3, search: usage, index, errorRate: 2 / 144, totalQueries: 126, tokens: 243680, owners },
    "/api/admin/system": {
      status: "ok（演示）", version: meta.version, dataRoot: "/demo/data", localMode: false, auth: "required",
      core: { importable: true, ok: true, modelId: "voyage-4-lite", dim: 1024 }, projects,
      host: { totalBytes: 2 * GiB, availableBytes: 1.3 * GiB, usedBytes: 0.7 * GiB, usedRatio: 0.35, availableBasis: "MemAvailable", reason: null },
    },
  };
  projects.forEach((project, i) => {
    routes[`/api/projects/${project.projectId}/index-runs`] = [runs[i]];
    routes[`/api/projects/${project.projectId}/index-stats`] = { projectId: project.projectId, current: project.indexProgress, history: index, diskBytes: project.diskBytes };
    routes[`/api/usage/projects/${project.projectId}`] = { ...usage, projectId: project.projectId, recent: recent.filter(item => item.projectId === project.projectId) };
  });
  return routes;
}
