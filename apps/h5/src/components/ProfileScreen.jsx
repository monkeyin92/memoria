import { useState } from "react";
import {
  Bell,
  Brain,
  CaretRight,
  DeviceMobile,
  Fingerprint,
  ShieldCheck,
  SignOut,
  UsersThree,
  Trash,
  Waveform,
  X,
} from "@phosphor-icons/react";

import { companionById } from "../lib/companions.js";
import { AccountDeletionForm } from "./AccountDeletionForm.jsx";
import { MascotVisual } from "./Mascot.jsx";
import { PreferenceRow } from "./PreferenceRow.jsx";

export function ProfileScreen({
  profile,
  memoryDays,
  onToggle,
  preferenceSaving,
  preferenceError,
  onChangeCompanion,
  onOpenSpeakerEnrollment,
  voiceSessionActive,
  speakerEnrollmentNotice,
  onOpenDigitalSelf,
  onOpenPrivacyData,
  deviceSummary,
  sensitiveGates,
  gateMessage,
  onOpenDevicePanel,
  onOpenBindFlow,
  onLogoutCurrent,
  onLogoutAll,
  onAccountDeleted,
  accountDeletionOpen,
  setAccountDeletionOpen,
}) {
  const [accountDeletionBusy, setAccountDeletionBusy] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState("");
  const [logoutError, setLogoutError] = useState("");
  const [logoutAllConfirmationOpen, setLogoutAllConfirmationOpen] =
    useState(false);
  const companion = companionById(profile.companion_id);
  const momentCount = memoryDays.reduce(
    (total, day) => total + day.message_count,
    0,
  );
  const runLogout = async (scope) => {
    setLogoutBusy(scope);
    setLogoutError("");
    try {
      await (scope === "all" ? onLogoutAll() : onLogoutCurrent());
    } catch {
      setLogoutError("退出失败，请检查网络后重试。");
    } finally {
      setLogoutBusy("");
    }
  };
  return (
    <section
      className={`screen profile-screen ${accountDeletionOpen ? "sheet-open" : ""}`}
      aria-label="个人信息"
    >
      <header
        className="topbar page-topbar"
        inert={accountDeletionOpen ? true : undefined}
        aria-hidden={accountDeletionOpen || undefined}
      >
        <div>
          <p className="eyebrow">你的陪伴空间</p>
          <h1>我的</h1>
        </div>
      </header>

      <div
        className="profile-scroll"
        inert={accountDeletionOpen ? true : undefined}
        aria-hidden={accountDeletionOpen || undefined}
      >
        <section className="profile-hero">
          <div className="profile-avatar">
            <MascotVisual
              companionId={companion.id}
              emotion="happy"
              className="profile-mascot"
              ariaLabel={`${companion.name}陪伴机器人`}
            />
          </div>
          <div>
            <h2>{profile.display_name}</h2>
          </div>
        </section>

        <section className="stats-grid" aria-label="陪伴数据">
          <div><strong>{memoryDays.length}</strong><span>聊过的天</span></div>
          <div><strong>{momentCount}</strong><span>记住的片刻</span></div>
          <div><strong>{Math.min(memoryDays.length, 7)}</strong><span>连续陪伴</span></div>
        </section>

        <button
          type="button"
          className="profile-companion-card"
          aria-label={`更换陪伴方式，当前是${companion.name}`}
          onClick={onChangeCompanion}
        >
          <MascotVisual
            companionId={companion.id}
            emotion="happy"
            className="profile-companion-mascot"
          />
          <span>
            <small>陪伴方式</small>
            <strong>{companion.name}</strong>
            <em>{companion.tagline}</em>
          </span>
          <CaretRight size={19} weight="bold" aria-hidden="true" />
        </button>

        <section className="settings-card">
          <h3>陪伴偏好</h3>
          <PreferenceRow
            Icon={Fingerprint}
            title="过滤明显旁人（实验）"
            caption="默认过滤明显旁人；关闭后访客可聊，但仍不能访问或写入主人回顾"
            checked={profile.reject_non_owner_voice}
            onToggle={() => void onToggle("reject_non_owner_voice")}
            disabled={preferenceSaving}
          />
          <PreferenceRow
            Icon={Brain}
            title="自动生成每日回顾"
            caption="每次聊天结束后静默整理"
            checked={profile.auto_summary}
            onToggle={() => void onToggle("auto_summary")}
            disabled={preferenceSaving}
          />
          <PreferenceRow
            Icon={Waveform}
            title="语音回应"
            caption="让 Memoria 用声音陪你"
            checked={profile.voice_reply}
            onToggle={() => void onToggle("voice_reply")}
            disabled={preferenceSaving}
          />
          <PreferenceRow
            Icon={Bell}
            title="温柔提醒"
            caption="即将开放 · 在合适的时候问候你"
            checked={profile.gentle_reminders}
            disabled
          />
          {preferenceError && <p className="inline-error" role="alert">{preferenceError}</p>}
        </section>

        {sensitiveGates.speaker_enrollment ? (
          <button
            type="button"
            className="privacy-card owner-voiceprint-entry"
            disabled={voiceSessionActive}
            onClick={onOpenSpeakerEnrollment}
          >
            <span className="privacy-icon"><Fingerprint size={22} weight="fill" /></span>
            <span>
              <strong>主人声纹</strong>
              <small>
                {voiceSessionActive
                  ? "请先结束当前对话，再使用麦克风录取"
                  : "补充自然、轻声、带笑等日常说话状态"}
              </small>
            </span>
            <CaretRight size={19} weight="bold" />
          </button>
        ) : (
          <div className="privacy-card privacy-card-gated">
            <span className="privacy-icon"><Fingerprint size={22} weight="fill" /></span>
            <span>
              <strong>主人声纹</strong>
              <small>{gateMessage("speaker_enrollment")}</small>
            </span>
          </div>
        )}
        {speakerEnrollmentNotice && (
          <p className="profile-success" role="status">
            {speakerEnrollmentNotice}
          </p>
        )}

        {sensitiveGates.digital_self ? (
          <button
            type="button"
            className="privacy-card digital-self-entry"
            onClick={onOpenDigitalSelf}
          >
            <span className="privacy-icon"><Brain size={22} weight="fill" /></span>
            <span><strong>数字心智与声音</strong><small>人格学习、声纹识别与声音复刻</small></span>
            <CaretRight size={19} weight="bold" />
          </button>
        ) : (
          <div className="privacy-card privacy-card-gated">
            <span className="privacy-icon"><Brain size={22} weight="fill" /></span>
            <span>
              <strong>数字心智与声音</strong>
              <small>{gateMessage("digital_self")}</small>
            </span>
          </div>
        )}

        {sensitiveGates.raw_voice_consent ? (
          <button type="button" className="privacy-card" onClick={onOpenPrivacyData}>
            <span className="privacy-icon"><ShieldCheck size={22} weight="fill" /></span>
            <span><strong>隐私与数据</strong><small>专属凭证保护你的对话</small></span>
            <CaretRight size={19} weight="bold" />
          </button>
        ) : (
          <div className="privacy-card privacy-card-gated">
            <span className="privacy-icon"><ShieldCheck size={22} weight="fill" /></span>
            <span>
              <strong>隐私与数据</strong>
              <small>{gateMessage("raw_voice_consent")}</small>
            </span>
          </div>
        )}

        <button
          type="button"
          className="privacy-card device-entry"
          onClick={onOpenDevicePanel}
        >
          <span className="privacy-icon"><DeviceMobile size={22} weight="fill" /></span>
          <span>
            <strong>设备与成员</strong>
            <small>
              {deviceSummary
                ? deviceSummary.degraded
                  ? `${deviceSummary.modeTitle} · 安全模式中`
                  : `${deviceSummary.modeTitle} · ${
                      deviceSummary.activeSubjectLabel || "等待确认使用人"
                    }`
                : "首次绑定机器人，区分家人与使用人"}
            </small>
          </span>
          <CaretRight size={19} weight="bold" />
        </button>

        {!deviceSummary && (
          <button
            type="button"
            className="privacy-card bind-entry"
            onClick={onOpenBindFlow}
          >
            <span className="privacy-icon"><UsersThree size={22} weight="fill" /></span>
            <span>
              <strong>开始首次绑定</strong>
              <small>选择给孩子、自己、父母或家庭共同使用</small>
            </span>
            <CaretRight size={19} weight="bold" />
          </button>
        )}

        <section className="account-session-card" aria-labelledby="account-session-title">
          <h3 id="account-session-title">账户与设备</h3>
          <button
            type="button"
            className="account-logout-entry"
            disabled={Boolean(logoutBusy)}
            aria-busy={logoutBusy === "current" || undefined}
            onClick={() => void runLogout("current")}
          >
            <SignOut size={19} weight="bold" aria-hidden="true" />
            <span>
              <strong>{logoutBusy === "current" ? "正在退出…" : "退出当前设备"}</strong>
              <small>其他已登录设备保持在线</small>
            </span>
          </button>
          <button
            type="button"
            className="account-logout-entry account-logout-all"
            disabled={Boolean(logoutBusy)}
            onClick={() => {
              setLogoutError("");
              setLogoutAllConfirmationOpen(true);
            }}
          >
            <SignOut size={19} weight="bold" aria-hidden="true" />
            <span>
              <strong>退出所有设备</strong>
              <small>所有设备都需要重新登录</small>
            </span>
          </button>

          {logoutAllConfirmationOpen && (
            <div
              className="account-logout-confirmation"
              role="alertdialog"
              aria-label="确认退出所有设备"
            >
              <p>确定要结束所有设备上的登录吗？</p>
              <div>
                <button
                  type="button"
                  disabled={Boolean(logoutBusy)}
                  onClick={() => setLogoutAllConfirmationOpen(false)}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="danger"
                  disabled={Boolean(logoutBusy)}
                  aria-busy={logoutBusy === "all" || undefined}
                  onClick={() => void runLogout("all")}
                >
                  {logoutBusy === "all" ? "正在退出…" : "确认退出所有设备"}
                </button>
              </div>
            </div>
          )}
          {logoutError && (
            <p className="inline-error" role="alert">{logoutError}</p>
          )}
        </section>

        <button
          type="button"
          className="account-delete-entry"
          onClick={() => setAccountDeletionOpen(true)}
        >
          <Trash size={19} weight="bold" aria-hidden="true" />
          <span>
            <strong>注销账号</strong>
            <small>永久删除账号及全部数据</small>
          </span>
        </button>
      </div>

      {accountDeletionOpen && (
        <div className="sheet-backdrop account-delete-backdrop" role="presentation">
          <section
            className="account-delete-sheet"
            role="dialog"
            aria-modal="true"
            aria-labelledby="account-delete-title"
            onKeyDown={(event) => {
              if (event.key === "Escape" && !accountDeletionBusy) {
                setAccountDeletionOpen(false);
              }
            }}
          >
            <div className="account-delete-sheet-header">
              <div>
                <span>危险操作</span>
                <h2 id="account-delete-title">注销账号</h2>
              </div>
              <button
                type="button"
                aria-label="关闭注销账号确认"
                disabled={accountDeletionBusy}
                onClick={() => setAccountDeletionOpen(false)}
              >
                <X size={21} weight="bold" aria-hidden="true" />
              </button>
            </div>
            <AccountDeletionForm
              autoFocus
              onBusyChange={setAccountDeletionBusy}
              onDeleted={onAccountDeleted}
              submitLabel="注销账号并删除全部数据"
            />
          </section>
        </div>
      )}
    </section>
  );
}
