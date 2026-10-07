import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { randomBytes, scrypt, timingSafeEqual } from "node:crypto";
import { promisify } from "node:util";
import { join } from "node:path";
import { demoData } from "./data.mjs";

// 凭据只在部署机器的 systemd credential 中保存，前端与源码不含密码。
const credentials = JSON.parse(readFileSync(join(process.env.CREDENTIALS_DIRECTORY, "auth.json"), "utf8"));
const expectedHash = Buffer.from(credentials.hash, "hex");
if (expectedHash.length !== 64 || !credentials.name || !credentials.salt) throw new Error("演示站凭据无效");
const deriveKey = promisify(scrypt);
const sessions = new Map();
const cookieName = "nova_ui_demo";
const sessionSeconds = 12 * 60 * 60;

function reply(res, status, data, headers = {}) {
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-UI-Demo": "true",
    ...headers,
  });
  res.end(status === 204 ? undefined : JSON.stringify(data));
}

function error(res, status, code, message) {
  reply(res, status, { error: { code, message } });
}

function sessionToken(req) {
  return (req.headers.cookie || "").split(";").map(item => item.trim())
    .find(item => item.startsWith(`${cookieName}=`))?.slice(cookieName.length + 1);
}

function cookie(value, maxAge) {
  return `${cookieName}=${value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${maxAge}`;
}

async function readBody(req) {
  if (!req.headers["content-type"]?.startsWith("application/json")) throw new Error("invalid_body");
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 8192) throw new Error("invalid_body");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost");
    const path = url.pathname;
    const now = Date.now();
    for (const [key, expires] of sessions) if (expires <= now) sessions.delete(key);

    if (req.method === "GET" && path === "/healthz") {
      return reply(res, 200, { status: "ok", demo: true });
    }
    if (req.method === "GET" && path === "/api/meta") {
      return reply(res, 200, demoData(credentials.name)[path]);
    }
    if (req.method === "POST" && path === "/api/auth/login") {
      let body;
      try { body = await readBody(req); }
      catch { return error(res, 400, "invalid_body", "请提交有效的账户和密码。"); }
      if (!body || typeof body.name !== "string" || typeof body.password !== "string" || body.password.length > 200) {
        return error(res, 400, "invalid_body", "请提交有效的账户和密码。");
      }
      const hash = await deriveKey(body.password, credentials.salt, 64);
      const passwordMatches = timingSafeEqual(hash, expectedHash);
      if (body.name !== credentials.name || !passwordMatches) {
        return error(res, 401, "unauthorized", "用户名或密码不正确。");
      }
      if (sessions.size >= 1000) return error(res, 503, "busy", "演示站繁忙，请稍后再试。");
      // 同一浏览器重新登录时撤销旧会话。
      sessions.delete(sessionToken(req));
      const token = randomBytes(32).toString("hex");
      sessions.set(token, now + sessionSeconds * 1000);
      return reply(res, 200, demoData(credentials.name)["/api/auth/me"], { "Set-Cookie": cookie(token, sessionSeconds) });
    }
    if (req.method === "POST" && ["/api/auth/register", "/api/auth/bootstrap"].includes(path)) {
      return error(res, 403, "demo_read_only", "这是 UI 演示站，请使用提供的演示账户登录。");
    }
    if (req.method === "POST" && path === "/api/auth/logout") {
      sessions.delete(sessionToken(req));
      return reply(res, 204, null, { "Set-Cookie": cookie("", 0) });
    }
    if (!sessions.has(sessionToken(req))) return error(res, 401, "unauthorized", "请先登录演示账户。");
    if (req.method !== "GET") {
      return error(res, 403, "demo_read_only", "演示模式：仅预览界面，不执行保存、删除或模型请求。");
    }
    const requestedDays = Number(url.searchParams.get("days"));
    const days = [1, 7, 30, 90].includes(requestedDays) ? requestedDays : 30;
    const routes = demoData(credentials.name, days);
    if (!Object.hasOwn(routes, path)) return error(res, 404, "not_found", "演示页面没有此接口。");
    return reply(res, 200, routes[path]);
  } catch {
    // 不打印请求体或凭据。
    error(res, 500, "demo_error", "演示服务暂时不可用，请刷新重试。");
  }
});

server.requestTimeout = 15000;
server.headersTimeout = 10000;
server.listen(Number(process.env.PORT || 8789), "127.0.0.1", () => console.log("UI demo API listening on loopback"));
process.on("SIGTERM", () => server.close(() => process.exit(0)));
