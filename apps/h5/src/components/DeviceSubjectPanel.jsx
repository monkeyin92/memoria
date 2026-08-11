import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowClockwise,
  Check,
  UsersThree,
} from "@phosphor-icons/react";

import {
  degradationFor,
  isNewerRuntimeProfile,
} from "../lib/multiSubject/runtimeProfile.js";
import { sensitiveEntriesFor } from "../lib/multiSubject/gates.js";
import { MODE_TITLES } from "../lib/multiSubject/modeMeta.js";

/**
 * 当前用户/设备管理（整改文档 §3 / §9.2 / §9.5）。
 *
 * - 展示 active subject、绑定角色与模式；调用主体解析；
 * - unknown_safe / offline / multiple-speakers 给出可解释降级；
 * - 只展示服务端 candidate / confirmation methods；app_confirm 后等待
 *   服务端返回 epoch 严格提升的新签名 profile；
 * - 切换原子性：先经 onBeforeSubjectSwitch 停止当前语音/副作用并清理
 *   generation/tool fence，再切换；旧请求、旧 profile、迟到响应由
 *   api/device.js 代次 + epoch 守卫丢弃，本组件不复制媒体状态机。
 */
function bindingRoleLabel(role) {
  return {
    account_owner: "账号持有人",
    device_admin: "设备管理员",
    primary_subject: "主要使用者",
    guardian: "监护人",
    delegate: "代理人",
    emergency_contact: "紧急联系人",
    member: "家庭成员",
  }[role] || role;
}

