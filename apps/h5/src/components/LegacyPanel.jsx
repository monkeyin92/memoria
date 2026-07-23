import { useEffect, useMemo, useRef, useState } from "react";
import { LockKey, UserCircle, WarningCircle, X } from "@phosphor-icons/react";

const FOCUSABLE_SELECTOR =
  'button:not([disabled]):not([tabindex="-1"]), input:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

const statusLabels = {
  pending: "待激活",
  active: "已激活",
  expired: "已过期",
  revoked: "已撤销",
};

function canEnter(grant, role) {
  if (["expired", "revoked"].includes(grant.status)) return false;
  return role === "owner" || grant.status === "active";
}

function unavailableCopy(status) {
  if (status === "revoked") return "授权已撤销，无法进入。";
  if (status === "expired") return "授权已过期，无法进入。";
  return "授权待激活，需由授权人确认后才能进入。";
}

function operationId() {
  return globalThis.crypto?.randomUUID?.() ||
    `legacy-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function relationshipId(value) {
  return value?.relationship_profile_id || value?.profile_id || value?.id || "";
}

function manifestItemRef(entry) {
  const kind = entry?.kind || entry?.entry_type;
  const idField = {
    memory_claim: "claim_id",
    persona_trait: "trait_id",
    cognitive_claim: "claim_id",
    decision_case: "case_id",
    relationship_profile: "profile_id",
  }[kind];
  const itemId = entry?.item_id || (idField ? entry?.[idField] : null);
  const sharingScope = kind === "memory_claim"
    ? entry?.sensitive_domain
    : kind === "persona_trait"
      ? null
      : entry?.sharing_scope;
  return typeof kind === "string" && typeof itemId === "string"
    ? { kind, item_id: itemId, sharing_scope: sharingScope }
    : null;
}

function ShellPreferences({ shell, busy, updatePreferences }) {
  const [responseLength, setResponseLength] = useState(
    shell.preferred_response_length,
  );
  const [questionFrequency, setQuestionFrequency] = useState(
    shell.question_frequency,
  );
  const [idempotencyKey, setIdempotencyKey] = useState(operationId);

  useEffect(() => {
    setResponseLength(shell.preferred_response_length);
    setQuestionFrequency(shell.question_frequency);
  }, [
    shell.preferred_response_length,
    shell.question_frequency,
    shell.revision,
  ]);

  const submit = (event) => {
    event.preventDefault();
    updatePreferences(shell.shell_id, {
      expected_revision: shell.revision,
      preferred_response_length: responseLength,
      question_frequency: questionFrequency,
      idempotency_key: idempotencyKey,
    });
    setIdempotencyKey(operationId());
  };

  return (
    <form className="legacy-preferences" onSubmit={submit}>
      <strong>关系外壳互动偏好</strong>
      <p>这些偏好只作用于接收人的关系外壳，不能修改冻结版本或账户主人的核心。</p>
      <div className="legacy-create-grid">
        <label>
          回答长度
          <select value={responseLength} onChange={(event) => setResponseLength(event.target.value)}>
            <option value="brief">简短</option>
            <option value="balanced">适中</option>
            <option value="detailed">详细</option>
          </select>
        </label>
        <label>
          提问频率
          <select value={questionFrequency} onChange={(event) => setQuestionFrequency(event.target.value)}>
            <option value="rare">较少</option>
            <option value="occasional">偶尔</option>
          </select>
        </label>
      </div>
      <button type="submit" className="button-quiet" disabled={Boolean(busy)}>
        保存互动偏好
      </button>
    </form>
  );
}

export function LegacyPanel({
  role = "owner",
  grants = [],
  versions = [],
  relationships = [],
  load,
  create,
  activate,
  revoke,
  startLegacy,
  updatePreferences,
  busy = "",
  onClose,
}) {
  const panelRef = useRef(null);
  const actionPasswordRef = useRef(null);
  const ownerTabRef = useRef(null);
  const granteeTabRef = useRef(null);
  const [viewRole, setViewRole] = useState(role);
  const eligibleVersions = useMemo(
    () => versions.filter((version) => version.status === "frozen"),
    [versions],
  );
  const eligibleRelationships = useMemo(
    () => relationships.filter(
      (relationship) =>
        relationship.status === "approved" &&
        relationship.step_up_verified !== false &&
        ["family", "public"].includes(relationship.sharing_scope),
    ),
    [relationships],
  );
  const [granteeUsername, setGranteeUsername] = useState("");
  const [versionId, setVersionId] = useState("");
  const [relationshipProfileId, setRelationshipProfileId] = useState("");
  const [allowedItemKeys, setAllowedItemKeys] = useState(() => new Set());
  const [voiceAllowed, setVoiceAllowed] = useState(false);
  const [expiresAt, setExpiresAt] = useState("");
  const [password, setPassword] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState(operationId);
  const [grantAction, setGrantAction] = useState(null);
  const [actionPassword, setActionPassword] = useState("");
  const [actionIdempotencyKey, setActionIdempotencyKey] = useState(operationId);

  const visibleGrants = useMemo(
    () => grants.filter((grant) => !grant.role || grant.role === viewRole),
    [grants, viewRole],
  );

  useEffect(() => setViewRole(role), [role]);

  useEffect(() => {
    if (!versionId && eligibleVersions[0]?.version_id) {
      setVersionId(eligibleVersions[0].version_id);
    }
  }, [eligibleVersions, versionId]);

  useEffect(() => {
    if (!relationshipProfileId && eligibleRelationships.length) {
      setRelationshipProfileId(relationshipId(eligibleRelationships[0]));
    }
  }, [eligibleRelationships, relationshipProfileId]);

  useEffect(() => {
    load?.(viewRole);
  }, [load, viewRole]);

  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return undefined;
    panel.querySelector(FOCUSABLE_SELECTOR)?.focus();
    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose?.();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...panel.querySelectorAll(FOCUSABLE_SELECTOR)];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    panel.addEventListener("keydown", handleKeyDown);
    return () => panel.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  useEffect(() => {
    if (grantAction) actionPasswordRef.current?.focus();
  }, [grantAction]);

  const selectedVersion = eligibleVersions.find(
    (version) => version.version_id === versionId,
  );
  const scopeItems = Array.isArray(selectedVersion?.manifest?.entries)
    ? selectedVersion.manifest.entries
      .map(manifestItemRef)
      .filter((item) => ["family", "public"].includes(item?.sharing_scope))
    : [];
  const canCreate = Boolean(
    create &&
      granteeUsername.trim() &&
      versionId &&
      relationshipProfileId &&
      allowedItemKeys.size &&
      expiresAt &&
      password.length >= 8 &&
      !busy,
  );

  const submitGrant = (event) => {
    event.preventDefault();
    if (!canCreate) return;
    const allowedItems = scopeItems
      .filter((item) => allowedItemKeys.has(`${item.kind}:${item.item_id}`))
      .map(({ kind, item_id }) => ({ kind, item_id }));
    create({
      grantee_username: granteeUsername.trim(),
      version_id: versionId,
      relationship_profile_id: relationshipProfileId,
      allowed_items: allowedItems,
      voice_allowed: voiceAllowed,
      expires_at: new Date(expiresAt).toISOString(),
      password,
      idempotency_key: idempotencyKey,
    });
    setIdempotencyKey(operationId());
  };

  const submitGrantAction = (event, grant) => {
    event.preventDefault();
    if (actionPassword.length < 8) return;
    const callback = grantAction?.type === "activate" ? activate : revoke;
    callback?.(grant.grant_id, {
      expected_grant_snapshot_sha256: grant.grant_snapshot_sha256,
      password: actionPassword,
      idempotency_key: actionIdempotencyKey,
    });
    setGrantAction(null);
    setActionPassword("");
    setActionIdempotencyKey(operationId());
  };

  const moveRoleTab = (event) => {
    const nextRole = {
      ArrowLeft: "owner",
      ArrowRight: "grantee",
      Home: "owner",
      End: "grantee",
    }[event.key];
    if (!nextRole) return;
    event.preventDefault();
    setViewRole(nextRole);
    (nextRole === "owner" ? ownerTabRef : granteeTabRef).current?.focus();
  };

  return (
    <section
      ref={panelRef}
      className="legacy-panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="legacy-panel-title"
      aria-busy={Boolean(busy)}
    >
      <header className="legacy-header">
        <div>
          <p className="eyebrow">冻结核心 · 独立关系边界</p>
          <h2 id="legacy-panel-title">传承模式</h2>
        </div>
        {onClose && (
          <button type="button" className="icon-button" aria-label="关闭传承模式" onClick={onClose}>
            <X size={22} weight="bold" aria-hidden="true" />
          </button>
        )}
      </header>

      <div className="legacy-disclosure" role="note" aria-label="数字身份披露">
        <WarningCircle size={24} weight="fill" aria-hidden="true" />
        <div>
          <strong>基于冻结资料生成的数字分身，不是本人</strong>
          <p>回答受授权版本、关系和内容范围约束；不会把新对话写回账户主人的生前核心。</p>
        </div>
      </div>

      <div className="legacy-role-tabs" role="tablist" aria-label="传承视角">
        <button
          ref={ownerTabRef}
          id="legacy-owner-tab"
          type="button"
          role="tab"
          aria-controls="legacy-owner-panel"
          aria-selected={viewRole === "owner"}
          tabIndex={viewRole === "owner" ? 0 : -1}
          onClick={() => setViewRole("owner")}
          onKeyDown={moveRoleTab}
        >
          授权人视角
        </button>
        <button
          ref={granteeTabRef}
          id="legacy-grantee-tab"
          type="button"
          role="tab"
          aria-controls="legacy-grantee-panel"
          aria-selected={viewRole === "grantee"}
          tabIndex={viewRole === "grantee" ? 0 : -1}
          onClick={() => setViewRole("grantee")}
          onKeyDown={moveRoleTab}
        >
          接收人视角
        </button>
      </div>

      <div
        id={`legacy-${viewRole}-panel`}
        role="tabpanel"
        aria-labelledby={`legacy-${viewRole}-tab`}
      >
      {viewRole === "owner" && create && (
        <form className="legacy-create" onSubmit={submitGrant}>
          <div className="legacy-create-heading">
            <LockKey size={20} weight="fill" aria-hidden="true" />
            <div><strong>新建传承授权</strong><small>只可选择冻结版本和已批准关系</small></div>
          </div>
          <label>
            接收人用户名
            <input
              value={granteeUsername}
              onChange={(event) => setGranteeUsername(event.target.value)}
              autoComplete="username"
              maxLength={128}
            />
          </label>
          <div className="legacy-create-grid">
            <label>
              冻结数字分身版本
              <select
                value={versionId}
                onChange={(event) => {
                  setVersionId(event.target.value);
                  setAllowedItemKeys(new Set());
                }}
              >
                {eligibleVersions.map((version) => (
                  <option key={version.version_id} value={version.version_id}>
                    v{version.version_number} · {version.version_id}
                  </option>
                ))}
              </select>
            </label>
            <label>
              关系画像
              <select
                value={relationshipProfileId}
                onChange={(event) => setRelationshipProfileId(event.target.value)}
              >
                {eligibleRelationships.map((relationship) => (
                  <option key={relationshipId(relationship)} value={relationshipId(relationship)}>
                    {relationship.label || relationship.salutation || relationshipId(relationship)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <fieldset className="legacy-scope">
            <legend>允许使用的冻结条目</legend>
            {scopeItems.map((item) => {
              const key = `${item.kind}:${item.item_id}`;
              return (
                <label key={key}>
                  <input
                    type="checkbox"
                    checked={allowedItemKeys.has(key)}
                    onChange={(event) => setAllowedItemKeys((current) => {
                      const next = new Set(current);
                      if (event.target.checked) next.add(key);
                      else next.delete(key);
                      return next;
                    })}
                  />
                  <span>{item.kind} · {item.item_id}</span>
                </label>
              );
            })}
            {!scopeItems.length && <p>该冻结版本没有可授权条目。</p>}
            <p className="legacy-scope-note">
              仅显示已明确标记为家庭或公开范围的关系与条目；私密或范围未知的内容不会进入授权。
            </p>
          </fieldset>
          <label className="legacy-voice-toggle">
            <input
              type="checkbox"
              checked={voiceAllowed}
              onChange={(event) => setVoiceAllowed(event.target.checked)}
            />
            <span>允许使用版本绑定的个人声音</span>
          </label>
          <div className="legacy-create-grid">
            <label>
              授权到期时间
              <input
                type="datetime-local"
                value={expiresAt}
                onChange={(event) => setExpiresAt(event.target.value)}
              />
            </label>
            <label>
              当前账号密码
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
              />
            </label>
          </div>
          <button type="submit" className="button-primary" disabled={!canCreate}>
            创建传承授权
          </button>
        </form>
      )}

      <div className="legacy-grant-list">
        {visibleGrants.map((grant) => {
          const enterable = canEnter(grant, viewRole);
          return (
            <article className="legacy-grant-card" key={grant.grant_id} data-status={grant.status}>
              <div className="legacy-grant-heading">
                <span><UserCircle size={20} weight="fill" aria-hidden="true" /></span>
                <div>
                  <strong>{viewRole === "owner" ? grant.grantee_username : "账户主人授权"}</strong>
                  <small>{statusLabels[grant.status]}</small>
                </div>
              </div>
              <dl className="legacy-grant-meta">
                <div><dt>冻结版本</dt><dd>数字分身版本 v{grant.version_number} · {grant.version_id}</dd></div>
                <div><dt>授权摘要</dt><dd>授权摘要 {grant.grant_snapshot_sha256}</dd></div>
                <div><dt>内容范围</dt><dd>授权范围 {grant.allowed_items.length} 项</dd></div>
              </dl>
              <p className="legacy-voice-boundary">
                <LockKey size={18} weight="fill" aria-hidden="true" />
                {grant.voice_allowed
                  ? "此授权允许使用版本绑定的个人声音；不可用时只回退到批准的设计音色。"
                  : "此授权不允许使用账户主人的个人声音，只能使用批准的设计音色。"}
              </p>
              {!enterable && (
                <p className="legacy-unavailable">{unavailableCopy(grant.status)}</p>
              )}
              {startLegacy && enterable && (
                <button
                  type="button"
                  className="button-primary"
                  disabled={Boolean(busy)}
                  onClick={() => startLegacy({
                    interactionMode: "legacy",
                    legacyGrantId: grant.grant_id,
                    legacyActorRole: viewRole === "owner" ? "owner_preview" : "grantee",
                  })}
                >
                  {viewRole === "owner" ? "在世预演" : "进入传承对话"}
                </button>
              )}
              {viewRole === "owner" && (
                <div className="legacy-owner-actions">
                  {grant.status === "pending" && activate && (
                    <button
                      type="button"
                      className="button-quiet"
                      disabled={Boolean(busy)}
                      onClick={() => {
                        setGrantAction({ grantId: grant.grant_id, type: "activate" });
                        setActionPassword("");
                      }}
                    >
                      激活授权
                    </button>
                  )}
                  {["pending", "active"].includes(grant.status) && revoke && (
                    <button
                      type="button"
                      className="button-danger"
                      disabled={Boolean(busy)}
                      onClick={() => {
                        setGrantAction({ grantId: grant.grant_id, type: "revoke" });
                        setActionPassword("");
                      }}
                    >
                      撤销授权
                    </button>
                  )}
                </div>
              )}
              {viewRole === "owner" && grantAction?.grantId === grant.grant_id && (
                <form
                  className="legacy-grant-confirm"
                  role="alertdialog"
                  aria-label={grantAction.type === "activate" ? "确认激活授权" : "确认撤销授权"}
                  onSubmit={(event) => submitGrantAction(event, grant)}
                >
                  <label>
                    {grantAction.type === "activate" ? "激活授权" : "撤销授权"}的账号密码
                    <input
                      ref={actionPasswordRef}
                      type="password"
                      value={actionPassword}
                      onChange={(event) => setActionPassword(event.target.value)}
                      autoComplete="current-password"
                    />
                  </label>
                  <div>
                    <button type="button" className="button-quiet" onClick={() => setGrantAction(null)}>
                      取消
                    </button>
                    <button type="submit" className={grantAction.type === "revoke" ? "button-danger" : "button-primary"} disabled={actionPassword.length < 8 || Boolean(busy)}>
                      {grantAction.type === "activate" ? "确认激活" : "确认撤销"}
                    </button>
                  </div>
                </form>
              )}
              {viewRole === "grantee" && grant.status === "active" && grant.shell && updatePreferences && (
                <ShellPreferences
                  shell={grant.shell}
                  busy={busy}
                  updatePreferences={updatePreferences}
                />
              )}
            </article>
          );
        })}
        {!visibleGrants.length && (
          <p className="legacy-empty">当前视角还没有传承授权。</p>
        )}
      </div>
      </div>
    </section>
  );
}
