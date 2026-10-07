/**
 * 唯一请求出口：所有对 nova-service 的调用都在这里（Module/07 §2）。
 *
 * 两条纪律：
 * 1. **错误只有一种形态**：CF-05 信封 `{error:{code,message}}` → `ApiError`；网络/代理失败 →
 *    `ApiError("network_error", …)`。页面不需要认识 `fetch` 或 `Response`。
 * 2. **服务未启动要可操作**：Vite 代理打不通时 `fetch` 抛 `TypeError`，此时给出的是
 *    "怎么把服务起起来"的提示，而不是一个白屏或 `Failed to fetch`。
 */

import type {
  ErrorEnvelope,
  Health,
  Project,
  SearchResponse,
} from "./types";

// 页面从 `api/client` 取类型（唯一出口），避免到处 import types 的相对路径。
export type {
  AskResponse,
  EvidenceSummaryItem,
  Health,
  IndexProgress,
  PackMeta,
  Project,
  SearchResponse,
  SyncStatus,
} from "./types";

/**
 * API 基址。
 *
 * 默认空串 = 同源（生产由 Caddy 同源托管；开发由 Vite 代理）。
 * `VITE_NOVA_API_BASE` 用于两种场景：web 与服务不同源部署，以及端到端测试直连真实服务
 * （`web/src/pages/e2e.test.tsx`）。
 */
export const API_BASE: string = import.meta.env.VITE_NOVA_API_BASE ?? "";

export const DEFAULT_MAX_TOKENS = 10_000;
export const MAX_MAX_TOKENS = 20_000;
export const MAX_QUERY_CHARS = 2_000;

/** 服务未启动时的可操作提示（与 `docs/handbook/getting-started/M2a-验收手册.md` §1 的命令一致）。 */
export const SERVICE_DOWN_MESSAGE =
  "连不上 nova-service。请先启动服务（示例：uv run nova-service local --repo <你的仓库>），" +
  "或在开发模式下确认 Vite 代理的 NOVA_WEB_API 指向正确的地址。";

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }

  /** 服务端路径存在但尚未实现（`/api/auth/*`、`/api/usage/*` 的 501 占位）。 */
  static isNotImplemented(error: unknown): boolean {
    return error instanceof ApiError && error.code === "not_implemented";
  }
}