export function DeviceSubjectPanel({
  binding,
  profile,
  resolution,
  displayContext = {},
  sessionId,
  voiceActive,
  loading,
  error,
  onRefresh,
  onResolveSubject,
  onSwitchSubject,
  onBeforeSubjectSwitch,
  onSubjectSwitched,
  onRestartVoice,
  onCreateSession,
  onOpenBindFlow,
  onBack,
}) {
  const [switching, setSwitching] = useState(false);
  const [switchError, setSwitchError] = useState("");
  const [selectedCandidateId, setSelectedCandidateId] = useState("");
  const flowSeqRef = useRef(0);

  useEffect(() => {
    if (!resolution?.candidate_subjects?.length) return;
    setSelectedCandidateId((current) => {
      if (current) return current;
      const active = resolution.candidate_subjects.find(
        (candidate) =>
          profile?.active_subject_id &&
          candidate.person_id === profile.active_subject_id,
      );
      return active?.person_id || "";
    });
  }, [profile?.active_subject_id, resolution]);

  const refresh = useCallback(() => {
    setSwitchError("");
    return onRefresh();
  }, [onRefresh]);

  const confirmSubject = useCallback(async () => {
    const flowSeq = (flowSeqRef.current = flowSeqRef.current + 1);
    if (switching || !selectedCandidateId) return;
    const allowedMethods = resolution?.allowed_confirmation_methods || [];
    if (!allowedMethods.includes("app_confirm")) {
      setSwitchError("当前会话需要通过语音确认身份，暂时不能在应用里切换。");
      return;
    }
    if (!sessionId) {
      setSwitchError("请先开始一次语音对话，再切换当前使用者。");
      return;
    }
    if (!profile || profile.valid !== true) {
      setSwitchError("还没有可验证的当前会话 Profile，无法安全切换使用者。");
      return;
    }
    const hadActiveVoice = Boolean(voiceActive);
    setSwitching(true);
    setSwitchError("");
    try {
      // 1) 若有活跃 media 会话：先停止语音/副作用并清理 generation/tool
      //    fence；否则无需假启动音频，直接切换 authority session。
      if (hadActiveVoice && onBeforeSubjectSwitch) {
        await onBeforeSubjectSwitch();
      }
      if (flowSeq !== flowSeqRef.current) return;
      // 2) 等待服务端返回新的签名 profile。
      const nextProfile = await onSwitchSubject(selectedCandidateId);
      if (flowSeq !== flowSeqRef.current) return;
      if (nextProfile === null) {
        setSwitchError("切换结果已过期，请刷新后重试。");
        return;
      }
      if (nextProfile.valid !== true) {
        setSwitchError("服务端返回的 Runtime Profile 校验失败，切换未生效。");
        return;
      }
      // 3) session_epoch 严格前进才接受。
      if (!isNewerRuntimeProfile(profile, nextProfile)) {
        setSwitchError("服务端返回的会话版本未提升，已拒绝应用。");
        return;
      }
      // 4) 新 profile 已生效，通知上层重连。
      if (onSubjectSwitched) onSubjectSwitched(nextProfile);
      if (hadActiveVoice && onRestartVoice) {
        await onRestartVoice().catch(() => undefined);
      }
      if (flowSeq !== flowSeqRef.current) return;
      await onResolveSubject().catch(() => null);
    } catch (caught) {
      if (flowSeq === flowSeqRef.current) {
        setSwitchError(
          caught instanceof Error ? caught.message : "切换失败，请稍后重试。",
        );
      }
    } finally {
      if (flowSeq === flowSeqRef.current) setSwitching(false);
    }
  }, [
    onBeforeSubjectSwitch,
    onResolveSubject,
    onRestartVoice,
    onSubjectSwitched,
    onSwitchSubject,
    profile,
    resolution,
    selectedCandidateId,
    sessionId,
    switching,
  ]);

  const degradation = degradationFor(profile, displayContext);
  const sensitiveEntries = sensitiveEntriesFor(profile, displayContext);
  const confirmedLabel =
    profile?.active_subject_id && profile.valid === true
      ? resolution?.candidate_subjects?.find(
          (candidate) => candidate.person_id === profile.active_subject_id,
        )?.display_name || profile.active_subject_id
      : null;

  return (
    <section className="screen device-subject-screen" aria-label="设备与成员">
      <header className="topbar page-topbar">
        <button type="button" className="icon-button" aria-label="返回" onClick={onBack}>
          <ArrowLeft size={20} weight="bold" aria-hidden="true" />
        </button>
        <div>
          <p className="eyebrow">多用户与设备</p>
          <h1>设备与成员</h1>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="刷新设备状态"
          disabled={loading || switching}
          onClick={() => void refresh()}
        >
          <ArrowClockwise size={20} weight="bold" aria-hidden="true" />
        </button>
      </header>

      <div className="device-subject-body">
        {!binding && (
          <div className="device-subject-empty">
            <span className="bind-success-icon" aria-hidden="true">
              <UsersThree size={26} weight="bold" />
            </span>
            <h2>还没有绑定设备</h2>
            <p>绑定后才能在设备上区分使用人，并为敏感功能取得服务端授权。</p>
            <button
              type="button"
              className="button-primary"
              onClick={onOpenBindFlow}
            >
              开始首次绑定
            </button>
          </div>
        )}

        {binding && (
          <>
            <section className="device-binding-card" aria-labelledby="binding-title">
              <h2 id="binding-title">当前绑定</h2>
              <div className="device-summary-row">
                <span>使用模式</span>
                <strong>{MODE_TITLES[binding.declared_mode] || binding.declared_mode}</strong>
              </div>
              <div className="device-summary-row">
                <span>绑定版本</span>
                <strong>v{binding.binding_version}</strong>
              </div>
              <div className="device-summary-row">
                <span>当前使用者</span>
                <strong>
                  {confirmedLabel || (
                    <em className="device-unknown-subject">尚未确认</em>
                  )}
                </strong>
              </div>
              <div className="device-role-list" aria-label="绑定角色">
                {(binding.roles || [])
                  .filter((role) => role.status !== "revoked")
                  .map((role) => (
                    <span className="device-role-chip" key={`${role.person_id}:${role.role}`}>
                      {bindingRoleLabel(role.role)}
                    </span>
                  ))}
              </div>
            </section>

            {degradation && (
              <section className="device-degradation" aria-label="安全模式说明">
                <h2>当前处于安全模式</h2>
                {degradation.reasons.map((reason) => (
                  <p key={reason}>{reason}</p>
                ))}
                <p>
                  允许：{degradation.allowed.join("、")}；限制：
                  {degradation.restricted.join("、")}；禁止：
                  {degradation.forbidden.join("、")}。
                </p>
              </section>
            )}

            {!degradation && sensitiveEntries.length > 0 && (
              <section className="device-sensitive" aria-labelledby="sensitive-title">
                <h2 id="sensitive-title">已开放的能力入口</h2>
                {sensitiveEntries.map((entry) => (
                  <div className="device-sensitive-item" key={entry.key}>
                    <span>
                      <strong>{entry.title}</strong>
                      <small>{entry.description}</small>
                    </span>
                    <Check size={17} weight="bold" aria-hidden="true" />
                  </div>
                ))}
              </section>
            )}

            {!sessionId && (
              <button
                type="button"
                className="button-primary bind-next"
                disabled={Boolean(loading) || Boolean(switching)}
                onClick={async () => {
                  setSwitchError("");
                  try {
                    await onCreateSession();
                  } catch (caught) {
                    setSwitchError(
                      caught instanceof Error
                        ? caught.message
                        : "会话没有建立，能力保持关闭。",
                    );
                  }
                }}
              >
                创建会话并获取当前主体能力
              </button>
            )}

            {sessionId && (
              <section
                className="device-switch-card"
                aria-labelledby="switch-title"
              >
                <h2 id="switch-title">切换当前使用者</h2>
                {(resolution?.candidate_subjects || []).length > 0 ? (
                  <div className="device-candidate-list" role="radiogroup" aria-label="候选使用者">
                    {resolution.candidate_subjects.map((candidate) => (
                      <button
                        type="button"
                        role="radio"
                        key={candidate.person_id}
                        aria-checked={selectedCandidateId === candidate.person_id}
                        className="device-candidate-option"
                        disabled={switching}
                        onClick={() => setSelectedCandidateId(candidate.person_id)}
                      >
                        <span>
                          <strong>{candidate.display_name}</strong>
                          <small>
                            可信度 {Math.round((candidate.confidence || 0) * 100)}%
                          </small>
                        </span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="device-candidate-empty">
                    服务端暂时没有提供候选主体；刷新或开始语音对话后再试。
                  </p>
                )}
                {(resolution?.allowed_confirmation_methods || []).includes(
                  "app_confirm",
                ) ? (
                  <button
                    type="button"
                    className="button-primary bind-next"
                    disabled={!selectedCandidateId || switching}
                    aria-busy={switching || undefined}
                    onClick={() => void confirmSubject()}
                  >
                    {switching ? "正在停止当前对话并切换…" : "在应用中确认并切换"}
                  </button>
                ) : (
                  <p className="device-candidate-empty">
                    当前会话需要通过语音确认身份，暂时不能在应用里切换。
                  </p>
                )}
              </section>
            )}

            {switchError && <p className="inline-error" role="alert">{switchError}</p>}
            {error && <p className="inline-error" role="alert">{error}</p>}
          </>
        )}
      </div>
    </section>
  );
}
