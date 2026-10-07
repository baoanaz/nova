/**
 * 接入指南：两卡牌（npm 下载 + Agent 接入）+ 片段与真实客户端参数一致（TASK-080）。
 *
 * 这组用例守的是"页面上写的能跑通"：
 * - 命令/参数名必须与 npm 包的真实 CLI 一致（`--base-url` / `--token`；包名 `zace-client`）；
 * - **`--token` 永远出现**（缺省为占位符）——旧实现里空 token 会让整段消失，
 *   用户因此不知道存在鉴权，这是本卡的核心回归点。
 */

import { describe, expect, it } from "vitest";

import {
  AGENT_TARGETS,
  BASE_URL_PLACEHOLDER,
  CLIENT_PACKAGE,
  claudeCommand,
  codexToml,
  INSTALL_COMMAND,
  snippetFor,
  stdioConfig,
  stdioServer,
  TOKEN_PLACEHOLDER,
} from "./connect-info";

const CTX = { baseUrl: "https://zace.example.com", token: "zace_abc123" };

/** 用户主动清空地址与 Key 后的上下文。 */
const EMPTY_CTX = { baseUrl: "", token: "" };

describe("卡牌一：下载客户端", () => {
  it("安装命令用的是 npm 上的真实包名", () => {
    expect(CLIENT_PACKAGE).toBe("zace-client");
    expect(INSTALL_COMMAND).toBe("npm install -g zace-client");
  });
});

describe("卡牌二：Agent 接入片段", () => {
  it("恰好三个 agent：Codex / Claude / pi（顺序不变）", () => {
    expect(AGENT_TARGETS.map((item) => item.label)).toEqual(["Codex", "Claude", "pi"]);
  });

  it("没填 Key 时仍然写出 --token（值为占位符）——本卡核心回归点", () => {
    const server = stdioServer(EMPTY_CTX);
    expect(server.command).toBe("npx");
    expect(server.args).toEqual([
      CLIENT_PACKAGE,
      "--base-url",
      BASE_URL_PLACEHOLDER,
      "--token",
      TOKEN_PLACEHOLDER,
    ]);
    // 三按键的产出里都能看到 --token（用户报的正是"页面里没有 --token 字段"）。
    for (const target of AGENT_TARGETS) {
      expect(snippetFor(target.id, EMPTY_CTX)).toContain("--token");
    }
  });

  it("没填地址时用占位符，而不是页面 origin", () => {
    expect(stdioServer(EMPTY_CTX).args).toContain(BASE_URL_PLACEHOLDER);
    expect(stdioServer(EMPTY_CTX).args.join(" ")).not.toContain(window.location.origin);
    // 纯空白也算没填。
    expect(stdioServer({ baseUrl: "   ", token: "  " }).args).toEqual(stdioServer(EMPTY_CTX).args);
  });

  it("填了地址与 Key 时占位符被真实值替换", () => {
    const server = stdioServer(CTX);
    expect(server.args).toEqual([
      CLIENT_PACKAGE,
      "--base-url",
      "https://zace.example.com",
      "--token",
      "zace_abc123",
    ]);
    expect(server.args.join(" ")).not.toContain(TOKEN_PLACEHOLDER);
    expect(server.args.join(" ")).not.toContain(BASE_URL_PLACEHOLDER);
  });

  it("三按键产出的是同一套 args，只有包装方式不同", () => {
    const expected = stdioServer(CTX).args;

    const toml = codexToml(CTX);
    expect(toml).toContain("[mcp_servers.zace]");
    expect(toml).toContain('command = "npx"');
    expect(toml).toContain("startup_timeout_ms = 60000");
    // 字段顺序与用户给的样例一致。
    expect(toml.split("\n").map((line) => line.split(" =")[0])).toEqual([
      "[mcp_servers.zace]",
      "command",
      "args",
      "startup_timeout_ms",
    ]);
    for (const arg of expected) expect(toml).toContain(`"${arg}"`);

    const pi = JSON.parse(snippetFor("pi", CTX)) as {
      mcpServers: { zace: { command: string; args: string[] } };
    };
    expect(pi.mcpServers.zace.command).toBe("npx");
    expect(pi.mcpServers.zace.args).toEqual(expected);

    const command = claudeCommand(CTX);
    expect(command.startsWith("claude mcp add-json zace --scope user '")).toBe(true);
    const payload = JSON.parse(
      command.slice(command.indexOf("{"), command.lastIndexOf("}") + 1),
    ) as { type: string; command: string; args: string[] };
    expect(payload.type).toBe("stdio");
    expect(payload.command).toBe("npx");
    expect(payload.args).toEqual(expected);
  });

  it("Codex TOML 与用户给的样例逐字对齐", () => {
    expect(codexToml({ baseUrl: "http://localhost:5174", token: "您的API Key" })).toBe(
      [
        "[mcp_servers.zace]",
        'command = "npx"',
        'args = ["zace-client", "--base-url", "http://localhost:5174", "--token", "您的API Key"]',
        "startup_timeout_ms = 60000",
      ].join("\n"),
    );
  });

  it("pi 产出标准 .mcp.json 结构", () => {
    const parsed = JSON.parse(snippetFor("pi", CTX)) as { mcpServers: { zace: unknown } };
    expect(stdioConfig(CTX).mcpServers.zace.args).toEqual(
      (parsed.mcpServers.zace as { args: string[] }).args,
    );
  });
});
