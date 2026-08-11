import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, CaretRight, Check, Sparkle } from "@phosphor-icons/react";

import {
  AGE_BAND_LABELS,
  MODE_AGE_BANDS,
  MODE_CARDS,
  MODE_TITLES,
  PRIMARY_RELATIONSHIPS,
  consentOffersFor,
} from "../lib/multiSubject/modeMeta.js";
import { companions } from "../lib/companions.js";

/**
 * 首次绑定四分流（整改文档 §2 / D-02）。
 *
 * 模式键严格使用 canonical DeviceDeclaredMode（parent_for_child /
 * self_use / child_for_parent / family_shared），UI 标题分别为
 * 给孩子使用 / 给自己使用 / 给父母使用 / 家庭共同使用。
 *
 * 数据边界：
 * - primary_subject.relationship 只引用 modeMeta.PRIMARY_RELATIONSHIPS
 *   （canonical RelationshipType），不手写第二份映射；
 * - family_shared 的 primary_subject 是当前 owner person_id +
 *   family_member_of，由服务端创建 family_space；绑定页不创建“家庭
 *   PersonSubject”，也不宣称成员档案已建立（成员邀请是绑定后的后续步骤，
 *   在成员邀请 API 冻结前不采集成员列表）；
 * - self_use 的称呼是只读展示（项目约定称呼只在首次注册设置，设备绑定
 *   不重新编辑），不阻塞绑定；
 * - 客户端不提交 policy_version 或任何假授权字段；child_for_parent 中
 *   “父母本人接受”类 offer 只展示为待父母在设备上确认，不能代为同意。
 */
const STEPS = ["claim", "mode", "form", "confirm", "done"];

function stepLabel(step) {
  return (
    {
      claim: "输入设备码",
      mode: "选择使用方式",
      form: "填写信息",
      confirm: "确认摘要",
      done: "绑定完成",
    }[step] || "绑定设备"
  );
}

function defaultForm(mode, identityName) {
  if (mode === "parent_for_child") {
    return {
      childNickname: "",
      childAgeBand: "under_14",
      tutorEnabled: true,
      englishPracticeEnabled: true,
      maxSessionMinutes: 30,
      quietHoursStart: "21:00",
      quietHoursEnd: "07:00",
    };
  }
  if (mode === "self_use") {
    return {
      displayName: identityName || "新朋友",
      memoryLevel: "personal",
      interviewFrequency: "low",
    };
  }
  if (mode === "child_for_parent") {
    return {
      parentNickname: "",
      speechSpeed: "slow",
      memoryLevel: "none",
      adminVisibility: "device_status",
    };
  }
  return {
    sharedPersonaEnabled: true,
  };
}

function defaultConsents(mode) {
  const checked = {};
  for (const offer of consentOffersFor(mode)) {
    if (offer.requiresParentSelfAcceptance) continue;
    checked[offer.id] = offer.defaultChecked;
  }
  return checked;
}

function collectConsentIds(mode, consents) {
  const offers = consentOffersFor(mode);
  return offers
    .filter(
      (offer) =>
        !offer.requiresParentSelfAcceptance && consents[offer.id] === true,
    )
    .map((offer) => offer.id);
}

function collectPreferences(mode, form) {
  if (mode === "parent_for_child") {
    return {
      tutor_enabled: form.tutorEnabled,
      english_practice_enabled: form.englishPracticeEnabled,
      memory_level: "growth_summary",
      max_session_minutes: form.maxSessionMinutes,
      quiet_hours: { start: form.quietHoursStart, end: form.quietHoursEnd },
    };
  }
  if (mode === "self_use") {
    return {
      memory_level: form.memoryLevel,
      interview_frequency: form.interviewFrequency,
    };
  }
  if (mode === "child_for_parent") {
    return {
      speech_speed: form.speechSpeed,
      memory_level: form.memoryLevel,
      admin_visibility: form.adminVisibility,
    };
  }
  return {
    memory_level: "family_shared",
    shared_persona_enabled: form.sharedPersonaEnabled,
  };
}

/**
 * 表单 → POST /v1/device-bindings 请求（纯函数，便于请求约束测试）。
 *
 * primary_subject 规则：
 * - parent_for_child / child_for_parent：person_id=new + subject_draft；
 * - self_use：owner person_id + self（称呼不随绑定提交）；
 * - family_shared：owner person_id + family_member_of（不创建“家庭主体”，
 *   成员邀请是绑定后后续步骤，当前请求不含成员列表）。
 */
