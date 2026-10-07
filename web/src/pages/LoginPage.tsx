/**
 * 登录 / 注册 / 初始化（TASK-071；首屏页面）。TASK-098 §B 改版为**左装饰 + 右白卡片**。
 *
 * 一个页面承担三件事，由 `GET /api/meta` 决定显示哪个：
 *
 * | 部署状态 | 显示 |
 * |---|---|
 * | 本地模式（默认） | 不需要登录，直接进入（避免"强制登录但没有账户体系"的死局） |
 * | 云端 + 无账户 | **初始化账户**（users 为空，register 默认关闭时这是唯一入口） |
 * | 云端 + 有账户 | **登录**（下方可切换到注册表单） |
 *
 * 都不需要用户去记"我该点哪个"。
 *
 * 视觉：暖白与珊瑚橙、方角边框、偏移阴影；左侧品牌区、右侧表单。
 * 三种模式、字段、错误展示、忙态禁用保持一致；窄屏收起装饰区。
 *
 * TASK-110 §1.1：注册表单追加**邀请码**输入（注册**必须**有码）。它是注册模式的专属字段——
 * 登录与初始化不需要它（那两条路径面对的是已有/首个账户）。
 */

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  ApiError,
  type Account,
  type DeploymentMeta,
  bootstrap,
  getMeta,
  login,
  register,
} from "../api/client";
import { ErrorBlock, LoadingBlock } from "../components/ui";
import { Brand } from "../components/Brand";
import { Icon } from "../components/Icon";
import { site } from "../../site.config";

/** 邀请码长度（与后端冻结的 6 位一致；前端只做输入体验，校验仍以后端为准）。 */
const INVITE_CODE_LENGTH = 6;

type Mode = "login" | "register" | "bootstrap";

