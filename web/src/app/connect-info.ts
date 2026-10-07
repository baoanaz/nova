/**
 * 接入片段：安装 npm 包、配置 Agent，以及可选的提示词样例。
 *
 * 事实来源（**不许凭印象写**）：
 * - 分发与配置片段：`npm/README.md` + `npm/package.json`（包名 `nova-client`，版本 `0.0.1`，
 *   bin 是 `run.js`；`npx nova-client` 已实测可用）；
 * - 参数名：`client/src/main.rs` 的 clap 定义（`--base-url` / `--token` / `--cache-root`）；
 * - 鉴权语义：`service/nova_service/auth.py`（Bearer token；API Key 前缀 `nova_`，在 UI 的
 *   「API Key」页创建）。
 *
 * 两条产品口径：
 * 1. 页面默认预填当前服务地址，用户可修改；主动清空地址时回落到占位符。
 *    API Key 不自动带入真实值，避免截图或录屏泄露；
 * 2. `--token` **永远出现**：空值只是"我还没填"，不是"没有 --token 这回事"。旧实现里
 *    token 为空就整段消失，用户因此不知道存在鉴权。
 */

/** MCP stdio 客户端包名（`npm/package.json` 的 `name`）。 */
export const CLIENT_PACKAGE = "nova-client";

/** 卡牌一的安装命令（全局安装，装完 `nova-client` 进 PATH）。 */
export const INSTALL_COMMAND = `npm install -g ${CLIENT_PACKAGE}`;

/** AGENTS.md 等指令文件的单个可选样例，控制在 100 字以内。 */
export const AGENT_PROMPT = "处理代码任务时，优先用 search_context 定位相关实现；需要跨文件解释时用 ask_project。修改前核对证据，任务转向新模块时补充检索，避免凭猜测作答。";

/** 本地索引缓存根（客户端默认）。 */
export const DEFAULT_CACHE_ROOT = "~/.cache/nova";

/** 服务地址占位符：用户可能从本机打开管理面，却要让别的机器上的 Agent 连过去。 */
export const BASE_URL_PLACEHOLDER = "http://你的服务器地址";

/** API Key 占位符（**不自动带入真实 Key**）。 */
export const TOKEN_PLACEHOLDER = "<您的 API Key>";

export type AgentId = "codex" | "claude" | "pi";

export interface AgentTarget {
  id: AgentId;
  label: string;
  /** 配置文件位置（写"哪个文件"，因为三者写法不同）。 */
  where: string;
}

/**
 * 三个按键（用户 2026-09-13 指定：只保留 Codex / Claude / pi）。
 *
 * 三个 agent 用的是**同一套 stdio 配置**，差别只在配置文件位置与包装方式——
 * 因此片段由同一函数生成，只有 `where` 不同。
 *
 * TASK-100 §需求6：删掉所有 `note`（用户 2026-09-14 要求删除
 * “[TOML 写法…]”“[用一条 CLI…]”“[pi 本身不含…]” 这类解释文字）。
 */
export const AGENT_TARGETS: AgentTarget[] = [
  {
    id: "codex",
    label: "Codex",
    where: "~/.codex/config.toml",
  },
  {
    id: "claude",
    label: "Claude",
    where: "claude mcp add-json nova --scope user '<下面的 JSON>'",
  },
  {
    id: "pi",
    label: "pi",
    where: ".mcp.json（命令行在 pi 里执行）",
  },
];

export interface SnippetContext {
  /** 服务地址；用户没填时用占位符（见卡内口径 1）。 */
  baseUrl: string;
  /** API Key；用户没填时用占位符。 */
  token?: string;
}

/** stdio 配置对象（Claude / pi / Cursor 等 JSON 形态通用）。 */
export interface StdioServer {
  command: string;
  args: string[];
}

/** 用户没填时一律回落到占位符（空串与纯空白都算没填）。 */
function orPlaceholder(value: string | undefined, fallback: string): string {
  const trimmed = value?.trim();
  return trimmed ? trimmed : fallback;
}

/**
 * stdio 配置（三按键共用的一份 args）。
 *
 * `--token` **无条件出现**：值缺省时是占位符 `<您的 API Key>`，让用户一眼看到有鉴权这回事；
 * 地址同理缺省为 `http://你的服务器地址`。
 */
export function stdioServer(ctx: SnippetContext): StdioServer {
  return {
    command: "npx",
    args: [
      CLIENT_PACKAGE,
      "--base-url",
      orPlaceholder(ctx.baseUrl, BASE_URL_PLACEHOLDER),
      "--token",
      orPlaceholder(ctx.token, TOKEN_PLACEHOLDER),
    ],
  };
}

export function stdioConfig(ctx: SnippetContext): { mcpServers: { nova: StdioServer } } {
  return { mcpServers: { nova: stdioServer(ctx) } };
}

/** Claude 的 CLI 写法（与 stdioConfig 同源，只是多了 type/stdio 包装）。 */
export function claudeCommand(ctx: SnippetContext): string {
  const server = stdioServer(ctx);
  const payload = { type: "stdio", command: server.command, args: server.args };
  return `claude mcp add-json nova --scope user '${JSON.stringify(payload)}'`;
}

/**
 * Codex 的 TOML 写法（与 stdioConfig 同源）。
 *
 * 字段名与顺序对齐用户给的样例：`[mcp_servers.nova]` → `command` → `args` → `startup_timeout_ms`。
 */
export function codexToml(ctx: SnippetContext): string {
  const server = stdioServer(ctx);
  const quoted = server.args.map((item) => `"${item}"`).join(", ");
  return [
    "[mcp_servers.nova]",
    `command = "${server.command}"`,
    `args = [${quoted}]`,
    "startup_timeout_ms = 60000",
  ].join("\n");
}

/** 按目标产出"可直接粘贴"的文本。 */
export function snippetFor(target: AgentId, ctx: SnippetContext): string {
  if (target === "codex") return codexToml(ctx);
  if (target === "claude") return claudeCommand(ctx);
  return JSON.stringify(stdioConfig(ctx), null, 2);
}
