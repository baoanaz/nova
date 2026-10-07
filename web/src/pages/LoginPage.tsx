import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, type Account, type DeploymentMeta, bootstrap, getMeta, login, register } from "../api/client";
import { ErrorBlock, LoadingBlock } from "../components/ui";
import "./LoginPage.css";

const logoUrl = new URL("../assets/nova-wordmark.png", import.meta.url).href;
const smallLogoUrl = new URL("../assets/nova-wordmark-small.png", import.meta.url).href;
const backgroundMp4Url = new URL("../assets/nova-background.mp4", import.meta.url).href;
const backgroundWebmUrl = new URL("../assets/nova-background.webm", import.meta.url).href;
const backgroundPosterUrl = new URL("../assets/nova-background-poster.jpg", import.meta.url).href;

const INVITE_CODE_LENGTH = 6;
type Mode = "login" | "register" | "bootstrap";

export function LoginPage({ onSignedIn }: { onSignedIn: (account: Account) => void }) {
  const navigate = useNavigate();
  const [meta, setMeta] = useState<DeploymentMeta | null>(null);
  const [mode, setMode] = useState<Mode>("login");
  const [open, setOpen] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const panel = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const [videoFailed, setVideoFailed] = useState(false);
  const [videoSource, setVideoSource] = useState(backgroundMp4Url);

  const startVideo = useCallback(() => {
    const element = video.current;
    if (!element) return;
    element.defaultMuted = true;
    element.muted = true;
    // 自动播放受限时，后续页面交互或重新可见会重试。
    void element.play()?.catch(() => undefined);
  }, []);

  useEffect(() => {
    if (videoFailed) return;
    startVideo();
    const retryOnInteraction = () => {
      if (video.current?.paused) startVideo();
    };
    const resumeVisible = () => {
      if (document.visibilityState === "visible") retryOnInteraction();
    };
    document.addEventListener("pointerdown", retryOnInteraction);
    document.addEventListener("keydown", retryOnInteraction);
    document.addEventListener("visibilitychange", resumeVisible);
    return () => {
      document.removeEventListener("pointerdown", retryOnInteraction);
      document.removeEventListener("keydown", retryOnInteraction);
      document.removeEventListener("visibilitychange", resumeVisible);
    };
  }, [startVideo, videoFailed, videoSource]);

  useEffect(() => {
    let active = true;
    void getMeta().then((resolved) => {
      if (!active) return;
      setMeta(resolved);
      setMode(resolved.needsBootstrap ? "bootstrap" : "login");
    }).catch((err: unknown) => { if (active) setError(err); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!open) return;
    const trigger = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panel.current?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
      if (event.key !== "Tab") return;
      const controls = panel.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), a[href], [tabindex="0"]',
      );
      if (!controls?.length) return;
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.current)) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleKey);
      trigger?.focus();
    };
  }, [open]);

  const submit = useCallback(async (event: FormEvent) => {
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
      const account = mode === "login" ? await login(name.trim(), password)
        : mode === "register" ? await register(name.trim(), password, inviteCode.trim())
        : await bootstrap(name.trim(), password);
      onSignedIn(account);
      navigate("/", { replace: true });
    } catch (err) { setError(err); }
    finally { setBusy(false); }
  }, [confirm, inviteCode, mode, name, onSignedIn, navigate, password]);

  const title = mode === "bootstrap" ? "初始化账户" : mode === "register" ? "注册" : "登录";
  const submitLabel = mode === "bootstrap" ? "创建并进入" : mode === "register" ? "注册并进入" : "登录";
  const switchMode = (next: Mode) => { setMode(next); setError(null); setShowPassword(false); };

  return (
    <div className="nova-landing">
      <div className="nova-landing-background" aria-hidden="true">
        {videoFailed && <img className="nova-background-media" src={backgroundPosterUrl} alt="" />}
        {!videoFailed && (
          <video
            ref={video}
            className="nova-background-media"
            src={videoSource}
            autoPlay muted loop playsInline preload="auto"
            poster={backgroundPosterUrl}
            onCanPlay={startVideo}
            onError={() => {
              if (videoSource === backgroundMp4Url) setVideoSource(backgroundWebmUrl);
              else setVideoFailed(true);
            }}
          />
        )}
      </div>
      <header className="nova-landing-nav">
        <a href={import.meta.env.BASE_URL} aria-label="NOVA 主页" className="nova-small-logo"><img src={smallLogoUrl} alt="NOVA" /></a>
        <span className="nova-nav-divider" />
        <a href="https://github.com/baoanaz/nova#readme" target="_blank" rel="noreferrer">Docs</a>
        <div className="nova-nav-end">
          <a href="https://github.com/baoanaz/nova" target="_blank" rel="noreferrer" className="nova-github">
            <svg aria-hidden="true" viewBox="0 0 24 24" width="26" height="26" fill="currentColor"><path d="M12 .8a11.2 11.2 0 0 0-3.54 21.83c.56.1.77-.24.77-.54v-2.1c-3.13.68-3.79-1.33-3.79-1.33-.51-1.3-1.25-1.65-1.25-1.65-1.02-.7.08-.68.08-.68 1.13.08 1.73 1.16 1.73 1.16 1 1.72 2.63 1.22 3.27.93.1-.73.39-1.22.71-1.5-2.5-.28-5.12-1.25-5.12-5.57 0-1.23.44-2.23 1.16-3.02-.12-.28-.5-1.43.11-2.98 0 0 .95-.3 3.08 1.16a10.7 10.7 0 0 1 5.6 0c2.14-1.46 3.08-1.16 3.08-1.16.62 1.55.23 2.7.12 2.98.72.79 1.15 1.8 1.15 3.02 0 4.33-2.63 5.28-5.13 5.56.4.35.76 1.03.76 2.08v3.1c0 .3.2.65.77.54A11.2 11.2 0 0 0 12 .8Z" /></svg>
            <span>GitHub</span>
          </a>
          <span className="nova-nav-divider" />
          <button type="button" onClick={() => setOpen(true)}>Login</button>
        </div>
      </header>
      <main className="nova-landing-hero">
        <h1><img src={logoUrl} alt="NOVA — Further Together" width="2172" height="724" /></h1>
        <p>A workspace for building and deploying<br />AI agents that actually work.</p>
        <button className="nova-primary nova-start" type="button" onClick={() => setOpen(true)}>Start now <span aria-hidden="true">›</span></button>
      </main>
      {open && (
        <div className="nova-login-overlay" onClick={(event) => { if (event.target === event.currentTarget) setOpen(false); }}>
          <div ref={panel} className="nova-login-panel" role="dialog" aria-modal="true" aria-labelledby="nova-login-title" tabIndex={-1}>
            <button className="nova-login-close" type="button" aria-label="关闭登录面板" onClick={() => setOpen(false)}>×</button>
            <div className="nova-login-content">
              <div className="nova-small-logo"><img src={smallLogoUrl} alt="NOVA" /></div>
              <h2 id="nova-login-title">{title}</h2>
              <p className="nova-login-intro">{mode === "login" ? <>欢迎回来，<br />继续构建更强大的 AI 智能体。</> : "从这里开启你的工作空间。"}</p>
              {meta === null ? (error !== null ? <ErrorBlock error={error} /> : <LoadingBlock text="正在检查部署状态…" />) : (
                <form onSubmit={submit} className="nova-login-form">
                  {mode === "bootstrap" && <p className="nova-bootstrap-note">这是首次部署：创建第一个账户后，初始化入口会自动关闭。</p>}
                  <label><span>账户</span><input name="name" autoComplete="username" placeholder="请输入邮箱或用户名" value={name} onChange={(event) => setName(event.target.value)} required /></label>
                  <label><span>密码</span><span className="nova-password-field">
                    <input name="password" type={showPassword ? "text" : "password"} autoComplete={mode === "login" ? "current-password" : "new-password"} placeholder="请输入密码" value={password} onChange={(event) => setPassword(event.target.value)} required />
                    <button type="button" aria-label={showPassword ? "隐藏密码" : "显示密码"} aria-pressed={showPassword} onClick={() => setShowPassword(!showPassword)}>
                      <svg aria-hidden="true" viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" /><circle cx="12" cy="12" r="3" />{!showPassword && <path d="m3 3 18 18" />}</svg>
                    </button>
                  </span></label>
                  {mode === "register" && <>
                    <label><span>确认密码</span><input name="confirm" type="password" autoComplete="new-password" value={confirm} onChange={(event) => setConfirm(event.target.value)} required /></label>
                    <div><label><span>邀请码</span><input name="inviteCode" autoComplete="off" placeholder="6 位邀请码" maxLength={INVITE_CODE_LENGTH} value={inviteCode} onChange={(event) => setInviteCode(event.target.value.toUpperCase())} required /></label><p className="nova-invite-hint">目前是邀请制：没有邀请码请联系管理员获取。</p></div>
                  </>}
                  {error !== null && <ErrorBlock error={error} />}
                  <button type="submit" className="nova-primary" disabled={busy || !name.trim() || !password || (mode === "register" && !inviteCode.trim())}>{busy ? "处理中…" : submitLabel}</button>
                  {mode === "login" && <p className="nova-mode-switch">还没有账号？ <button type="button" aria-label="没有账户？注册" onClick={() => switchMode("register")}>立即注册 <span aria-hidden="true">›</span></button></p>}
                  {mode === "register" && <p className="nova-mode-switch">已有账号？ <button type="button" aria-label="已有账户？登录" onClick={() => switchMode("login")}>立即登录 <span aria-hidden="true">›</span></button></p>}
                </form>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