export function LoginPage({ onSignedIn }: { onSignedIn: (account: Account) => void }) {
  const navigate = useNavigate();
  const [meta, setMeta] = useState<DeploymentMeta | null>(null);
  const [mode, setMode] = useState<Mode>("login");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  //: 邀请码（TASK-110；仅注册模式使用）。大写化在输入时就做——码面只有大写字母/数字，
  //: 而手机输入法默认首字母大写、从聊天软件粘贴也可能带小写，在这里统一比让用户自己改成大写好。
  const [inviteCode, setInviteCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    void (async () => {
      try {
        const resolved = await getMeta();
        setMeta(resolved);
        setMode(resolved.needsBootstrap ? "bootstrap" : "login");
      } catch (err) {
        setError(err);
      }
    })();
  }, []);

  const submit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      if (mode === "register" && password !== confirm) {
        setError(new ApiError("password_mismatch", "两次输入的密码不一致", 400));
        return;
      }
      if (mode === "register" && inviteCode.trim().length === 0) {
        setError(new ApiError("invalid_invite", "注册需要邀请码：请填入收到的邀请码", 400));
        return;
      }
      setBusy(true);
      setError(null);
      try {
        const account =
          mode === "login"
            ? await login(name.trim(), password)
            : mode === "register"
              ? await register(name.trim(), password, inviteCode.trim())
              : await bootstrap(name.trim(), password);
        onSignedIn(account);
        navigate("/", { replace: true });
      } catch (err) {
        setError(err);
      } finally {
        setBusy(false);
      }
    },
    [confirm, inviteCode, mode, name, onSignedIn, navigate, password],
  );

  if (error !== null && meta === null) return <ErrorBlock error={error} />;
  if (meta === null) return <LoadingBlock text="正在检查部署状态…" />;

  const title =
    mode === "bootstrap" ? "初始化账户" : mode === "register" ? "注册" : "登录";
  const submitLabel =
    mode === "bootstrap" ? "创建并进入" : mode === "register" ? "注册并进入" : "登录";

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      {/* 品牌介绍区：点阵背景与代码节点装饰，窄屏收起以保留表单空间。 */}
      <section
        aria-hidden="true"
        className="login-art relative hidden overflow-hidden border-r-2 border-ink-line md:flex md:w-1/2 md:flex-col md:justify-between md:p-10 lg:w-auto lg:flex-1 lg:p-16"
      >
        <div className="relative z-10"><Brand /></div>

        <div className="relative z-10 max-w-md">
          {/* 装饰性大标题：用 p 而不是 h1——真正的 h1 是表单标题（屏幕阅读器只该读一个）。 */}
          <p className="text-4xl font-bold leading-tight tracking-tight text-ink-primary lg:text-5xl">
            {site.tagline}
          </p>
          <p className="mt-5 text-sm leading-relaxed text-ink-muted">
            把仓库索引成可检索的上下文，让编码 Agent 在你的代码里找到依据，
            <br />
            而不是靠猜。
          </p>
          <div className="context-motif" aria-hidden="true">
            <span><Icon name="folder" className="h-7 w-7" /></span>
            <span><Icon name="cpu" className="h-7 w-7" /></span>
            <span><Icon name="connect" className="h-7 w-7" /></span>
          </div>
        </div>

        <p className="relative z-10 text-xs tracking-wider text-ink-muted">{site.tagline}</p>
      </section>

      {/* 登录、注册与初始化共用同一表单卡片。 */}
      <section className="flex w-full items-center justify-center px-4 py-10 md:w-1/2 md:px-6 lg:w-[28%] lg:min-w-[22rem] lg:shrink-0">
        <div className="w-full max-w-md">
          {/* 窄屏下装饰区收起，品牌字改在卡片上方显示。 */}
          <div className="mb-8 text-center md:hidden"><Brand /></div>

          <form
            onSubmit={submit}
            className="space-y-5 border-2 border-ink-line bg-paper-card p-6 shadow-xl"
          >
            <h1 className="border-b border-ink-line pb-5 text-2xl font-bold tracking-tight text-ink-primary">{title}</h1>

            {mode === "bootstrap" && (
              <p className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                这是首次部署：创建第一个账户后，初始化入口会自动关闭。
              </p>
            )}

            <label className="block text-sm">
              <span className="mb-1 block text-xs text-ink-muted">账户</span>
              <input
                name="name"
                autoComplete="username"
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="w-full rounded border border-ink-line bg-paper-card px-3 py-2 text-sm text-ink-primary"
                required
              />
            </label>

            <label className="block text-sm">
              <span className="mb-1 block text-xs text-ink-muted">密码</span>
              <input
                name="password"
                type="password"
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="w-full rounded border border-ink-line bg-paper-card px-3 py-2 text-sm text-ink-primary"
                required
              />
            </label>

            {mode === "register" && (
              <label className="block text-sm">
                <span className="mb-1 block text-xs text-ink-muted">确认密码</span>
                <input
                  name="confirm"
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(event) => setConfirm(event.target.value)}
                  className="w-full rounded border border-ink-line bg-paper-card px-3 py-2 text-sm text-ink-primary"
                  required
                />
              </label>
            )}

            {/*
              TASK-110（2026-09-15 用户要求）：邀请码排在**最后**（账户 → 密码 → 确认密码 → 邀请码）。
              之前它夹在密码与确认密码之间，读起来像“密码的一部分”。
            */}
            {mode === "register" && (
              <div className="block text-sm">
                <label className="block">
                  <span className="mb-1 block text-xs text-ink-muted">邀请码</span>
                  <input
                    name="inviteCode"
                    autoComplete="off"
                    value={inviteCode}
                    onChange={(event) => setInviteCode(event.target.value.toUpperCase())}
                    placeholder="6 位邀请码"
                    maxLength={INVITE_CODE_LENGTH}
                    className="w-full rounded border border-ink-line bg-paper-card px-3 py-2 font-mono text-sm tracking-widest text-ink-primary"
                    required
                  />
                </label>
                {/* 提示放在 label **之外**：放进 label 会把它拼进可访问名（屏幕阅读器与测试都
                    会把“邀请码”读成一整句话），而它本来只是辅助说明。 */}
                <span className="mt-1 block text-xs text-ink-muted">
                  目前是邀请制：没有邀请码请联系管理员获取。
                </span>
              </div>
            )}

            {error !== null && <ErrorBlock error={error} />}

            <button
              type="submit"
              disabled={
                busy ||
                name.trim().length === 0 ||
                password.length === 0 ||
                (mode === "register" && inviteCode.trim().length === 0)
              }
              className="ui-button w-full border border-accent-seal bg-accent-seal px-4 py-3 text-sm font-semibold text-white disabled:opacity-40"
            >
              {busy ? "处理中…" : submitLabel}
            </button>

            <div className="space-y-1 border-t border-ink-line pt-3 text-center text-xs">
              {mode === "login" && (
                <button
                  type="button"
                  className="text-accent-seal underline"
                  onClick={() => setMode("register")}
                >
                  没有账户？注册
                </button>
              )}
              {mode === "register" && (
                <button
                  type="button"
                  className="text-accent-seal underline"
                  onClick={() => setMode("login")}
                >
                  已有账户？登录
                </button>
              )}
            </div>
          </form>

          <p className="mt-5 text-center text-xs text-ink-muted">{site.name} v{meta.version}</p>
        </div>
      </section>
    </div>
  );
}
