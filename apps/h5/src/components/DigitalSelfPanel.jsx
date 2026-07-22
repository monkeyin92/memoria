import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  Brain,
  CheckCircle,
  ClockCounterClockwise,
  DownloadSimple,
  Fingerprint,
  LockKey,
  Play,
  SpeakerHigh,
  Trash,
  UploadSimple,
  WarningCircle,
} from "@phosphor-icons/react";

import {
  approveDigitalSelfVersion,
  beginDigitalSelfTesting,
  buildDigitalSelfVersion,
  createVoiceBlindTrial,
  enrollSpeakerProfiles,
  enrollVoiceProfile,
  evaluateVoiceProfile,
  exportAccountArchive,
  freezeDigitalSelfVersion,
  getDigitalSelfVersions,
  getInteractionCapabilities,
  getPersonaStatus,
  getPersonaTraits,
  getPersonaVersions,
  getSpeakerProfiles,
  getVoiceProfiles,
  grantPersonaConsent,
  grantVoiceConsent,
  previewVoiceBlindTrial,
  reviewPersonaTrait,
  revokePersonaConsent,
  revokeDigitalSelfVersion,
  revokeSpeakerProfile,
  revokeVoiceConsent,
  revokeVoiceProfile,
  rollbackDigitalSelfVersion,
  rollbackPersonaVersion,
} from "../api.js";
import {
  prepareSpeakerEnrollment,
  prepareVoiceCloneSample,
} from "../lib/audioEnrollment.js";
import { AccountDeletionForm } from "./AccountDeletionForm.jsx";
import { DigitalSelfVersions } from "./DigitalSelfVersions.jsx";
import { InteractionModePanel } from "./InteractionModePanel.jsx";

const traitLabels = {
  verbal_tic: "口头表达",
  sentence_length: "句式长度",
  speech_rate: "表达节奏",
  pause_style: "停顿方式",
  emphasis_style: "重音方式",
  emotional_expression: "情绪表达",
  discourse_style: "沟通方式",
  narrative_style: "叙事方式",
  decision_habit: "决策习惯",
  value_priority: "价值排序",
};

const statusLabels = {
  active: "已启用",
  candidate: "待评估",
  confirmed: "已确认",
  disabled: "已停用",
  failed: "未通过",
  cleanup_failed: "删除未完成",
  passed: "已通过",
  pending: "待处理",
  revoked: "已撤销",
  shadow: "影子观察",
  superseded: "历史版本",
};

const initialEvaluation = {
  preferred_slot: "",
  similarity: 4,
  naturalness: 4,
  accent_similarity: 4,
  emotion_adherence: 4,
  instruction_adherence: 4,
  uncanny: 2,
  notes: "",
};

function itemsOf(result) {
  return Array.isArray(result) ? result : result?.items || [];
}

function errorMessage(error, fallback) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function StatusBadge({ value }) {
  return (
    <span className="digital-status" data-status={value}>
      {statusLabels[value] || value || "未建立"}
    </span>
  );
}

