#!/usr/bin/env node
// npm 平台子包一致性校验（TASK-MCP-NPM），由 `scripts/check-version.sh` 调用。
//
// 为什么需要它（真实故障模式）：主包的 `optionalDependencies` 指向 6 个平台子包。
// 只要出现下面任一种不一致，**故障都是静默的**——npm 不会报错，它只是"这个可选依赖
// 解析不了，跳过"，于是那个平台的用户 `npx nova-client` 时才发现没有二进制：
//
//   - 某个子包的 `version` 与主包不同 → 该平台装不上；
//   - `optionalDependencies` 少写一个平台 → 那个平台从来没被考虑过；
//   - 某个子包目录缺 `package.json` → 发布脚本 stage 时才会失败（更晚、更贵）；
//   - 子包的 `os`/`cpu` 与 `npm/run.js` 的 `PLATFORMS` 表不符 → 包装器按 A 名字找、
//     npm 按 B 条件装，两边对不上。
//
// 退出码 0 = 一致；1 = 不一致（打印具体差异）。

"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const NPM_DIR = path.join(ROOT, "npm");

/** 唯一事实来源：`scripts/make-platform-packages.py` 的 `PLATFORMS`。 */
const EXPECTED = [
  { suffix: "linux-x64", os: "linux", cpu: "x64" },
  { suffix: "linux-arm64", os: "linux", cpu: "arm64" },
  { suffix: "darwin-x64", os: "darwin", cpu: "x64" },
  { suffix: "darwin-arm64", os: "darwin", cpu: "arm64" },
  { suffix: "windows-x64", os: "win32", cpu: "x64" },
  { suffix: "windows-arm64", os: "win32", cpu: "arm64" },
];

const expectedVersion = process.argv[2];
const problems = [];

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    problems.push(`读不到或解析失败：${path.relative(ROOT, file)}（${error.message}）`);
    return null;
  }
}

const main = readJson(path.join(NPM_DIR, "package.json"));
if (!main) {
  console.log(problems.join("\n"));
  process.exit(1);
}
if (expectedVersion && main.version !== expectedVersion) {
  problems.push(`主包版本 ${main.version} 与期望 ${expectedVersion} 不一致`);
}

const optional = main.optionalDependencies || {};
for (const spec of EXPECTED) {
  const name = `nova-client-${spec.suffix}`;
  const dir = path.join(NPM_DIR, "platforms", spec.suffix);
  if (!optional[name]) {
    problems.push(`主包 optionalDependencies 缺 ${name}`);
    continue;
  }
  if (optional[name] !== main.version) {
    problems.push(`${name} 在 optionalDependencies 里是 ${optional[name]}，主包是 ${main.version}`);
  }
  const pkg = readJson(path.join(dir, "package.json"));
  if (!pkg) continue;
  if (pkg.name !== name) problems.push(`${spec.suffix}/package.json 的 name 是 ${pkg.name}，期望 ${name}`);
  if (pkg.version !== main.version) {
    problems.push(`${name} 的 version 是 ${pkg.version}，主包是 ${main.version}`);
  }
  if (JSON.stringify(pkg.os) !== JSON.stringify([spec.os])) {
    problems.push(`${name} 的 os 是 ${JSON.stringify(pkg.os)}，期望 ${JSON.stringify([spec.os])}`);
  }
  if (JSON.stringify(pkg.cpu) !== JSON.stringify([spec.cpu])) {
    problems.push(`${name} 的 cpu 是 ${JSON.stringify(pkg.cpu)}，期望 ${JSON.stringify([spec.cpu])}`);
  }
  if (pkg.bin) {
    // 子包不该有 bin：本平台的 shim 会与主包的 bin 同名冲突。
    problems.push(`${name} 不该声明 bin（它会与主包的 nova-client 冲突）`);
  }
}

// 反向：optionalDependencies 里不能有表外的平台包。
for (const name of Object.keys(optional)) {
  if (!EXPECTED.some((spec) => `nova-client-${spec.suffix}` === name)) {
    problems.push(`主包 optionalDependencies 里有表外的包：${name}`);
  }
}

// 包装器的平台表也要能对上（run.js 按这些后缀拼子包名）。
const runJs = fs.readFileSync(path.join(NPM_DIR, "run.js"), "utf8");
for (const spec of EXPECTED) {
  if (!runJs.includes(`"${spec.suffix}"`)) {
    problems.push(`npm/run.js 的 PLATFORMS 表缺 ${spec.suffix}`);
  }
}

if (problems.length) {
  for (const problem of problems) console.log(`  - ${problem}`);
  process.exit(1);
}
process.exit(0);
