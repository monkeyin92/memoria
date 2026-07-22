import { useEffect, useRef, useState } from "react";
import {
  ArrowCounterClockwise,
  CheckCircle,
  Cube,
  Flask,
  Snowflake,
  WarningCircle,
} from "@phosphor-icons/react";

const statusLabels = {
  draft: "草稿",
  testing: "测试中",
  approved: "已批准",
  frozen: "已冻结",
  revoked: "已撤销",
};

function sourceSummary(version) {
  return version?.manifest?.source_summary || {};
}

function sourceCount(summary, ...keys) {
  for (const key of keys) {
    const value = summary?.[key];
    if (Number.isInteger(value) && value >= 0) return value;
  }
  return 0;
}

function shortDigest(value) {
  return typeof value === "string" && value.length >= 12
    ? `${value.slice(0, 8)}…${value.slice(-4)}`
    : "未生成";
}

function shortIdentifier(value) {
  return typeof value === "string" && value
    ? `${value.slice(0, 8)}…`
    : null;
}

function dateLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function actionFor(version) {
  if (version.status === "draft") {
    return { id: "testing", label: "进入测试", icon: Flask, needsPassword: false };
  }
  if (version.status === "testing") {
    return { id: "approve", label: "批准此版本", icon: CheckCircle, needsPassword: true };
  }
  if (version.status === "approved") {
    return { id: "freeze", label: "冻结此版本", icon: Snowflake, needsPassword: true };
  }
  if (version.status === "frozen") {
    return { id: "revoke", label: "撤销此版本", icon: WarningCircle, needsPassword: true };
  }
  return {
    id: "rollback",
    label: "基于此版本创建回滚草稿",
    icon: ArrowCounterClockwise,
    needsPassword: true,
  };
}

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

