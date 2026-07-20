import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  LockKey,
  Microphone,
  ShieldCheck,
  WarningCircle,
} from "@phosphor-icons/react";

import {
  getRawVoiceConsent,
  grantRawVoiceConsent,
  revokeRawVoiceConsent,
} from "../api.js";

function activeConsent(result) {
  return result?.consent ?? (result?.consent_grant_id ? result : null);
}

export function PrivacyDataPanel({ onBack }) {
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [consent, setConsent] = useState(undefined);
  const [confirmingRevoke, setConfirmingRevoke] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const revokeTriggerRef = useRef(null);
  const cancelRevokeRef = useRef(null);

  const loadConsent = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setConsent(activeConsent(await getRawVoiceConsent()));
    } catch {
      setConsent(undefined);
      setError("原始语音归档状态暂时无法读取，请检查网络后重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadConsent();
  }, [loadConsent]);

  useEffect(() => {
    if (confirmingRevoke) cancelRevokeRef.current?.focus();
  }, [confirmingRevoke]);

  const grant = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      setConsent(activeConsent(await grantRawVoiceConsent()));
      setAccepted(false);
      setNotice("原始语音归档已开启");
    } catch (actionError) {
      setError(actionError?.message || "原始语音归档授权没有完成，请稍后重试。");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await revokeRawVoiceConsent();
      setConsent(null);
      setConfirmingRevoke(false);
      setNotice(
        "原始语音归档授权已撤销，新增归档已停止，历史原始音频已删除。转写与结构化记忆仍保留。",
      );
    } catch (actionError) {
      setError(actionError?.message || "撤销没有完成，现有授权状态保持不变。请稍后重试。");
    } finally {
      setBusy(false);
    }
  };

  const status = loading
    ? { kind: "pending", label: "读取中" }
    : consent === undefined
      ? { kind: "failed", label: "读取失败" }
      : consent
        ? { kind: "active", label: "已开启" }
        : { kind: "pending", label: "未开启" };

  return (
    <section className="screen digital-self-screen" aria-label="隐私与数据">
      <header className="topbar digital-self-topbar">
        <button type="button" className="icon-button" aria-label="返回我的" onClick={onBack}>
          <ArrowLeft size={22} weight="bold" />
        </button>
        <div>
          <p className="eyebrow">采集范围与保存策略</p>
          <h1>隐私与数据</h1>
        </div>
      </header>

      <div className="digital-self-scroll" aria-busy={loading}>
        <article className="digital-self-intro">
          <LockKey size={24} weight="fill" aria-hidden="true" />
          <div>
            <strong>授权按用途彼此独立</strong>
            <p>原始语音归档不会替代声纹登记或声音复刻授权。</p>
          </div>
        </article>

        <section className="digital-section" aria-labelledby="raw-voice-title">
          <div className="digital-section-heading">
            <span className="digital-section-icon"><Microphone size={22} weight="fill" /></span>
            <div>
              <p>加密保存对话原声</p>
              <h2 id="raw-voice-title">原始语音归档</h2>
            </div>
            <span className="digital-status" data-status={status.kind}>
              {status.label}
            </span>
          </div>

          <div className="raw-voice-boundaries">
            <p><ShieldCheck size={18} weight="fill" />仅归档判定为账户主人的音频。</p>
            <p>访客、不确定说话人与助手音频不会归档。</p>
            <p>原始音频加密保存至账户存续期结束，仅用于个人档案回顾与可追溯复核。</p>
          </div>

          {loading ? (
            <p className="digital-empty" role="status">正在读取授权状态…</p>
          ) : consent === undefined ? (
            <p className="digital-empty">确认当前授权状态前，不会发起授权变更。</p>
          ) : consent ? (
            <>
              <div className="digital-meta-row">
                <span>保存策略</span>
                <strong>账户存续期</strong>
                <button
                  type="button"
                  className="text-danger"
                  ref={revokeTriggerRef}
                  onClick={() => setConfirmingRevoke(true)}
                  disabled={busy}
                >
                  撤销并删除原始音频
                </button>
              </div>
              {confirmingRevoke && (
                <div
                  className="digital-confirm"
                  role="alertdialog"
                  aria-labelledby="raw-voice-revoke-title"
                  aria-describedby="raw-voice-revoke-description"
                >
                  <WarningCircle size={22} weight="fill" aria-hidden="true" />
                  <div>
                    <strong id="raw-voice-revoke-title">撤销原始语音归档</strong>
                    <p id="raw-voice-revoke-description">
                      将立即停止新增归档，并删除该授权下已归档的历史原始音频。
                      转写与结构化记忆不会随原始音频一起删除。
                    </p>
                    <div className="digital-confirm-actions">
                      <button
                        type="button"
                        className="button-quiet"
                        ref={cancelRevokeRef}
                        onClick={() => {
                          setConfirmingRevoke(false);
                          revokeTriggerRef.current?.focus();
                        }}
                        disabled={busy}
                      >
                        取消
                      </button>
                      <button
                        type="button"
                        className="button-danger"
                        onClick={() => void revoke()}
                        disabled={busy}
                      >
                        {busy ? "正在撤销…" : "确认撤销并删除"}
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="digital-consent-box">
              <label>
                <input
                  type="checkbox"
                  checked={accepted}
                  onChange={(event) => setAccepted(event.target.checked)}
                />
                <span>我同意按上述范围加密保存本人的原始语音</span>
              </label>
              <button
                type="button"
                className="button-primary"
                disabled={!accepted || busy}
                onClick={() => void grant()}
              >
                {busy ? "正在开启…" : "开启原始语音归档"}
              </button>
            </div>
          )}

          {notice && <p className="digital-notice" role="status">{notice}</p>}
          {error && (
            <div className="digital-error">
              <span role="alert">{error}</span>
              {consent === undefined && !loading && (
                <button type="button" onClick={() => void loadConsent()}>
                  重新读取
                </button>
              )}
            </div>
          )}
        </section>
      </div>
    </section>
  );
}