function ConfirmAction({ title, body, confirmLabel, busy, onCancel, onConfirm }) {
  return (
    <div className="digital-confirm" role="alertdialog" aria-label={title}>
      <WarningCircle size={22} weight="fill" aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <p>{body}</p>
        <div className="digital-confirm-actions">
          <button type="button" className="button-quiet" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button type="button" className="button-danger" onClick={onConfirm} disabled={busy}>
            {busy ? "正在处理…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function DigitalSelfPanel({
  onBack,
  onAccountDeleted,
  onOpenArchive,
  onChangeCompanion,
}) {
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmation, setConfirmation] = useState(null);
  const [interactionCapabilities, setInteractionCapabilities] = useState(null);
  const [personaAllowed, setPersonaAllowed] = useState(false);
  const [personaConsent, setPersonaConsent] = useState(false);
  const [traits, setTraits] = useState([]);
  const [versions, setVersions] = useState([]);
  const [digitalSelfVersions, setDigitalSelfVersions] = useState([]);
  const [speakerConsent, setSpeakerConsent] = useState(false);
  const [speakerFiles, setSpeakerFiles] = useState([]);
  const [speakerProfiles, setSpeakerProfiles] = useState([]);
  const [voiceConsent, setVoiceConsent] = useState(null);
  const [voiceConsentChecked, setVoiceConsentChecked] = useState(false);
  const [voiceFile, setVoiceFile] = useState(null);
  const [voiceProfiles, setVoiceProfiles] = useState([]);
  const [previewText, setPreviewText] = useState("今天也想听你讲讲。");
  const [blindTrial, setBlindTrial] = useState(null);
  const [previewUrls, setPreviewUrls] = useState({});
  const previewUrlRef = useRef({});
  const [evaluation, setEvaluation] = useState(initialEvaluation);
  const [exportPassword, setExportPassword] = useState("");

  const reload = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    setError("");
    const results = await Promise.allSettled([
      getPersonaStatus(),
      getInteractionCapabilities(),
      getPersonaTraits(),
      getPersonaVersions(),
      getDigitalSelfVersions(),
      getSpeakerProfiles(),
      getVoiceProfiles(),
    ]);
    const [
      statusResult,
      interactionResult,
      traitsResult,
      versionsResult,
      digitalSelfVersionsResult,
      speakersResult,
      voicesResult,
    ] = results;
    if (statusResult.status === "fulfilled") {
      setPersonaAllowed(Boolean(statusResult.value?.learning_allowed));
    }
    if (interactionResult.status === "fulfilled") {
      setInteractionCapabilities(interactionResult.value);
    }
    if (traitsResult.status === "fulfilled") setTraits(itemsOf(traitsResult.value));
    if (versionsResult.status === "fulfilled") setVersions(itemsOf(versionsResult.value));
    if (digitalSelfVersionsResult.status === "fulfilled") {
      setDigitalSelfVersions(itemsOf(digitalSelfVersionsResult.value));
    }
    if (speakersResult.status === "fulfilled") setSpeakerProfiles(itemsOf(speakersResult.value));
    if (voicesResult.status === "fulfilled") {
      setVoiceConsent(voicesResult.value?.consent || null);
      setVoiceProfiles(itemsOf(voicesResult.value));
    }
    const anyFailed = results.some((result) => result.status === "rejected");
    if (anyFailed) {
      setError("部分状态暂时无法同步，你仍可查看已经加载的内容并稍后重试。");
    }
    if (!silent) setLoading(false);
    return {
      anyFailed,
      digitalSelfFailed: digitalSelfVersionsResult.status === "rejected",
    };
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(
    () => () => {
      Object.values(previewUrlRef.current).forEach((url) => URL.revokeObjectURL(url));
    },
    [],
  );

  const run = async (key, action, success) => {
    setBusy(key);
    setError("");
    setNotice("");
    try {
      await action();
      await reload({ silent: true });
      setNotice(success);
      return true;
    } catch (actionError) {
      await reload({ silent: true });
      setError(errorMessage(actionError, "操作没有完成，请稍后重试。"));
      return false;
    } finally {
      setBusy("");
    }
  };

  const applyDigitalSelfVersion = (version) => {
    if (!version?.version_id) return;
    setDigitalSelfVersions((current) =>
      [...current.filter((item) => item.version_id !== version.version_id), version]
        .sort((left, right) => right.version_number - left.version_number),
    );
  };

  const runDigitalSelfMutation = async (key, action, success) => {
    setBusy(key);
    setError("");
    setNotice("");
    try {
      const version = await action();
      const sync = await reload({ silent: true });
      if (sync.digitalSelfFailed) {
        applyDigitalSelfVersion(version);
        setError(
          "版本操作已完成，但最新版本列表暂时无法同步。当前显示保留了服务器返回的结果，请稍后重试。",
        );
      }
      setNotice(success);
      return true;
    } catch (actionError) {
      await reload({ silent: true });
      setError(errorMessage(actionError, "操作没有完成，请稍后重试。"));
      return false;
    } finally {
      setBusy("");
    }
  };

  const confirm = (config) => setConfirmation(config);
  const executeConfirmation = async () => {
    if (!confirmation) return;
    const current = confirmation;
    const completed = await run(current.key, current.action, current.success);
    if (completed) setConfirmation(null);
  };

  const enrollSpeaker = async () => {
    setBusy("speaker-prepare");
    setError("");
    setNotice("正在读取并转换录音，原文件不会写入浏览器缓存…");
    try {
      const samples = await prepareSpeakerEnrollment(speakerFiles);
      setBusy("speaker-upload");
      setNotice("录音已转换，正在生成声纹模板…");
      await enrollSpeakerProfiles(samples);
      await reload({ silent: true });
      setSpeakerFiles([]);
      setNotice("声纹档案已进入影子观察，正式评估通过前不会作为主人判定依据。");
    } catch (actionError) {
      setError(errorMessage(actionError, "声纹登记没有完成，请检查录音后重试。"));
    } finally {
      setBusy("");
    }
  };

  const buildDigitalSelf = () =>
    runDigitalSelfMutation(
      "digital-self-build",
      buildDigitalSelfVersion,
      "数字分身草稿已根据当前确认材料生成。",
    );

  const transitionDigitalSelf = (action, version, password) => {
    const digest = version.manifest_sha256;
    const actions = {
      testing: {
        execute: () => beginDigitalSelfTesting(version.version_id, digest),
        success: `数字分身版本 ${version.version_number} 已进入测试。`,
      },
      approve: {
        execute: () =>
          approveDigitalSelfVersion(version.version_id, password, digest),
        success: `数字分身版本 ${version.version_number} 已批准。`,
      },
      freeze: {
        execute: () =>
          freezeDigitalSelfVersion(version.version_id, password, digest),
        success: `数字分身版本 ${version.version_number} 已冻结。`,
      },
      revoke: {
        execute: () =>
          revokeDigitalSelfVersion(version.version_id, password, digest),
        success: `数字分身版本 ${version.version_number} 已撤销。`,
      },
      rollback: {
        execute: () =>
          rollbackDigitalSelfVersion(version.version_id, password, digest),
        success: `已基于版本 ${version.version_number} 创建新的回滚草稿。`,
      },
    };
    const selected = actions[action];
    if (!selected) return Promise.resolve(false);
    return runDigitalSelfMutation(
      `digital-self-${action}-${version.version_id}`,
      selected.execute,
      selected.success,
    );
  };

  const enrollVoice = async () => {
    if (!voiceFile) return;
    setBusy("voice-prepare");
    setError("");
    setNotice("正在本机读取录音并检查时长…");
    try {
      const sample = await prepareVoiceCloneSample(voiceFile);
      setBusy("voice-upload");
      setNotice("录音检查通过，正在上传并登记供应商声音…");
      await enrollVoiceProfile(sample);
      await reload({ silent: true });
      setVoiceFile(null);
      setBlindTrial(null);
      setNotice("候选声音已创建。你可以继续试听和评估；当前豆包语音暂不应用此档案。");
    } catch (actionError) {
      setError(errorMessage(actionError, "候选声音没有创建成功，请检查录音后重试。"));
    } finally {
      setBusy("");
    }
  };

  const beginBlindTrial = async (profileId) => {
    setBusy("voice-blind-trial");
    setError("");
    try {
      const trial = await createVoiceBlindTrial(profileId);
      Object.values(previewUrlRef.current).forEach((url) => URL.revokeObjectURL(url));
      previewUrlRef.current = {};
      setPreviewUrls({});
      setEvaluation(initialEvaluation);
      setBlindTrial(trial);
      setNotice("盲测已开始。系统不会告诉浏览器哪一项是候选声音。");
    } catch (actionError) {
      setError(errorMessage(actionError, "A/B 盲测没有开始，请稍后重试。"));
    } finally {
      setBusy("");
    }
  };

  const renderPreview = async (slot) => {
    if (!blindTrial) return;
    const key = `preview-${slot}`;
    setBusy(key);
    setError("");
    try {
      const blob = await previewVoiceBlindTrial(
        blindTrial.trial_id,
        slot,
        previewText.trim(),
      );
      const previous = previewUrlRef.current[slot];
      if (previous) URL.revokeObjectURL(previous);
      const url = URL.createObjectURL(blob);
      previewUrlRef.current = { ...previewUrlRef.current, [slot]: url };
      setPreviewUrls({ ...previewUrlRef.current });
    } catch (actionError) {
      setError(errorMessage(actionError, "试听生成失败，请稍后重试。"));
    } finally {
      setBusy("");
    }
  };

  const downloadArchive = async () => {
    setBusy("account-export");
    setError("");
    setNotice("");
    try {
      const archive = await exportAccountArchive(exportPassword);
      const blob = new Blob([JSON.stringify(archive, null, 2)], {
        type: "application/json;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `memoria-archive-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setExportPassword("");
      setNotice("完整档案已生成并开始下载，请妥善保管文件。");
    } catch (actionError) {
      setError(errorMessage(actionError, "档案导出没有完成，请检查密码后重试。"));
    } finally {
      setBusy("");
    }
  };

  const activeVoiceConsent = Boolean(voiceConsent && !voiceConsent.revoked_at);
  const incompleteVoiceProfile =
    voiceProfiles.find(
      (profile) =>
        profile.status === "revoked" && profile.deletion_status !== "completed",
    ) || null;
  const voiceCleanupIncomplete = Boolean(
    incompleteVoiceProfile ||
      (voiceConsent?.revoked_at &&
        voiceProfiles.some((profile) => profile.deletion_status !== "completed")),
  );
  const selectedVoice = activeVoiceConsent
    ? voiceProfiles.find((profile) => profile.status === "candidate") ||
      voiceProfiles.find((profile) => profile.status === "active") ||
      null
    : null;
  const activeVersion = versions.find((version) => version.status === "active");
  const learnedTraits = traits.filter(
    (trait) => trait.status === "confirmed" || trait.status === "disabled",
  );
  const historicalVersions = versions.filter((version) => version.status !== "active");
  const hasPersonaArchive = Boolean(
    activeVersion || learnedTraits.length || historicalVersions.length,
  );
  const personaLearningStatus = activeVersion
    ? `v${activeVersion.version_number} 已启用`
    : "持续学习中（聊天越多越准确）";

  return (
    <section className="screen digital-self-screen" aria-label="数字心智与声音">
      <header className="topbar digital-self-topbar">
        <button type="button" className="icon-button" aria-label="返回我的" onClick={onBack}>
          <ArrowLeft size={22} weight="bold" />
        </button>
        <div>
          <p className="eyebrow">每一项都可独立授权和撤销</p>
          <h1>数字心智与声音</h1>
        </div>
      </header>

      <div className="digital-self-scroll" aria-busy={loading}>
        <article className="digital-self-intro">
          <LockKey size={24} weight="fill" aria-hidden="true" />
          <div>
            <strong>你始终拥有控制权</strong>
            <p>陪伴方式、数字分身、声纹和复刻声音相互独立；撤销后实时对话会回到安全基线。</p>
          </div>
        </article>

        {loading ? (
          <div className="digital-loading" role="status">
            <span className="loading-orbit" />
            正在读取你的数字档案…
          </div>
        ) : (
          <>
            <InteractionModePanel
              capabilities={interactionCapabilities}
              activeVersion={activeVersion}
              learnedTraitCount={learnedTraits.length}
              onOpenArchive={onOpenArchive}
              onChangeCompanion={onChangeCompanion}
            />
            <DigitalSelfVersions
              versions={digitalSelfVersions}
              busy={busy}
              onBuild={buildDigitalSelf}
              onTransition={transitionDigitalSelf}
            />
            <section className="digital-section" aria-labelledby="persona-title">
              <div className="digital-section-heading">
                <span className="digital-section-icon"><Brain size={22} weight="fill" /></span>
                <div>
                  <p>表达、性格与思维偏好</p>
                  <h2 id="persona-title">人格学习</h2>
                </div>
                <StatusBadge value={personaAllowed ? "active" : "pending"} />
              </div>
              <p className="digital-explainer">
                授权后会在自然聊天中持续提取特征；只有跨会话重复出现的低敏表达风格会自动生效。撤销后停止学习，已生效特征仍可停用和回滚。
              </p>

              {!personaAllowed && (
                <div className="digital-consent-box">
                  <label>
                    <input
                      type="checkbox"
                      checked={personaConsent}
                      onChange={(event) => setPersonaConsent(event.target.checked)}
                    />
                    <span>我同意 Memoria 学习我的表达与思维偏好</span>
                  </label>
                  <button
                    type="button"
                    className="button-primary"
                    disabled={!personaConsent || Boolean(busy)}
                    onClick={() => void run(
                      "persona-grant",
                      grantPersonaConsent,
                      "人格学习已开启，系统会在持续聊天中自动更新。",
                    )}
                  >
                    {busy === "persona-grant" ? "正在开启…" : "开启人格学习"}
                  </button>
                </div>
              )}
              {personaAllowed && (
                <div className="digital-meta-row">
                  <span>{activeVersion ? "当前版本" : "学习状态"}</span>
                  <strong>{personaLearningStatus}</strong>
                  <button
                    type="button"
                    className="text-danger"
                    onClick={() => confirm({
                      key: "persona-revoke",
                      title: "撤销人格学习授权",
                      body: "将立即停止采集新的表达与思维证据。已形成的特征不会偷偷继续更新。",
                      confirmLabel: "确认撤销人格学习",
                      action: revokePersonaConsent,
                      success: "人格学习授权已撤销。",
                    })}
                  >
                    撤销人格学习授权
                  </button>
                </div>
              )}
              {!personaAllowed && hasPersonaArchive && (
                <div className="digital-meta-row">
                  <span>当前版本</span>
                  <strong>
                    {activeVersion ? `v${activeVersion.version_number} 已启用` : "档案已保留"}
                  </strong>
                </div>
              )}
              {(personaAllowed || hasPersonaArchive) && (
                <>
                  {learnedTraits.length ? (
                    <div className="trait-list">
                      {learnedTraits.map((trait) => (
                        <article className="trait-card" key={trait.trait_id}>
                          <div className="trait-meta">
                            <span>{traitLabels[trait.category] || trait.category}</span>
                            <StatusBadge value={trait.status} />
                          </div>
                          <p>{trait.description}</p>
                          <small>
                            来自 {trait.observation_count || 0} 次观察 · 置信度 {Math.round((trait.confidence || 0) * 100)}%
                          </small>
                          {trait.counterexample?.trim() ? (
                            <p className="trait-counterexample">
                              <strong>例外/反例</strong>
                              {trait.counterexample}
                            </p>
                          ) : null}
                          {trait.status !== "disabled" && (
                            <div className="inline-actions">
                              <button
                                type="button"
                                className="button-quiet"
                                onClick={() => void run(
                                  `trait-disable-${trait.trait_id}`,
                                  () => reviewPersonaTrait(trait.trait_id, "disable"),
                                  "这条人格特征已停用。",
                                )}
                              >
                                停用
                              </button>
                            </div>
                          )}
                        </article>
                      ))}
                    </div>
                  ) : (
                    <p className="digital-empty">
                      {personaAllowed
                        ? "继续自然聊天即可。证据充分后，系统会自动生成并更新人格版本。"
                        : "人格学习已停止；重新授权后可继续积累新特征。"}
                    </p>
                  )}
                  {historicalVersions.length > 0 && (
                    <details className="version-history">
                      <summary>查看人格历史版本</summary>
                      {historicalVersions.map((version) => (
                        <div key={version.version_id}>
                          <span>v{version.version_number} · {version.reason}</span>
                          <button
                            type="button"
                            onClick={() => confirm({
                              key: `persona-rollback-${version.version_id}`,
                              title: `回退到人格版本 v${version.version_number}`,
                              body: "当前版本会保留为历史记录，后续仍可追溯。",
                              confirmLabel: "确认回退版本",
                              action: () => rollbackPersonaVersion(version.version_id),
                              success: `已回退到人格版本 v${version.version_number}。`,
                            })}
                          >
                            <ClockCounterClockwise size={17} /> 回退
                          </button>
                        </div>
                      ))}
                    </details>
                  )}
                </>
              )}
              {confirmation?.key === "persona-revoke" && (
                <ConfirmAction
                  title={confirmation.title}
                  body={confirmation.body}
                  confirmLabel={confirmation.confirmLabel}
                  busy={busy === confirmation.key}
                  onCancel={() => setConfirmation(null)}
                  onConfirm={() => void executeConfirmation()}
                />
              )}
              {confirmation?.key?.startsWith("persona-rollback-") && (
                <ConfirmAction
                  title={confirmation.title}
                  body={confirmation.body}
                  confirmLabel={confirmation.confirmLabel}
                  busy={busy === confirmation.key}
                  onCancel={() => setConfirmation(null)}
                  onConfirm={() => void executeConfirmation()}
                />
              )}
            </section>

            <section className="digital-section" aria-labelledby="speaker-title">
              <div className="digital-section-heading">
                <span className="digital-section-icon"><Fingerprint size={22} weight="fill" /></span>
                <div>
                  <p>主人、访客或不确定</p>
                  <h2 id="speaker-title">声纹识别</h2>
                </div>
                <StatusBadge value={speakerProfiles.find((item) => item.status === "active")?.status || speakerProfiles[0]?.status || "pending"} />
              </div>
              <div className="digital-warning">
                <WarningCircle size={20} weight="fill" aria-hidden="true" />
                <p>
                  声纹只用于区分主人、访客或不确定。“我的”里的“过滤明显旁人（实验）”可减少旁人插话；关闭时访客可聊，但不会访问或写入主人回顾。声纹不能单独授权删除、导出或其他敏感操作。
                </p>
              </div>
              <div className="digital-consent-box">
                <label>
                  <input
                    type="checkbox"
                    checked={speakerConsent}
                    onChange={(event) => setSpeakerConsent(event.target.checked)}
                  />
                  <span>我确认这些录音均为本人声音并同意用于声纹登记</span>
                </label>
                <label className="file-field">
                  <span>选择 3–10 段声纹录音</span>
                  <input
                    type="file"
                    accept="audio/*"
                    multiple
                    aria-label="选择 3–10 段声纹录音"
                    onChange={(event) => setSpeakerFiles(Array.from(event.target.files || []))}
                  />
                  <small>{speakerFiles.length ? `已选择 ${speakerFiles.length} 段` : "每段 1.5–15 秒，内容尽量不同"}</small>
                </label>
                <button
                  type="button"
                  className="button-primary"
                  disabled={!speakerConsent || speakerFiles.length < 3 || speakerFiles.length > 10 || Boolean(busy)}
                  onClick={() => void enrollSpeaker()}
                >
                  <UploadSimple size={18} weight="bold" />
                  {busy.startsWith("speaker-") ? "正在登记…" : "登记声纹"}
                </button>
              </div>
              <div className="profile-list">
                {speakerProfiles.map((speaker) => (
                  <article className="digital-profile-card" key={speaker.profile_id}>
                    <div>
                      <strong>声纹模板 v{speaker.template_version}</strong>
                      <StatusBadge value={speaker.status} />
                    </div>
                    <p>{speaker.model_version} · {speaker.sample_count} 段登记录音</p>
                    {speaker.status === "shadow" && (
                      <small className="shadow-note">仍需至少 200 条正式评估样本并通过 FAR/FRR/EER 门禁，才可激活主人判定。</small>
                    )}
                    {speaker.status !== "revoked" && (
                      <button
                        type="button"
                        className="button-danger-outline"
                        onClick={() => confirm({
                          key: `speaker-revoke-${speaker.profile_id}`,
                          title: "撤销声纹档案",
                          body: "该模板会立即停止参与主人判定，并进入可审计的删除流程。",
                          confirmLabel: "确认撤销声纹",
                          action: () => revokeSpeakerProfile(speaker.profile_id),
                          success: "声纹档案已撤销。",
                        })}
                      >
                        <Trash size={17} /> 撤销声纹档案
                      </button>
                    )}
                  </article>
                ))}
              </div>
              {confirmation?.key?.startsWith("speaker-revoke-") && (
                <ConfirmAction
                  title={confirmation.title}
                  body={confirmation.body}
                  confirmLabel={confirmation.confirmLabel}
                  busy={busy === confirmation.key}
                  onCancel={() => setConfirmation(null)}
                  onConfirm={() => void executeConfirmation()}
                />
              )}
            </section>

            <section className="digital-section" aria-labelledby="voice-title">
              <div className="digital-section-heading">
                <span className="digital-section-icon"><SpeakerHigh size={22} weight="fill" /></span>
                <div>
                  <p>授权录音与可撤销音色</p>
                  <h2 id="voice-title">声音复刻</h2>
                </div>
                <StatusBadge
                  value={
                    voiceCleanupIncomplete
                      ? "cleanup_failed"
                      : selectedVoice?.status || (activeVoiceConsent ? "pending" : "revoked")
                  }
                />
              </div>
              <p className="digital-explainer">
                复刻声音不等于声纹身份，也不等于人格。当前实时对话使用 FunASR + 豆包 TTS 2.0 级联；历史复刻档案继续保留和可撤销，但暂不应用于当前豆包语音，也不能在此激活。
              </p>

              {voiceCleanupIncomplete ? (
                <div className="digital-consent-box">
                  <p>供应商声音删除未完成。声音档案已停止使用，请重试清理。</p>
                  <button
                    type="button"
                    className="button-danger-outline"
                    disabled={Boolean(busy)}
                    onClick={() => void run(
                      "voice-consent-retry",
                      incompleteVoiceProfile
                        ? () => revokeVoiceProfile(incompleteVoiceProfile.profile_id)
                        : revokeVoiceConsent,
                      "声音资产删除已完成。",
                    )}
                  >
                    {busy === "voice-consent-retry" ? "正在重试…" : "重试删除声音资产"}
                  </button>
                </div>
              ) : !activeVoiceConsent ? (
                <div className="digital-consent-box">
                  <label>
                    <input
                      type="checkbox"
                      checked={voiceConsentChecked}
                      onChange={(event) => setVoiceConsentChecked(event.target.checked)}
                    />
                    <span>我同意使用本人录音创建可撤销的声音档案</span>
                  </label>
                  <button
                    type="button"
                    className="button-primary"
                    disabled={!voiceConsentChecked || Boolean(busy)}
                    onClick={() => void run(
                      "voice-grant",
                      grantVoiceConsent,
                      "声音复刻授权已记录，你现在可以创建候选声音。",
                    )}
                  >
                    {busy === "voice-grant" ? "正在授权…" : "授权声音复刻"}
                  </button>
                </div>
              ) : (
                <>
                  <div className="digital-meta-row">
                    <span>授权版本</span>
                    <strong>{voiceConsent.policy_version}</strong>
                    <button
                      type="button"
                      className="text-danger"
                      onClick={() => confirm({
                        key: "voice-consent-revoke",
                        title: "撤销声音复刻总授权",
                        body: "所有未撤销声音档案都会停止使用，并进入供应商删除流程。实时对话继续使用所选伙伴的豆包设计音色。",
                        confirmLabel: "确认撤销声音授权",
                        action: revokeVoiceConsent,
                        success: "声音复刻总授权已撤销。",
                      })}
                    >
                      撤销总授权
                    </button>
                  </div>
                  <div className="digital-consent-box">
                    <label className="file-field">
                      <span>选择 10–20 秒本人录音</span>
                      <input
                        type="file"
                        accept="audio/wav,audio/x-wav,audio/mpeg,audio/mp4,audio/aac,audio/ogg,audio/flac"
                        aria-label="选择 10–20 秒本人录音"
                        onChange={(event) => setVoiceFile(event.target.files?.[0] || null)}
                      />
                      <small>{voiceFile ? voiceFile.name : "安静环境、只有本人说话，最大 15 MB"}</small>
                    </label>
                    <button
                      type="button"
                      className="button-primary"
                      disabled={!voiceFile || Boolean(busy)}
                      onClick={() => void enrollVoice()}
                    >
                      <UploadSimple size={18} weight="bold" />
                      {busy.startsWith("voice-") ? "正在创建…" : "创建候选声音"}
                    </button>
                  </div>
                </>
              )}

              {selectedVoice && selectedVoice.status !== "revoked" && (
                <article className="voice-candidate-card">
                  <div className="voice-candidate-title">
                    <div>
                      <strong>声音档案 v{selectedVoice.version_number}</strong>
                      <p>{selectedVoice.target_model}</p>
                    </div>
                    <StatusBadge value={selectedVoice.status} />
                  </div>
                  <label className="preview-text-field">
                    同文本试听内容
                    <input
                      value={previewText}
                      maxLength={120}
                      disabled={Boolean(blindTrial)}
                      onChange={(event) => setPreviewText(event.target.value)}
                    />
                  </label>
                  {!blindTrial && selectedVoice.evaluation_status !== "passed" ? (
                    <button
                      type="button"
                      className="button-secondary full-width"
                      disabled={!previewText.trim() || Boolean(busy)}
                      onClick={() => void beginBlindTrial(selectedVoice.profile_id)}
                    >
                      {busy === "voice-blind-trial" ? "正在准备…" : "开始 A/B 盲测"}
                    </button>
                  ) : blindTrial && selectedVoice.evaluation_status !== "passed" ? (
                    <div className="preview-grid">
                      {["A", "B"].map((slot) => (
                        <div className="preview-card" key={slot}>
                          <span>声音 {slot}</span>
                        <button
                          type="button"
                          className="button-secondary"
                          disabled={!previewText.trim() || busy === `preview-${slot}`}
                          onClick={() => void renderPreview(slot)}
                        >
                          <Play size={17} weight="fill" />
                          {busy === `preview-${slot}` ? "生成中…" : `试听 ${slot}`}
                        </button>
                        {previewUrls[slot] && (
                          <audio
                            controls
                            preload="none"
                            src={previewUrls[slot]}
                            aria-label={`声音 ${slot} 试听`}
                          />
                        )}
                      </div>
                      ))}
                    </div>
                  ) : null}

                  {selectedVoice.evaluation_status !== "passed" ? (
                    <form
                      className="evaluation-form"
                      onSubmit={(event) => {
                        event.preventDefault();
                        if (
                          !blindTrial ||
                          !evaluation.preferred_slot ||
                          !previewUrls.A ||
                          !previewUrls.B
                        ) return;
                        void run(
                          "voice-evaluate",
                          () => evaluateVoiceProfile(selectedVoice.profile_id, {
                            trial_id: blindTrial.trial_id,
                            preferred_slot: evaluation.preferred_slot,
                            similarity: evaluation.similarity,
                            naturalness: evaluation.naturalness,
                            accent_similarity: evaluation.accent_similarity,
                            emotion_adherence: evaluation.emotion_adherence,
                            instruction_adherence: evaluation.instruction_adherence,
                            uncanny: evaluation.uncanny,
                            notes: evaluation.notes,
                          }),
                          "A/B 评估已提交。结果会保留在历史档案中，当前豆包语音暂不应用此档案。",
                        );
                      }}
                    >
                      <div className="evaluation-title">
                        <div><CheckCircle size={20} weight="fill" /><strong>A/B 评估</strong></div>
                        <small>五项正向评分均 ≥3.5；违和感 ≤2.5</small>
                      </div>
                      <fieldset className="blind-preference">
                        <legend>哪一个更像本人？</legend>
                        {["A", "B"].map((slot) => (
                          <label key={slot}>
                            <input
                              type="radio"
                              name="preferred-voice-slot"
                              value={slot}
                              checked={evaluation.preferred_slot === slot}
                              onChange={(event) => setEvaluation({
                                ...evaluation,
                                preferred_slot: event.target.value,
                              })}
                            />
                            更喜欢声音 {slot}
                          </label>
                        ))}
                      </fieldset>
                      <div className="score-grid">
                        {[
                          ["similarity", "相似度（1–5）", 1, 5, 0.1],
                          ["naturalness", "自然度（1–5）", 1, 5, 0.1],
                          ["accent_similarity", "口音相似度（1–5）", 1, 5, 0.1],
                          ["emotion_adherence", "情绪遵循度（1–5）", 1, 5, 0.1],
                          ["instruction_adherence", "指令遵循度（1–5）", 1, 5, 0.1],
                          ["uncanny", "违和感（1–5，越低越好）", 1, 5, 0.1],
                        ].map(([field, label, min, max, step]) => (
                          <label key={field}>
                            {label}
                            <input
                              type="number"
                              min={min}
                              max={max}
                              step={step}
                              value={evaluation[field]}
                              onChange={(event) => setEvaluation({ ...evaluation, [field]: Number(event.target.value) })}
                            />
                          </label>
                        ))}
                      </div>
                      <label>
                        评估备注（可选）
                        <textarea
                          rows="2"
                          maxLength="2000"
                          value={evaluation.notes}
                          onChange={(event) => setEvaluation({ ...evaluation, notes: event.target.value })}
                        />
                      </label>
                      <button
                        type="submit"
                        className="button-primary"
                        disabled={
                          Boolean(busy) ||
                          !blindTrial ||
                          !evaluation.preferred_slot ||
                          !previewUrls.A ||
                          !previewUrls.B
                        }
                      >
                        {busy === "voice-evaluate" ? "正在提交…" : "提交 A/B 评估"}
                      </button>
                    </form>
                  ) : selectedVoice.quality_status !== "passed" ? (
                    <p className="voice-quality-pending" role="status">
                      <ClockCounterClockwise size={18} />
                      {selectedVoice.quality_status === "failed"
                        ? "服务端质量探针未通过，档案仅作为历史记录保留。"
                        : "主观盲测已通过，等待服务端质量探针；档案暂不应用于当前豆包语音。"}
                    </p>
                  ) : selectedVoice.status !== "active" ? (
                    <p className="voice-quality-pending" role="status">
                      <ClockCounterClockwise size={18} />
                      历史档案已通过评估，但暂不应用于当前豆包语音。实时对话继续使用所选伙伴的豆包设计音色。
                    </p>
                  ) : (
                    <p className="voice-active-note"><CheckCircle size={18} weight="fill" /> 此档案保留原激活状态，但暂不应用于当前豆包语音；实时对话使用所选伙伴的豆包设计音色。</p>
                  )}
                  <button
                    type="button"
                    className="button-danger-outline full-width"
                    onClick={() => confirm({
                      key: `voice-profile-revoke-${selectedVoice.profile_id}`,
                      title: "撤销声音档案",
                      body: "实时对话继续使用所选伙伴的豆包设计音色，并请求供应商删除对应复刻声音。",
                      confirmLabel: "确认撤销声音档案",
                      action: () => revokeVoiceProfile(selectedVoice.profile_id),
                      success: "声音档案已撤销。",
                    })}
                  >
                    <Trash size={17} /> 撤销声音档案
                  </button>
                </article>
              )}
              {confirmation?.key === "voice-consent-revoke" && (
                <ConfirmAction
                  title={confirmation.title}
                  body={confirmation.body}
                  confirmLabel={confirmation.confirmLabel}
                  busy={busy === confirmation.key}
                  onCancel={() => setConfirmation(null)}
                  onConfirm={() => void executeConfirmation()}
                />
              )}
              {confirmation?.key?.startsWith("voice-profile-revoke-") && (
                <ConfirmAction
                  title={confirmation.title}
                  body={confirmation.body}
                  confirmLabel={confirmation.confirmLabel}
                  busy={busy === confirmation.key}
                  onCancel={() => setConfirmation(null)}
                  onConfirm={() => void executeConfirmation()}
                />
              )}
            </section>

            <section className="digital-section" aria-labelledby="account-data-title">
              <div className="digital-section-heading">
                <span className="digital-section-icon">
                  <DownloadSimple size={22} weight="fill" />
                </span>
                <div>
                  <p>可携带、可退出</p>
                  <h2 id="account-data-title">数据与账户</h2>
                </div>
              </div>
              <p className="digital-explainer">
                你可以随时导出完整 JSON 档案。永久删除会传播到对话、记忆投影、人格、声纹、声音样本和供应商资产，完成后当前登录立即失效。
              </p>

              <form
                className="account-data-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  void downloadArchive();
                }}
              >
                <label htmlFor="archive-export-password">导出验证密码</label>
                <input
                  id="archive-export-password"
                  type="password"
                  autoComplete="current-password"
                  value={exportPassword}
                  onChange={(event) => setExportPassword(event.target.value)}
                />
                <button
                  type="submit"
                  className="button-secondary full-width"
                  disabled={!exportPassword || Boolean(busy)}
                >
                  <DownloadSimple size={18} aria-hidden="true" />
                  {busy === "account-export" ? "正在生成档案…" : "下载完整档案"}
                </button>
              </form>

              <AccountDeletionForm onDeleted={onAccountDeleted} />
            </section>
          </>
        )}

        {notice && <p className="digital-notice" role="status">{notice}</p>}
        {error && (
          <div className="digital-error" role="alert">
            <WarningCircle size={19} weight="fill" />
            <span>{error}</span>
            <button type="button" onClick={() => void reload()}>重试同步</button>
          </div>
        )}
      </div>
    </section>
  );
}
