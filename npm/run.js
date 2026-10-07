#!/usr/bin/env node
// nova-client 的 npm 启动器。
//
// **唯一职责**：找到本平台的二进制并把它拉起来（stdio 透传）。
//
// 二进制来自 **npm 平台子包** `nova-client-<os>-<arch>`（主包的 optionalDependencies），
// npm 自己按子包的 `os`/`cpu` 字段装本平台那一个。**没有任何网络下载步骤。**
//
// 为什么不做 GitHub 下载回退（D-48/D-49，用户 2026-09-16 明确要求删掉）：
//   ① Node 默认**不读** `https_proxy`（只认 `NODE_USE_ENV_PROXY=1`，v20+），
//      于是代理环境里包装器直连 GitHub，命中共享出口 IP 的 403 rate limit
//      （实测：同一时刻 curl 走代理 200、node 直连 403）。用户看到的是
//      「MCP server failed to start: connection closed」，极难排查；
//   ② 保留回退 = 保留一条**两条分发渠道并存**的隐路径，出错时无法判断用户拿的是
//      哪个二进制；而「npm 是唯一二进制分发渠道」是定下来的决策。
//   故：子包缺失就**显式失败并说清怎么修**，不偷偷下载、不静默降级。
//
// 唯一的本地例外是 `NOVA_CLIENT_BINARY`（开发者显式指定）与仓库内已构建产物
// ——它们是**开发期**通道，不是用户分发路径，且会打印用了哪一条。
//
// 纪律：本包装器**只往 stderr 写日志**，stdout 必须原样留给 MCP 的 JSON-RPC 帧。

"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const PACKAGE_NAME = "nova-client";
const BINARY_NAME = process.platform === "win32" ? "nova-client.exe" : "nova-client";

//: 平台子包前缀与平台表。**必须与 `scripts/make-platform-packages.py` 的 `PLATFORMS`
//: 一一对应**——`scripts/check-npm-platforms.js` 会校验这张表（含子包与 CI 矩阵）。
const PLATFORM_PACKAGE_PREFIX = "nova-client";
const PLATFORMS = [
  { platform: "linux", arch: "x64", suffix: "linux-x64" },
  { platform: "linux", arch: "arm64", suffix: "linux-arm64" },
  { platform: "darwin", arch: "x64", suffix: "darwin-x64" },
  { platform: "darwin", arch: "arm64", suffix: "darwin-arm64" },
  { platform: "win32", arch: "x64", suffix: "windows-x64" },
  { platform: "win32", arch: "arm64", suffix: "windows-arm64" },
];

function log(message) {
  console.error(`[${PACKAGE_NAME}] ${message}`);
}

/** 当前平台对应的子包名；本平台没有子包时返回 ``null``。 */
function platformPackage() {
  const hit = PLATFORMS.find(
    (item) => item.platform === process.platform && item.arch === process.arch
  );
  return hit ? `${PLATFORM_PACKAGE_PREFIX}-${hit.suffix}` : null;
}

/**
 * 在 npm 平台子包里找二进制（**唯一的分发路径**）。
 *
 * 试多个位置是因为安装布局随包管理器变化，而这些都是**同一次 npm 安装**的产物：
 * - 平级：npm 把可选依赖提升到同一 `node_modules/`；
 * - 仓库根 `node_modules/`：在仓库内跑 `node npm/run.js` 时；
 * - `require.resolve`：pnpm / 嵌套安装等非常规布局。
 */
function fromPlatformPackage() {
  const name = platformPackage();
  if (!name) return null;
  const candidates = [
    path.resolve(__dirname, "..", name, BINARY_NAME),
    path.resolve(__dirname, "node_modules", name, BINARY_NAME),
    path.resolve(__dirname, "..", "node_modules", name, BINARY_NAME),
  ];
  try {
    candidates.push(
      path.join(path.dirname(require.resolve(`${name}/package.json`)), BINARY_NAME)
    );
  } catch {
    /* 未安装：正常，由调用方报错 */
  }
  for (const candidate of candidates) {
    try {
      if (fs.statSync(candidate).isFile()) return { path: candidate, package: name };
    } catch {
      continue;
    }
  }
  return null;
}