/** 把错误映射成"下一步做什么"（页面统一从这里取指引，不各自写文案）。 */
export function errorHint(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  switch (error.code) {
    case "network_error":
      return SERVICE_DOWN_MESSAGE;
    case "index_in_progress":
      return "索引尚未就绪：等索引完成后再查询（进度见项目页），或在索引完成后手动重扫。";
    case "index_failed":
      return "上次索引失败：请到项目页查看 indexProgress.error，修复后手动重扫。不要反复重试同一查询。";
    case "embedding_unavailable":
    case "embedding_unreachable":
      return "embedding provider 不可用：检查 EMBED_MODE / EMBED_MODEL / EMBED_BASE_URL / EMBED_API_KEY 配置。";
    case "project_not_found":
      return "项目不存在或未被当前用户认领：确认 projectId（可用项目页复制）后重试。";
    case "index_running":
      return "该项目的索引已在运行，无需重复触发。";
    case "invalid_root":
      return "目录不存在或不是目录：请填绝对路径。";
    case "local_mode_required":
      return "该端点仅在本地模式可用（本地模式之外的绑定请用客户端上传）。";
    case "local_root_unknown":
      return "服务重启后未重新 attach：请重新绑定本地目录。";
    case "not_implemented":
      return "该能力尚未实现（后端仍是 501 占位），依赖卡号见页面的“未就绪”说明。";
    // ---- TASK-060 鉴权 ----
    case "unauthorized":
      return "凭据无效或已失效：请重新登录（或检查 API Key 是否已被撤销）。";
    case "already_initialized":
      return "已存在账户：初始化接口已关闭，请直接登录。";
    case "name_taken":
      return "账户名已被占用：换一个名字。";
    case "invalid_password":
      return "密码不符合要求：至少 3 个字符（上限 200）。";
    case "invalid_name":
      return "账户名不能为空，且不超过 64 个字符。";
    case "local_mode":
      return "本地单用户模式没有账户与 API Key（R34）：把 NOVA_LOCAL_MODE 设为 false 才启用。";
    case "token_not_found":
      return "该 API Key 不存在或已被撤销。";
    // ---- TASK-110 邀请码与身份分级 ----
    case "invalid_invite":
      return "邀请码无效、已失效或已用尽：请确认后重试，或联系管理员获取新的邀请码。";
    case "custom_key_forbidden":
      return "自定义 API Key 是【拓荒者】特权（内测玩家与管理员可用）：可留空让服务端随机生成。";
    case "invalid_custom_key":
      return "自定义 Key 必须以 nova_ 开头，且其后至少 16 个字符（只能用字母、数字、- 与 _）。";
    case "key_taken":
      return "这个 Key 已被使用：换一个（Key 明文在库里唯一）。";
    case "quota_exceeded":
      return "索引空间已超限：请在项目页删除不再需要的项目后重试，或联系管理员调整配额。";
    case "admin_required":
      return "该功能仅管理员可用。";
    case "invalid_role":
      return "未知身份：合法值是 admin / beta / public。";
    case "invalid_quota":
      return "配额不能为负数。";
    case "invalid_admin_action":
      return "该管理员操作不被允许（例如不能封禁自己）。";
    case "invite_not_found":
      return "邀请码不存在或已失效。";
    case "invite_code_taken":
      return "邀请码已存在：换一个码面，或留空让服务端生成。";
    case "invalid_invite_kind":
      return "未知邀请码类型：A=管理员 / B=内测 / C=公测。";
    case "invalid_invite_uses":
      return "可用次数必须在 1~10000 之间。";
    case "invalid_invite_expiry":
      return "有效期必须在 1~3650 天之间（不填 = 永久）。";
    case "invalid_invite_code":
      return "邀请码必须是 6 位大写字母/数字，且首字母与类型一致。";
    // ---- TASK-062/064 统计 ----
    case "meta_db_unavailable":
      return "元数据库（nova-meta.db）未就绪：历史与用量暂时读不到。";
    default:
      return null;
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const init: RequestInit = {
    method: options.method ?? "GET",
    headers: { Accept: "application/json" },
    // 本地模式无鉴权；M2c 起 session cookie 会随同源请求自动带上（credentials 默认 same-origin）。
  };
  if (options.body !== undefined) {
    init.headers = { ...init.headers, "Content-Type": "application/json" };
    init.body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, init);
  } catch {
    throw new ApiError("network_error", SERVICE_DOWN_MESSAGE, 0);
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload: unknown = text.length > 0 ? safeJson(text) : null;

  if (!response.ok) {
    const envelope = payload as ErrorEnvelope | null;
    const code = envelope?.error?.code ?? `http_${response.status}`;
    const message = envelope?.error?.message ?? `请求失败（HTTP ${response.status}）`;
    throw new ApiError(code, message, response.status);
  }
  return payload as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

// --------------------------------------------------------------------------- 端点

export function getHealth(): Promise<Health> {
  return request<Health>("/healthz");
}

export function listProjects(): Promise<Project[]> {
  return request<Project[]>("/api/projects");
}

export function search(
  projectId: string,
  query: string,
  maxTokens: number = DEFAULT_MAX_TOKENS,
): Promise<SearchResponse> {
  return request<SearchResponse>("/api/query/search", {
    method: "POST",
    body: { projectId, query, maxTokens },
  });
}

// --------------------------------------------------------------------------- 账户（TASK-060）

/** 部署形态（`GET /api/meta`，**免鉴权**）：首屏据此决定显示登录/注册/初始化。 */
export interface DeploymentMeta {
  version: string;
  localMode: boolean;
  authRequired: boolean;
  registerOpen: boolean;
  needsBootstrap: boolean;
  userCount: number | null;
  /** TASK-088 §F：设置页的只读配置（**绝不含 key 任何部分**）。TASK-094 §B1 追加 `storage`。 */
  config: EffectiveConfig;
}

/**
 * 生效中的服务配置（设置页展示）。
 *
 * 两种详细度（服务端按"本地模式或已登录"门禁）：未鉴权时只有 `configured` / `missingEnv` / `mode`，
 * 模型名与地址等属内部拓扑，登录后才给；`apiKeyConfigured` 永远是布尔（连长度都不给）。
 */
export interface EffectiveConfig {
  embedding: EmbeddingConfigView;
  llm: LlmConfigView;
  /** TASK-094 §B1：存储配额（只读展示；`0` 表示不限）。 */
  storage?: StorageConfigView;
}

export interface EmbeddingConfigView {
  mode?: string;
  configured?: boolean;
  missingEnv?: string[];
  model?: string | null;
  provider?: string | null;
  baseUrl?: string | null;
  dim?: number | null;
  maxInputTokens?: number | null;
  offline?: boolean;
  error?: string;
  /** 速率与吞吐限额；厂商没有提供可靠静态值时为空。 */
  tpm?: number | null;
  rpm?: number | null;
}

export interface LlmConfigView {
  configured: boolean;
  apiKeyConfigured: boolean;
  missingEnv: string[];
  model?: string | null;
  baseUrl?: string | null;
  timeoutS?: number;
  maxTokens?: number;
  temperature?: number;
  /**
   * 当前生效的是哪一份配置（TASK-099 §C-4）。
   *
   * `user` = 用户自己在设置页配的那份（`ask_project` 正在用它）；`server` = 环境变量默认。
   * 设置页据此把"保存/清除"的语义说清楚，而不是让用户猜。
   */
  source?: "user" | "server";
  /** 用户配置缺哪几项（字段名，不是环境变量名；TASK-099 §C）。 */
  missingKeys?: string[];
  /** 厂商与上下文窗口（TASK-100 §需求9）。 */
  provider?: string | null;
  maxContextTokens?: number | null;
  /**
   * 当前生效的上游协议（TASK-113 / D-47）。
   *
   * `openai` = `/v1/chat/completions`（默认）；`responses` = `/v1/responses`；
   * `anthropic` = `/v1/messages`。设置页据此回填“协议”下拉框。
   */
  protocol?: LlmProtocol;
  /** 协议的中文展示名（服务端给，前端不自己拼——两处各写一份必然漂移）。 */
  protocolLabel?: string;
  /** 服务端支持的协议全集（下拉框选项的单一事实源）。 */
  supportedProtocols?: string[];
}

/**
 * 上游协议（TASK-113 / D-47）。
 *
 * 为什么需要它：同一网关的不同模型可能只开放不同协议（实测 2026-09-16：
 * `deepseek-v4-flash` 只声明 `ANTHROPIC`/`RESPONSES`），选错协议会表现为
 * “保存成功但 ask 持续 503”——本卡把它变成可见、可测、可改的显式选项。
 */
export type LlmProtocol = "openai" | "responses" | "anthropic";

/** `PUT /api/auth/llm-config` 的响应（TASK-099 §C-3；**不含 key 的任何部分**）。 */
export interface LlmConfigSaved {
  model: string;
  baseUrl: string;
  apiKeyConfigured: boolean;
  updatedAt: number;
  source: "user";
  /** 已保存的协议（TASK-113）：空串保存时服务端保留旧值，此处回填真实生效值。 */
  protocol?: LlmProtocol | null;
}

/** `POST /api/auth/llm-config/test` 的响应（TASK-113；**不含 key 的任何部分**）。 */
export interface LlmTestResult {
  /** 总判定：L1（或叠加的 L2）是否通过。 */
  ok: boolean;
  /** 面向用户的结果说明（失败时是可操作建议）。 */
  message: string;
  protocol: string;
  protocolLabel?: string;
  /** 本次请求的端点（不含凭据），便于用户核对 URL 是否写错。 */
  endpoint: string;
  /** 模型是否在上游列表里被精确找到；`null` = 未做 L1。 */
  modelFound?: boolean | null;
  /** 上游为该模型声明的协议（已归一）。 */
  supportedProtocols?: string[];
  /** 按上游声明推断的建议协议；`null` = 无法推断。 */
  suggestedProtocol?: LlmProtocol | null;
  /** 用户选的协议与上游声明不一致（前端据此提示“该模型不支持你选的协议”）。 */
  protocolMismatch?: boolean;
  /** L2 是否跑过、是否通过；`null` = 没跑。 */
  completionOk?: boolean | null;
  /** 上游报错摘要（已脱敏）。 */
  detail?: string | null;
  /** 本次实际检查了哪几级（`models` / `completion`）。 */
  checks?: string[];
}

/** 当前身份（`GET /api/auth/me`）。 */
export interface Account {
  userId: string;
  name: string;
  createdAt: number;
  isLocal: boolean;
  via: string;
  /**
   * 身份分级（TASK-110 §3.3）：`admin` / `beta` / `public`。
   *
   * **不要用它写特权判断**——一律读 `capabilities`（后端是唯一事实源；
   * 前端各写一份 `role === "beta"` 一定会与后端漂移）。它只用于“要不要显示后台入口”这类
   * 粗粒度分支。
   */
  role: "admin" | "beta" | "public";
  /** 头衔（执炬者 / 拓荒者 / 旅人）。 */
  title: string;
  /** 内测编号（仅前 100 名内测玩家非 null；展示为 `拓荒者 #0027`）。 */
  earlyMemberNo: number | null;
  /**
   * 全站注册顺序号（所有人都有；控制台展示为 `ID #001`）。
   *
   * 与 `earlyMemberNo` 并存：那个是**内测收藏品编号**（只发前 100 名），
   * 这个是**注册顺序**（永不变）。两者回答不同的问题。
   */
  userNo: number | null;
  capabilities: Capabilities;
}

/**
 * 能力位（TASK-110 §3.3）：后端算好，前端只渲染。
 *
 * 用户 2026-09-15 拍板**不设项目数上限**，因此这里没有 `projectLimit` 字段
 * （不是 null / 0——那会被误读成“限制 0 个项目”）。
 */
export interface Capabilities {
  /** 自定义 API Key（拓荒者 / 管理员特权）。 */
  canCustomKey: boolean;
  /** **该用户实际生效**的索引空间上限（字节；含后台对单人的覆盖）；`0` = 不限。 */
  quotaBytes: number;
  /** 内测编号（非内测恒为 null）。 */
  earlyMemberNo: number | null;
  /** 是否管理员（后台入口的依据）。 */
  isAdmin: boolean;
}

/** API Key 列表项（**不含明文与哈希**）。 */
export interface ApiKeySummary {
  id: string;
  name: string;
  prefix: string;
  createdAt: number;
  lastUsedAt: number | null;
  /** TASK-110 §3.4：自定义 Key（用户自选明文；拓荒者特权）。 */
  isCustom?: boolean;
}

/** 创建 API Key 的响应（`token` 明文**仅此一次**）。 */
export interface ApiKeyCreated extends ApiKeySummary {
  token: string;
}

export function getMeta(): Promise<DeploymentMeta> {
  return request<DeploymentMeta>("/api/meta");
}

export function getMe(): Promise<Account> {
  return request<Account>("/api/auth/me");
}

export function login(name: string, password: string): Promise<Account> {
  return request<Account>("/api/auth/login", { method: "POST", body: { name, password } });
}

/** 注册（TASK-110：**必须**给邀请码，否则服务端 400 `invalid_invite`）。 */
export function register(
  name: string,
  password: string,
  inviteCode: string,
): Promise<Account> {
  return request<Account>("/api/auth/register", {
    method: "POST",
    body: { name, password, inviteCode },
  });
}

/** 首个用户初始化（`users` 表为空时可用；用后自动关闭）。 */
export function bootstrap(name: string, password: string): Promise<Account> {
  return request<Account>("/api/auth/bootstrap", { method: "POST", body: { name, password } });
}

export async function logout(): Promise<void> {
  await request<void>("/api/auth/logout", { method: "POST" });
}

export function listApiKeys(): Promise<ApiKeySummary[]> {
  return request<ApiKeySummary[]>("/api/auth/tokens");
}

export function createApiKey(name: string, key = ""): Promise<ApiKeyCreated> {
  return request<ApiKeyCreated>("/api/auth/tokens", { method: "POST", body: { name, key } });
}

export function revokeApiKey(id: string): Promise<void> {
  return request<void>(`/api/auth/tokens/${encodeURIComponent(id)}`, { method: "DELETE" });
}

// --------------------------------------------------------------------------- 用户级 LLM 配置（TASK-099）

/**
 * 保存本用户的 LLM 配置（TASK-099 §C-3）。
 *
 * `apiKey` 传空串 = **保持不变**（key 从不回显，因此"留空不改"是唯一可用语义）；
 * 首次保存必须给值，否则服务端返回 400 `invalid_llm_config`。
 *
 * 响应**不含 key 的任何部分**（连长度都没有），因此调用方拿不到它去回填输入框——
 * 这是刻意的：输入框在提交后就该清空。
 */
export function saveUserLlmConfig(input: {
  model: string;
  baseUrl: string;
  apiKey?: string;
  /** 上游协议（TASK-113）；空串 = 保持不变（与 apiKey 同一语义）。 */
  protocol?: string;
}): Promise<LlmConfigSaved> {
  return request<LlmConfigSaved>("/api/auth/llm-config", {
    method: "PUT",
    body: {
      model: input.model,
      baseUrl: input.baseUrl,
      apiKey: input.apiKey ?? "",
      protocol: input.protocol ?? "",
    },
  });
}

/**
 * 连接自检（TASK-113）：L1 探测 `/v1/models`；`deep=true` 时叠加一次真实最小请求。
 *
 * 测的是**屏幕上正填的那份**（`apiKey` 留空则沿用已保存的 key）：这样“改 URL → 测试 →
 * 保存”这个最自然的顺序不会测到旧配置。**不写库、无副作用**。
 *
 * 失败是**正常结果**（`ok=false` + 可操作 `message`），不是异常；只有请求体不成立
 * （首次配置未给 key）才抛 `ApiError`（400）。
 */
export function testUserLlmConfig(input: {
  model: string;
  baseUrl: string;
  apiKey?: string;
  protocol?: string;
  deep?: boolean;
}): Promise<LlmTestResult> {
  return request<LlmTestResult>("/api/auth/llm-config/test", {
    method: "POST",
    body: {
      model: input.model,
      baseUrl: input.baseUrl,
      apiKey: input.apiKey ?? "",
      protocol: input.protocol ?? "",
      deep: input.deep ?? false,
    },
  });
}

/** 删除本用户的 LLM 配置 → 回落服务端默认（幂等：本来就没配也返回 204）。 */
export function deleteUserLlmConfig(): Promise<void> {
  return request<void>("/api/auth/llm-config", { method: "DELETE" });
}

// --------------------------------------------------------------------------- 统计与历史（TASK-062/064）

export interface IndexRun {
  runId: number;
  projectId: string;
  state: string;
  startedAt: number;
  finishedAt: number;
  durationMs: number;
  filesTotal: number;
  filesProcessed: number;
  chunks: number;
  errors: number;
  error: string | null;
  /**
   * 同一次 Tool 调用的 id（TASK-099 §B）。同值的多条 run 是**一次逻辑初始化**
   * （客户端按 1MB 分批上传，服务端每批触发一次 ingest）。
   * `null` = 旧服务端或旧客户端（无法关联，按独立记录展示）。
   */
  callId?: string | null;
}

export interface IndexStats {
  projectId?: string;
  total: number;
  succeeded: number;
  failed: number;
  avgDurationMs: number | null;
  minDurationMs?: number | null;
  maxDurationMs?: number | null;
  lastRunAt: number | null;
  lastState: string | null;
  recent: IndexRun[];
  diskBytes?: number;
}

/** 单项目索引统计：内存态当前进度 + 落库历史（两者口径不同，**不合成**）。 */
export interface ProjectIndexStats {
  projectId: string;
  current: {
    state: string;
    startedAt: number | null;
    finishedAt: number | null;
    processedFiles: number;
    totalFiles: number;
    error: string | null;
  };
  history: IndexStats;
  diskBytes: number;
}

export interface UsageRecord {
  queryId: number;
  projectId: string;
  mode: string;
  query: string;
  answerable: boolean | null;
  confidence: string | null;
  degraded: boolean;
  latencyMs: number;
  evidenceCount: number;
  docsCount: number;
  usedTokens: number;
  citationCoverage: number | null;
  /**
   * 请求 trace id（TASK-094 §C）：与响应头 `X-Request-Id`、服务端日志的 `requestId` 同源。
   * `null` = 这条记录落库时没有 trace（旧版本写下的行）——页面显示 `—`，不编造。
   * 用户报错时把它报给管理员，可到 `/api/request-log/{requestId}` 查完整链路（TASK-090）。
   */
  requestId: string | null;
  /**
   * LLM 答案正文（TASK-099 §A）。`null` = **没走 LLM**（证据不足短路 / 未配置 / 调用失败），
   * 不是"调了但答案是空"——两者靠 `answerStatus` 区分。
   */
  answerText: string | null;
  /**
   * 答案状态（TASK-099 §A）：`answered` / `insufficient_evidence` / `degraded`；
   * `null` = 非 LLM 路径（如 fast 模式的检索）。
   */
  answerStatus: string | null;
  /**
   * 证据清单（TASK-107/TASK-108）：`search_context` 的"Tool 输出"。
   *
   * 含 `id/path/lines/tier/score/symbol/group/reason`，**不含源码正文**
   * （Module/04 §8：审计不存源码内容）。`group` 与 Agent 看到的 Markdown 分组同源。
   */
  evidence: EvidenceMetaItem[];
  createdAt: number;
}

/** 审计里的单条证据元数据（`query_audit.evidence_json` 的对外形态）。 */
export interface EvidenceMetaItem {
  id?: string;
  path?: string;
  lines?: [number, number] | number[];
  tier?: number;
  score?: number;
  /** 符号名（如 `AiboxHost.start`）；fallback/模块块为 null。 */
  symbol?: string | null;
  /** 分组名：Core / Related / Tests / Docs（与 Markdown 渲染同源）。 */
  group?: string;
  /** 召回依据（如 `bm25 rank 24 + vector rank 1 + ...`）。 */
  reason?: string;
}

export interface UsageSummary {
  projectId?: string;
  days: number;
  total: number;
  succeeded: number;
  insufficient: number;
  failed: number;
  avgLatencyMs: number | null;
  p95LatencyMs: number | null;
  confidenceDistribution: Record<string, number>;
  citationCoverageAvg: number | null;
  topQueries: { query: string; count: number }[];
  recent: UsageRecord[];
}

/** 一个配额的维度（用量 / 上限 / 状态；`limitBytes=0` 表示**不限**）。 */
export interface QuotaDimensionView {
  usedBytes: number;
  limitBytes: number;
  ratio: number | null;
  status: "ok" | "warning" | "exceeded";
  unlimited: boolean;
}

/** 存储配额状态（TASK-094 §B4；`/api/account/overview` 的 `storage`）。 */
export interface QuotaStatusView {
  status: "ok" | "warning" | "exceeded";
  warnRatio: number;
  projectId: string;
  user: QuotaDimensionView;
  project: QuotaDimensionView;
}

/** 设置页展示的配额配置（`/api/meta` 的 `config.storage`，只读）。 */
export interface StorageConfigView {
  perProjectBytes: number;
  perUserBytes: number;
  warnRatio: number;
  /** 两个上限都为 0 时为 `false`（“不限”，页面只显示已用）。 */
  enabled: boolean;
}

/** 首页仪表盘（账户资料 + 索引统计 + 查询用量 + 存储配额）。 */
export interface AccountOverview {
  account: {
    name: string;
    createdAt: number;
    isLocal: boolean;
    projectCount: number;
    /** TASK-110：头衔与编号（账户页展示）。 */
    role?: "admin" | "beta" | "public" | null;
    title?: string | null;
    earlyMemberNo?: number | null;
  };
  index: IndexStats;
  usage: UsageSummary;
  projects: Project[];
  days: number;
  /** TASK-094 §B4：后端未提供时缺省（页面隐藏配额条）。 */
  storage?: QuotaStatusView;
}

export function getAccountOverview(days = 30): Promise<AccountOverview> {
  return request<AccountOverview>(`/api/account/overview?days=${days}`);
}

export function getProjectIndexStats(id: string, limit = 50): Promise<ProjectIndexStats> {
  return request<ProjectIndexStats>(
    `/api/projects/${encodeURIComponent(id)}/index-stats?limit=${limit}`,
  );
}

export function getProjectIndexRuns(id: string, limit = 50): Promise<IndexRun[]> {
  return request<IndexRun[]>(
    `/api/projects/${encodeURIComponent(id)}/index-runs?limit=${limit}`,
  );
}

export function getUsageSummary(days = 30, limit = 50): Promise<UsageSummary> {
  return request<UsageSummary>(`/api/usage/summary?days=${days}&limit=${limit}`);
}

export function getProjectUsage(id: string, days = 30): Promise<UsageSummary> {
  return request<UsageSummary>(
    `/api/usage/projects/${encodeURIComponent(id)}?days=${days}`,
  );
}

/**
 * 删除项目（TASK-094 §D）：级联删除该项目的**索引数据**（含向量与同步账本）。
 *
 * 后端已就绪（`DELETE /api/projects/{id}`，含 TASK-061 归属校验：他人项目同样 404）。
 * 返回 204（无内容），因此调用方不需要处理响应体。
 */
export function deleteProject(id: string): Promise<void> {
  return request<void>(`/api/projects/${encodeURIComponent(id)}`, { method: "DELETE" });
}

// --------------------------------------------------------------------------- 管理员后台（TASK-110）

/**
 * 后台用户（`GET /api/admin/users` 的列表项）。
 *
 * 全部字段都是**服务端已算好的聚合值**（不在这里把 `usedBytes` 换算成 MB 再显示：
 * 换算单位同时给 `usedText`，两处各算一次必然在舍入上漂移）。
 */
export interface AdminUser {
  userId: string;
  name: string;
  createdAt: number;
  isLocal: boolean;
  role: "admin" | "beta" | "public";
  title: string;
  earlyMemberNo: number | null;
  /** 全站注册顺序号（所有人都有）。 */
  userNo?: number | null;
  /** 后台对该用户的**单人覆盖**（null = 按角色默认）。 */
  quotaBytes: number | null;
  /** 实际生效的上限（含角色默认与单人覆盖）。 */
  effectiveQuotaBytes: number;
  bannedAt: number | null;
  lastSeenAt: number | null;
  projectCount: number;
  usedBytes: number;
  usedText: string;
  queryCount: number;
}

/** 邀请码使用记录（谁在什么时候用了它）。 */
export interface InviteUse {
  userId: string;
  userName: string | null;
  usedAt: number;
}

/** 邀请码（`GET /api/admin/invites`）。 */
export interface Invite {
  code: string;
  kind: string;
  createdBy: string | null;
  createdAt: number;
  expiresAt: number | null;
  maxUses: number;
  usedCount: number;
  revokedAt: number | null;
  uses?: InviteUse[];
}

/** `indexProgress` 六字段（TASK-034；口径见 M2a 手册 §2）。 */
export interface IndexProgressView {
  state: string;
  startedAt: number | null;
  finishedAt: number | null;
  processedFiles: number;
  totalFiles: number;
  error: string | null;
}

/** 后台项目行（含索引健康；`/api/admin/projects`）。 */
export interface AdminProject {
  projectId: string;
  displayName: string;
  attachedRoot: string | null;
  ownerId: string | null;
  /** 归属人展示名（用户要求“显示名称”）；未认领为 null。 */
  ownerName: string | null;
  ownerNo: number | null;
  diskBytes: number;
  indexProgress: IndexProgressView | null;
  history: {
    total: number;
    succeeded: number;
    failed: number;
    lastState: string | null;
    lastRunAt: number | null;
  };
  lastError: string | null;
  lastErrors: number;
  lastSkipped: number | null;
}

/** 后台统计（`GET /api/admin/stats`）。 */
export interface AdminStats {
  days: number;
  /** 查询范围：`null` = 全站；否则是某个用户。 */
  userId: string | null;
  projectCount: number;
  search: UsageSummary;
  index: IndexStats;
  /** 无调用时为 `null`（“没有调用”不是“错误率 0%”）。 */
  errorRate: number | null;
  totalQueries: number;
  tokens: number;
  /** 用户下拉选项（全站查询用）。 */
  owners: AdminOwnerOption[];
}

/** 后台“按用户筛选”的下拉选项。 */
export interface AdminOwnerOption {
  userId: string;
  name: string;
  userNo: number | null;
  role: "admin" | "beta" | "public";
}

/** VPS 主机内存（`/api/admin/system` 的 `host`）。 */
export interface HostMemory {
  totalBytes: number | null;
  availableBytes: number | null;
  usedBytes: number | null;
  usedRatio: number | null;
  /** `MemAvailable`（含可回收缓存）或 `MemFree`（退让口径）。 */
  availableBasis: "MemAvailable" | "MemFree" | null;
  /** 读不到时的原因（非 Linux 等）；成功时为 null。 */
  reason: string | null;
}

export function listAdminUsers(): Promise<{ users: AdminUser[]; limit: number }> {
  return request<{ users: AdminUser[]; limit: number }>("/api/admin/users");
}

/** 后台改用户（**只改给到的字段**；`quotaBytes: null` = 恢复按角色默认）。 */
export function patchAdminUser(
  userId: string,
  patch: { role?: string; quotaBytes?: number | null; banned?: boolean },
): Promise<AdminUser> {
  return request<AdminUser>(`/api/admin/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    body: patch,
  });
}

export function listAdminInvites(): Promise<{
  invites: Invite[];
  kinds: Record<string, string>;
  quotaByRole: Record<string, number>;
}> {
  return request<{
    invites: Invite[];
    kinds: Record<string, string>;
    quotaByRole: Record<string, number>;
  }>("/api/admin/invites");
}

export function createAdminInvite(input: {
  kind: string;
  maxUses: number;
  expiresInDays?: number | null;
  code?: string;
}): Promise<Invite> {
  return request<Invite>("/api/admin/invites", {
    method: "POST",
    body: {
      kind: input.kind,
      maxUses: input.maxUses,
      expiresInDays: input.expiresInDays ?? null,
      code: input.code ?? "",
    },
  });
}

export function revokeAdminInvite(code: string): Promise<void> {
  return request<void>(`/api/admin/invites/${encodeURIComponent(code)}`, { method: "DELETE" });
}

export function listAdminProjects(
  userId?: string | null,
): Promise<{ projects: AdminProject[]; totalBytes: number; owners: AdminOwnerOption[] }> {
  const query = userId ? `?userId=${encodeURIComponent(userId)}` : "";
  return request<{ projects: AdminProject[]; totalBytes: number; owners: AdminOwnerOption[] }>(
    `/api/admin/projects${query}`,
  );
}

/** 管理员删除任意项目（级联删除索引数据；不可恢复）。 */
export function deleteAdminProject(projectId: string): Promise<void> {
  return request<void>(`/api/admin/projects/${encodeURIComponent(projectId)}`, {
    method: "DELETE",
  });
}

export function getAdminStats(days = 30, userId?: string | null): Promise<AdminStats> {
  const scope = userId ? `&userId=${encodeURIComponent(userId)}` : "";
  return request<AdminStats>(`/api/admin/stats?days=${days}${scope}`);
}

export function getAdminSystem(): Promise<Health> {
  return request<Health>("/api/admin/system");
}