export function buildDeviceBindingRequest({
  mode,
  form,
  consents,
  personaId,
  ownerPersonId,
  claimToken,
}) {
  const relationship = PRIMARY_RELATIONSHIPS[mode];
  if (!relationship) {
    throw new TypeError(`declared_mode 取值无效：${mode}`);
  }
  let primarySubject;
  if (mode === "parent_for_child" || mode === "child_for_parent") {
    primarySubject = {
      person_id: "new",
      relationship,
      subject_draft: {
        display_name:
          mode === "parent_for_child" ? form.childNickname : form.parentNickname,
        age_band: mode === "parent_for_child" ? form.childAgeBand : "adult",
      },
    };
  } else {
    primarySubject = { person_id: ownerPersonId, relationship };
  }
  return {
    device_claim_token: claimToken,
    declared_mode: mode,
    account_owner_person_id: ownerPersonId,
    primary_subject: primarySubject,
    persona_selection: personaId,
    service_preferences: collectPreferences(mode, form),
    consent_offer_ids: collectConsentIds(mode, consents),
  };
}

export function DeviceBindingFlow({
  identity,
  onCreateBinding,
  onComplete,
  onBack,
}) {
  const [step, setStep] = useState("claim");
  const [claimToken, setClaimToken] = useState("");
  const [mode, setMode] = useState(null);
  const [form, setForm] = useState(null);
  const [consents, setConsents] = useState({});
  const [personaId, setPersonaId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);
  const screenRef = useRef(null);

  const ownerPersonId = identity?.user_id || "";
  const identityName = identity?.display_name || "新朋友";

  useEffect(() => {
    if (screenRef.current) screenRef.current.scrollTop = 0;
  }, [step, mode]);

  const goToForm = (selectedMode) => {
    setMode(selectedMode);
    setForm(defaultForm(selectedMode, identityName));
    setConsents(defaultConsents(selectedMode));
    setPersonaId("");
    setError("");
    setStep("form");
  };

  const formComplete = useMemo(() => {
    if (!form) return false;
    if (mode === "parent_for_child") {
      return Boolean(form.childNickname.trim());
    }
    if (mode === "child_for_parent") {
      return Boolean(form.parentNickname.trim());
    }
    // self_use / family_shared：称呼只读展示、成员为后续步骤，不阻塞绑定。
    return true;
  }, [form, mode]);

  const summaryItems = useMemo(() => {
    if (!mode || !form) return [];
    const items = [];
    if (mode === "parent_for_child") {
      items.push([
        "给孩子使用",
        `${form.childNickname}（${AGE_BAND_LABELS[form.childAgeBand]}）`,
      ]);
      items.push([
        "服务偏好",
        `${form.tutorEnabled ? "学习陪练开启" : "学习陪练关闭"} · ${
          form.englishPracticeEnabled ? "英语口语开启" : "英语口语关闭"
        } · 单次 ${form.maxSessionMinutes} 分钟`,
      ]);
    } else if (mode === "self_use") {
      items.push(["给自己使用", identityName]);
      items.push([
        "服务偏好",
        `记忆：${
          form.memoryLevel === "personal" ? "完整个人记忆" : "仅当前对话"
        } · 采访：${
          { off: "不主动", low: "偶尔", medium: "适度" }[form.interviewFrequency]
        }`,
      ]);
    } else if (mode === "child_for_parent") {
      items.push(["给父母使用", `${form.parentNickname}（本人主体）`]);
      items.push([
        "服务偏好",
        `语速：${form.speechSpeed === "slow" ? "慢速清晰" : "正常语速"} · ${
          form.adminVisibility === "device_status"
            ? "子女只看设备状态与提醒"
            : "子女可看设备状态 + 周报聚合"
        }`,
      ]);
    } else {
      items.push([
        "家庭共同使用",
        "绑定后可在「设备与成员」中邀请/添加成员，每位成员独立档案",
      ]);
      items.push([
        "服务偏好",
        `共享伙伴：${form.sharedPersonaEnabled ? "开启" : "关闭"} · 共享记忆按成员确认`,
      ]);
    }
    const chosen = companions.find((item) => item.id === personaId);
    items.push(["伙伴", chosen ? `${chosen.name}（${chosen.tagline}）` : "未选择"]);
    return items;
  }, [identityName, mode, form, personaId]);

  const submit = async () => {
    if (!mode || !personaId) return;
    setSubmitting(true);
    setError("");
    try {
      const manifest = await onCreateBinding(
        buildDeviceBindingRequest({
          mode,
          form,
          consents,
          personaId,
          ownerPersonId,
          claimToken,
        }),
      );
      setResult(manifest);
      setStep("done");
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "绑定没有完成，请稍后重试。",
      );
    } finally {
      setSubmitting(false);
    }
  };

  const parentAcceptanceOffers =
    mode === "child_for_parent"
      ? consentOffersFor(mode).filter((offer) => offer.requiresParentSelfAcceptance)
      : [];
  const selectableOffers = mode
    ? consentOffersFor(mode).filter((offer) => !offer.requiresParentSelfAcceptance)
    : [];
  const claimTokenReady = Boolean(claimToken.trim());

  return (
    <section
      ref={screenRef}
      className="screen device-bind-screen"
      aria-label={stepLabel(step)}
    >
      <header className="topbar page-topbar">
        <button
          type="button"
          className="icon-button"
          aria-label="返回"
          onClick={() => {
            if (step === "form" || step === "confirm") {
              setStep(step === "confirm" ? "form" : "mode");
              setError("");
            } else if (step === "mode") {
              setStep("claim");
              setError("");
            } else {
              onBack();
            }
          }}
        >
          <ArrowLeft size={20} weight="bold" aria-hidden="true" />
        </button>
        <div>
          <p className="eyebrow">首次绑定 · {stepLabel(step)}</p>
          <h1>这台机器人主要给谁使用？</h1>
        </div>
      </header>

      {step === "claim" && (
        <div className="device-bind-body">
          <p className="device-bind-intro">
            扫描机器人底部或包装上的二维码；没有二维码时也可以手动输入设备码。
          </p>
          <div className="bind-field">
            <label htmlFor="device-claim-token">设备码</label>
            <input
              id="device-claim-token"
              value={claimToken}
              maxLength={128}
              autoComplete="off"
              placeholder="例如 dev-01J…"
              onChange={(event) => setClaimToken(event.target.value)}
            />
          </div>
          {error && <p className="inline-error" role="alert">{error}</p>}
          <button
            type="button"
            className="button-primary bind-next"
            disabled={!claimTokenReady}
            onClick={() => {
              setError("");
              setStep("mode");
            }}
          >
            继续
          </button>
        </div>
      )}

      {step === "mode" && (
        <div className="device-bind-body">
          <p className="device-bind-intro">
            选择后会初始化设备用途、主体关系与默认能力；运行时仍按当前说话人
            和安全策略动态决策，不会永久锁死。
          </p>
          <div className="device-mode-list" role="radiogroup" aria-label="主要使用方式">
            {MODE_CARDS.map((card) => (
              <button
                type="button"
                key={card.mode}
                role="radio"
                aria-checked={mode === card.mode}
                className="device-mode-card"
                onClick={() => goToForm(card.mode)}
              >
                <span className="device-mode-title">{card.title}</span>
                <span className="device-mode-tagline">{card.tagline}</span>
                <span className="device-mode-description">{card.description}</span>
                <CaretRight size={18} weight="bold" aria-hidden="true" />
              </button>
            ))}
          </div>
        </div>
      )}

      {step === "form" && mode && form && (
        <div className="device-bind-body">
          {mode === "parent_for_child" && (
            <>
              <div className="bind-field">
                <label htmlFor="child-nickname">孩子的昵称</label>
                <input
                  id="child-nickname"
                  value={form.childNickname}
                  maxLength={24}
                  onChange={(event) =>
                    setForm({ ...form, childNickname: event.target.value })
                  }
                />
                <fieldset className="bind-field">
                  <legend>年龄段（仅用于默认体验文案）</legend>
                  {MODE_AGE_BANDS.parent_for_child.map((band) => (
                    <label key={band} className="bind-option">
                      <input
                        type="radio"
                        name="child-age-band"
                        checked={form.childAgeBand === band}
                        onChange={() => setForm({ ...form, childAgeBand: band })}
                      />
                      {AGE_BAND_LABELS[band]}
                    </label>
                  ))}
                </fieldset>
              </div>
              <fieldset className="bind-field">
                <legend>学习与服务偏好</legend>
                <label className="bind-option">
                  <input
                    type="checkbox"
                    checked={form.tutorEnabled}
                    onChange={(event) =>
                      setForm({ ...form, tutorEnabled: event.target.checked })
                    }
                  />
                  允许学习陪练
                </label>
                <label className="bind-option">
                  <input
                    type="checkbox"
                    checked={form.englishPracticeEnabled}
                    onChange={(event) =>
                      setForm({
                        ...form,
                        englishPracticeEnabled: event.target.checked,
                      })
                    }
                  />
                  允许英语口语陪练
                </label>
                <label className="bind-option">
                  单次使用时长（分钟）
                  <select
                    value={form.maxSessionMinutes}
                    onChange={(event) =>
                      setForm({
                        ...form,
                        maxSessionMinutes: Number(event.target.value),
                      })
                    }
                  >
                    {[15, 30, 45, 60, 90, 120].map((minutes) => (
                      <option key={minutes} value={minutes}>
                        {minutes}
                      </option>
                    ))}
                  </select>
                </label>
              </fieldset>
            </>
          )}

          {mode === "self_use" && (
            <>
              <div className="bind-field bind-readonly-field">
                <label htmlFor="self-nickname">怎么称呼你？</label>
                <input
                  id="self-nickname"
                  value={form.displayName}
                  readOnly
                  aria-describedby="self-nickname-note"
                />
                <small id="self-nickname-note">
                  称呼只在注册时设置，这里不会重新编辑。
                </small>
              </div>
              <fieldset className="bind-field">
                <legend>记忆深度</legend>
                <label className="bind-option">
                  <input
                    type="radio"
                    name="self-memory"
                    checked={form.memoryLevel === "personal"}
                    onChange={() => setForm({ ...form, memoryLevel: "personal" })}
                  />
                  完整个人记忆（分项授权）
                </label>
                <label className="bind-option">
                  <input
                    type="radio"
                    name="self-memory"
                    checked={form.memoryLevel === "none"}
                    onChange={() => setForm({ ...form, memoryLevel: "none" })}
                  />
                  仅当前对话，不长期记忆
                </label>
              </fieldset>
              <fieldset className="bind-field">
                <legend>主动采访频率</legend>
                <label className="bind-option">
                  <input
                    type="radio"
                    name="self-interview"
                    checked={form.interviewFrequency === "off"}
                    onChange={() => setForm({ ...form, interviewFrequency: "off" })}
                  />
                  不主动采访
                </label>
                <label className="bind-option">
                  <input
                    type="radio"
                    name="self-interview"
                    checked={form.interviewFrequency === "low"}
                    onChange={() => setForm({ ...form, interviewFrequency: "low" })}
                  />
                  偶尔主动问候
                </label>
                <label className="bind-option">
                  <input
                    type="radio"
                    name="self-interview"
                    checked={form.interviewFrequency === "medium"}
                    onChange={() => setForm({ ...form, interviewFrequency: "medium" })}
                  />
                  适度主动采访
                </label>
              </fieldset>
            </>
          )}

          {mode === "child_for_parent" && (
            <>
              <div className="bind-field">
                <label htmlFor="parent-nickname">父母本人怎么称呼？</label>
                <input
                  id="parent-nickname"
                  value={form.parentNickname}
                  maxLength={24}
                  onChange={(event) =>
                    setForm({ ...form, parentNickname: event.target.value })
                  }
                />
              </div>
              <fieldset className="bind-field">
                <legend>服务偏好</legend>
                <label className="bind-option">
                  语速
                  <select
                    value={form.speechSpeed}
                    onChange={(event) =>
                      setForm({ ...form, speechSpeed: event.target.value })
                    }
                  >
                    <option value="slow">慢速清晰</option>
                    <option value="normal">正常语速</option>
                  </select>
                </label>
                <label className="bind-option">
                  子女可见范围
                  <select
                    value={form.adminVisibility}
                    onChange={(event) =>
                      setForm({ ...form, adminVisibility: event.target.value })
                    }
                  >
                    <option value="device_status">只看设备状态与提醒</option>
                    <option value="device_status_and_reminders">
                      设备状态 + 周报聚合
                    </option>
                  </select>
                </label>
              </fieldset>
              <div className="bind-notice" role="note">
                子女可以管理设备，但不能默认读取父母的私人内容；父母本人接受
                后才能开启本人记录。
              </div>
            </>
          )}

          {mode === "family_shared" && (
            <>
              <label className="bind-option">
                <input
                  type="checkbox"
                  checked={form.sharedPersonaEnabled}
                  onChange={(event) =>
                    setForm({ ...form, sharedPersonaEnabled: event.target.checked })
                  }
                />
                <span>
                  <strong>家庭共享伙伴</strong>
                  <small>家庭共用同一伙伴人格，成员各自保留独立档案</small>
                </span>
              </label>
              <div className="bind-notice" role="note">
                绑定后可在「设备与成员」中邀请/添加成员；每位成员拥有独立
                主体档案，私人记忆与家庭共享记忆分区管理。
              </div>
            </>
          )}

          <fieldset className="bind-field">
            <legend>同意要约（可代为确认的项目）</legend>
            {selectableOffers.map((offer) => (
              <label className="bind-option" key={offer.id}>
                <input
                  type="checkbox"
                  checked={consents[offer.id] === true}
                  onChange={(event) =>
                    setConsents({ ...consents, [offer.id]: event.target.checked })
                  }
                />
                <span>
                  <strong>{offer.label}</strong>
                  <small>{offer.description}</small>
                </span>
              </label>
            ))}
          </fieldset>

          {parentAcceptanceOffers.length > 0 && (
            <fieldset className="bind-field bind-parent-pending">
              <legend>需父母本人在设备上确认（本次不会代为同意）</legend>
              {parentAcceptanceOffers.map((offer) => (
                <div className="bind-parent-item" key={offer.id}>
                  <span>
                    <strong>{offer.label}</strong>
                    <small>{offer.description}</small>
                  </span>
                  <em>待父母确认</em>
                </div>
              ))}
            </fieldset>
          )}

          <fieldset className="bind-field">
            <legend>选择伙伴</legend>
            <div className="bind-persona-list" role="radiogroup" aria-label="伙伴">
              {companions.map((item) => (
                <button
                  type="button"
                  role="radio"
                  aria-checked={personaId === item.id}
                  key={item.id}
                  className="bind-persona-option"
                  onClick={() => setPersonaId(item.id)}
                >
                  <span className="bind-persona-name">{item.name}</span>
                  <small>{item.tagline}</small>
                </button>
              ))}
            </div>
          </fieldset>

          {error && <p className="inline-error" role="alert">{error}</p>}
          <button
            type="button"
            className="button-primary bind-next"
            disabled={!formComplete || !personaId}
            onClick={() => setStep("confirm")}
          >
            查看确认摘要
          </button>
        </div>
      )}

      {step === "confirm" && mode && (
        <div className="device-bind-body">
          <section className="bind-summary" aria-labelledby="bind-summary-title">
            <h2 id="bind-summary-title">确认摘要</h2>
            {summaryItems.map(([label, value]) => (
              <div className="bind-summary-row" key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
              </div>
            ))}
            <div className="bind-summary-row">
              <span>同意要约</span>
              <strong>
                {collectConsentIds(mode, consents).length > 0
                  ? `${collectConsentIds(mode, consents).length} 项已选择`
                  : "未选择可代确认项目"}
              </strong>
            </div>
            {parentAcceptanceOffers.length > 0 && (
              <p className="bind-summary-pending" role="note">
                {parentAcceptanceOffers.length} 项需要父母本人在设备上确认，
                本次不会代为同意；设备绑定后可继续完成。
              </p>
            )}
          </section>
          {error && <p className="inline-error" role="alert">{error}</p>}
          <button
            type="button"
            className="button-primary bind-next"
            disabled={submitting}
            aria-busy={submitting || undefined}
            onClick={() => void submit()}
          >
            {submitting ? "正在提交绑定…" : "确认并绑定设备"}
          </button>
        </div>
      )}

      {step === "done" && result && (
        <div className="device-bind-body">
          <div className="bind-success" role="status">
            <span className="bind-success-icon" aria-hidden="true">
              <Check size={26} weight="bold" />
            </span>
            <h2>绑定完成</h2>
            <p>
              {MODE_TITLES[result.declared_mode]}已建立（版本 v
              {result.binding_version}）；能力与记忆规则由服务端按当前主体
              签发，敏感功能只会在确认使用人后开放。
            </p>
            {mode === "family_shared" && (
              <p className="bind-summary-pending" role="note">
                成员邀请是绑定后的下一步：可在「设备与成员」中邀请/添加成员。
              </p>
            )}
            {parentAcceptanceOffers.length > 0 && (
              <p className="bind-summary-pending" role="note">
                父母本人确认项仍待设备端完成。
              </p>
            )}
          </div>
          <button
            type="button"
            className="button-primary bind-next"
            onClick={() => onComplete(result)}
          >
            <Sparkle size={18} weight="fill" aria-hidden="true" />
            前往设备与成员
          </button>
        </div>
      )}
    </section>
  );
}
