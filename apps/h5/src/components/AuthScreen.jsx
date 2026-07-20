import { useState } from "react";
import { Eye, EyeSlash, ShieldCheck } from "@phosphor-icons/react";

export function AuthScreen({ onLogin, onRegister, preservesExistingData = false }) {
  const [mode, setMode] = useState("register");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event) => {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await (mode === "register" ? onRegister : onLogin)(username, password);
    } catch (requestError) {
      setError(requestError?.message || "暂时无法创建账号，请稍后重试。");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="mobile-prototype" data-page="account">
      <div className="app-surface">
        <section
          className="screen auth-screen"
          aria-label={mode === "register" ? "账号注册" : "账号登录"}
        >
          <div className="auth-brand" aria-hidden="true">
            <img
              src={`${import.meta.env.BASE_URL}assets/mascot-neutral.webp`}
              alt=""
            />
          </div>
          <div className="auth-copy">
            <p className="eyebrow">让每次回来都还是你</p>
            <h1>
              {mode === "register" ? "创建你的 Memoria 账号" : "欢迎回到 Memoria"}
            </h1>
            <p>
              {mode === "register"
                ? "用账号绑定聊天、记忆和声纹。浏览器关闭后，你的身份不会重新初始化。"
                : "登录后继续使用原来的聊天、记忆和声音档案。"}
            </p>
          </div>

          {preservesExistingData && mode === "register" && (
            <div className="auth-preserve-note">
              <ShieldCheck size={20} weight="fill" />
              <span>注册后会保留当前身份下已有的聊天和个人资料。</span>
            </div>
          )}

          <form className="auth-form" onSubmit={submit}>
            <label htmlFor="account-username">用户名</label>
            <input
              id="account-username"
              name="username"
              autoComplete="username"
              autoCapitalize="none"
              minLength={2}
              maxLength={32}
              required
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              aria-describedby="username-help"
            />
            <small id="username-help">2–32 个文字、数字、点、下划线或短横线</small>

            <label htmlFor="account-password">密码</label>
            <div className="password-field">
              <input
                id="account-password"
                name="password"
                type={showPassword ? "text" : "password"}
                autoComplete={mode === "register" ? "new-password" : "current-password"}
                minLength={8}
                maxLength={128}
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                aria-describedby="password-help"
              />
              <button
                type="button"
                aria-label={showPassword ? "隐藏密码" : "显示密码"}
                aria-pressed={showPassword}
                onClick={() => setShowPassword((visible) => !visible)}
              >
                {showPassword ? <EyeSlash size={21} /> : <Eye size={21} />}
              </button>
            </div>
            <small id="password-help">至少 8 个字符，请勿与其他网站共用</small>

            {error && <p className="auth-error" role="alert">{error}</p>}

            <button className="auth-submit" type="submit" disabled={submitting}>
              {submitting
                ? mode === "register" ? "正在创建…" : "正在登录…"
                : mode === "register" ? "创建账号" : "登录"}
            </button>
            <button
              className="auth-switch"
              type="button"
              disabled={submitting}
              onClick={() => {
                setError("");
                setMode((current) => current === "register" ? "login" : "register");
              }}
            >
              {mode === "register" ? "登录已有账号" : "还没有账号？去创建"}
            </button>
          </form>
        </section>
      </div>
    </main>
  );
}