export function DigitalSelfVersions({
  versions,
  busy,
  onBuild,
  onTransition,
}) {
  const [pending, setPending] = useState(null);
  const [password, setPassword] = useState("");
  const dialogRef = useRef(null);
  const passwordInputRef = useRef(null);
  const triggerRef = useRef(null);
  const restoreFocusRef = useRef(false);
  const items = Array.isArray(versions) ? versions : [];
  const closePending = () => {
    restoreFocusRef.current = true;
    setPending(null);
    setPassword("");
  };

  useEffect(() => {
    if (pending) {
      passwordInputRef.current?.focus();
      return;
    }
    if (restoreFocusRef.current) {
      restoreFocusRef.current = false;
      triggerRef.current?.focus();
    }
  }, [pending]);

  const requestAction = async (version, trigger) => {
    const action = actionFor(version);
    if (!action.needsPassword) {
      await onTransition(action.id, version, "");
      return;
    }
    triggerRef.current = trigger;
    setPending({ action, version });
    setPassword("");
  };

  const confirmAction = async () => {
    if (!pending || password.length < 8) return;
    const completed = await onTransition(
      pending.action.id,
      pending.version,
      password,
    );
    if (completed) {
      closePending();
    }
  };

  return (
    <section className="digital-section digital-self-versions" aria-labelledby="digital-self-versions-title">
      <div className="digital-section-heading">
        <span className="digital-section-icon"><Cube size={22} weight="fill" /></span>
        <div>
          <p>本人确认的不可变快照</p>
          <h2 id="digital-self-versions-title">数字分身版本</h2>
        </div>
        <span className="digital-status" data-status={items[0]?.status || "pending"}>
          {items.length ? `${items.length} 个版本` : "尚未建立"}
        </span>
      </div>

      <p className="digital-section-copy">
        版本只编译你已确认的记忆和当前已生效的人格特征；陪伴伙伴说的话不会进入。
        已批准版本才可用于未来的数字自我预览，已冻结版本才可用于未来传承。
      </p>

      <button
        type="button"
        className="button-primary digital-self-build"
        onClick={() => void onBuild()}
        disabled={Boolean(busy)}
      >
        {busy === "digital-self-build" ? "正在编译…" : "根据当前确认材料构建草稿"}
      </button>

      {!items.length && (
        <article className="digital-empty">
          <strong>先积累并确认材料</strong>
          <p>没有已确认来源时，系统会诚实拒绝生成空壳数字分身。</p>
        </article>
      )}

      <div className="digital-version-list">
        {items.map((version) => {
          const summary = sourceSummary(version);
          const memoryCount = sourceCount(
            summary,
            "memory_claim_count",
            "confirmed_memory_claim_count",
            "memory_claims",
          );
          const personaCount = sourceCount(
            summary,
            "persona_trait_count",
            "confirmed_persona_trait_count",
            "persona_traits",
          );
          const action = actionFor(version);
          const ActionIcon = action.icon;
          return (
            <article className="digital-version-card" key={version.version_id}>
              <div className="digital-version-title">
                <div>
                  <strong>版本 {version.version_number}</strong>
                  <small>{dateLabel(version.created_at)}</small>
                </div>
                <span className="digital-status" data-status={version.status}>
                  {statusLabels[version.status] || version.status}
                </span>
              </div>
              <dl className="digital-version-summary">
                <div>
                  <dt>已确认记忆</dt>
                  <dd>{memoryCount}</dd>
                </div>
                <div>
                  <dt>人格特征</dt>
                  <dd>{personaCount}</dd>
                </div>
                <div>
                  <dt>Manifest</dt>
                  <dd title={version.manifest_sha256}>{shortDigest(version.manifest_sha256)}</dd>
                </div>
              </dl>
              <p className="digital-version-parent">
                {shortIdentifier(version.parent_version_id)
                  ? `父版本：${shortIdentifier(version.parent_version_id)}`
                  : "首个版本，无父版本"}
                {shortIdentifier(version.rollback_target_version_id)
                  ? ` · 回滚自 ${shortIdentifier(version.rollback_target_version_id)}`
                  : ""}
              </p>
              <button
                type="button"
                className={action.id === "revoke" ? "button-danger" : "button-secondary"}
                onClick={(event) => void requestAction(version, event.currentTarget)}
                disabled={Boolean(busy)}
              >
                <ActionIcon size={18} weight="bold" aria-hidden="true" />
                {busy === `digital-self-${action.id}-${version.version_id}`
                  ? "正在处理…"
                  : action.label}
              </button>
            </article>
          );
        })}
      </div>

      {pending && (
        <form
          ref={dialogRef}
          className="digital-confirm"
          role="alertdialog"
          aria-label={pending.action.label}
          aria-modal="true"
          onKeyDown={(event) => {
            if (event.key === "Tab") {
              const focusable = Array.from(
                dialogRef.current?.querySelectorAll(FOCUSABLE_SELECTOR) || [],
              );
              if (focusable.length) {
                const currentIndex = focusable.indexOf(document.activeElement);
                const nextIndex = event.shiftKey
                  ? (currentIndex <= 0 ? focusable.length - 1 : currentIndex - 1)
                  : (currentIndex === focusable.length - 1 ? 0 : currentIndex + 1);
                event.preventDefault();
                focusable[nextIndex]?.focus();
              }
              return;
            }
            if (event.key === "Escape" && !busy) closePending();
          }}
          onSubmit={(event) => {
            event.preventDefault();
            void confirmAction();
          }}
        >
          <WarningCircle size={22} weight="fill" aria-hidden="true" />
          <div>
            <strong>{pending.action.label}</strong>
            <p>
              请输入当前账号密码确认。此操作只改变版本状态或创建新草稿，
              不会改写旧 manifest。
            </p>
            <label className="digital-field">
              <span>账号密码</span>
              <input
                ref={passwordInputRef}
                type="password"
                autoComplete="current-password"
                minLength={8}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            <div className="digital-confirm-actions">
              <button
                type="button"
                className="button-quiet"
                onClick={closePending}
                disabled={Boolean(busy)}
              >
                取消
              </button>
              <button
                type="submit"
                className={pending.action.id === "revoke" ? "button-danger" : "button-primary"}
                disabled={Boolean(busy) || password.length < 8}
              >
                {busy ? "正在处理…" : "确认"}
              </button>
            </div>
          </div>
        </form>
      )}
    </section>
  );
}