/**
 * 开发期通道（**不是用户分发路径**，命中时会打印来源）。
 *
 * 优先级：显式 `NOVA_CLIENT_BINARY` → 仓库内 `cargo build` 的产物。
 * 刻意不含「PATH 里的 nova-client」：那会把包装器自己（npm 生成的 shim）当成二进制，
 * 造成无限自我递归（TASK-099 实测踩到）。
 */
function fromDevChannel() {
  const explicit = process.env.NOVA_CLIENT_BINARY;
  if (explicit && fs.existsSync(explicit)) {
    return { path: explicit, package: "NOVA_CLIENT_BINARY" };
  }
  const built = [
    path.resolve(__dirname, "..", "client", "target", "release", BINARY_NAME),
    path.resolve(__dirname, "..", "client", "target", "debug", BINARY_NAME),
  ];
  for (const candidate of built) {
    if (fs.existsSync(candidate)) {
      return { path: candidate, package: "local build" };
    }
  }
  return null;
}

/** 找不到二进制时的**可操作**报错（不静默降级，也不猜用户环境）。 */
function failMissingBinary() {
  const name = platformPackage();
  const target = name ? `${name}/${BINARY_NAME}` : `（本平台无对应子包）`;
  log(`找不到本平台的二进制：${target}`);
  log(`平台：${process.platform}/${process.arch}｜node：${process.version}`);
  log("");
  if (!name) {
    log("本平台没有对应的 npm 平台子包。支持：Linux/macOS/Windows × x64/arm64。");
    log("请从源码构建：git clone https://github.com/baoanaz/nova nova && cd nova/client && cargo build --release");
    log(`然后用 NOVA_CLIENT_BINARY=<path> 指定，或把二进制放到 ${BINARY_NAME} 可被找到的位置。`);
    process.exit(1);
  }
  log("原因通常是：平台子包没装上（装了可选依赖被跳过、或该版本漏发了这个平台）。");
  log("");
  log("请按顺序尝试：");
  log(`  1) 重装最新版（让 npm 重新解析可选依赖）：`);
  log(`       npx --yes --prefer-online ${PACKAGE_NAME}@latest --help`);
  log(`     或全局安装：npm i -g ${PACKAGE_NAME}@latest`);
  log(`  2) 确认子包是否存在于 registry：npm view ${name} version`);
  log(`     - 查不到 → 发布侧漏发了这个平台，请到仓库提 issue（附上面的平台信息）；`);
  log(`     - 能查到 → 本地 npm 缓存/可选依赖状态异常，删掉 node_modules 重装；`);
  log(`  3) 开发者可显式指定二进制：NOVA_CLIENT_BINARY=/abs/path/${BINARY_NAME}`);
  process.exit(1);
}

function resolveBinary() {
  const packaged = fromPlatformPackage();
  if (packaged) return packaged;
  const dev = fromDevChannel();
  if (dev) {
    log(`使用开发期二进制（${dev.package}）：${dev.path}`);
    return dev;
  }
  failMissingBinary();
  return null; // 到不了这里（failMissingBinary 会 exit）
}

function main() {
  const binary = resolveBinary().path;
  const child = spawn(binary, process.argv.slice(2), { stdio: "inherit", env: process.env });
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    process.on(signal, () => {
      if (!child.killed) child.kill(signal);
    });
  }
  child.on("error", (error) => {
    log(`启动失败：${error.message}`);
    process.exit(1);
  });
  child.on("exit", (code, signal) => {
    process.exit(signal ? 128 + (os.constants.signals[signal] || 0) : (code ?? 0));
  });
}

try {
  main();
} catch (error) {
  log(error.stack || String(error));
  process.exit(1);
}
