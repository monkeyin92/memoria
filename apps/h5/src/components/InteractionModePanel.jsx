import {
  Brain,
  ChatTeardropDots,
  Notebook,
  Sparkle,
  UserCircle,
} from "@phosphor-icons/react";

import { companions } from "../lib/companions.js";

const modeCopy = {
  companion: {
    title: "陪伴模式",
    description: "由你选择的轻量陪伴方式负责倾听、追问和整理。它不会定义你是谁。",
    action: "更换陪伴方式",
    Icon: ChatTeardropDots,
  },
  archive: {
    title: "档案模式",
    description: "查看已经保存的人生记忆和来源，不启动语音，也不模拟你的回答。",
    action: "打开生活档案",
    Icon: Notebook,
  },
  self_preview: {
    title: "数字自我预览",
    description: "使用经本人确认的数字分身版本回答，并允许逐句纠正“不像我”的内容。",
    blocked: "需要先建立并批准数字分身版本",
    Icon: Brain,
  },
  legacy: {
    title: "传承模式",
    description: "让获授权的亲友与冻结版本对话；本人核心、关系范围和身份披露均受约束。",
    blocked: "需要冻结版本、关系档案和传承授权",
    Icon: UserCircle,
  },
};

const orderedModes = ["companion", "archive", "self_preview", "legacy"];

function availabilityOf(capabilities, mode) {
  const value = capabilities?.modes?.[mode];
  return value?.status === "available" ? "available" : "blocked";
}

export function InteractionModePanel({
  capabilities,
  activeVersion,
  learnedTraitCount = 0,
  onOpenArchive,
  onChangeCompanion,
}) {
  const actions = {
    companion: onChangeCompanion,
    archive: onOpenArchive,
  };
  const selectedCompanionId = capabilities?.selected_companion_id;
  const selectedCompanionName = selectedCompanionId
    ? companions.find((companion) => companion.id === selectedCompanionId)?.name || selectedCompanionId
    : "尚未同步";

  return (
    <>
      <section className="digital-section interaction-mode-section" aria-labelledby="interaction-mode-title">
        <div className="digital-section-heading">
          <span className="digital-section-icon"><Sparkle size={22} weight="fill" /></span>
          <div>
            <p>一个入口，四种明确边界</p>
            <h2 id="interaction-mode-title">互动模式</h2>
          </div>
          <span className="digital-status" data-status="active">服务端约束</span>
        </div>
        <p className="digital-explainer">
          陪伴者有稳定但克制的工作风格；数字分身从你的原话、确认和纠正中独立成长。更换陪伴方式不会改写数字分身。
        </p>

        <div className="interaction-mode-grid">
          {orderedModes.map((mode) => {
            const copy = modeCopy[mode];
            const status = availabilityOf(capabilities, mode);
            const available = status === "available";
            const action = actions[mode];
            const Icon = copy.Icon;
            return (
              <article className="interaction-mode-card" data-status={status} key={mode}>
                <div className="interaction-mode-card-title">
                  <span><Icon size={20} weight="fill" aria-hidden="true" /></span>
                  <h3>{copy.title}</h3>
                  <small>{available ? "可用" : "建设中"}</small>
                </div>
                <p>{copy.description}</p>
                {available && action ? (
                  <button type="button" className="button-quiet" onClick={action}>
                    {copy.action}
                  </button>
                ) : (
                  <p className="interaction-mode-blocked">{copy.blocked || "服务端状态暂不可用"}</p>
                )}
              </article>
            );
          })}
        </div>
      </section>

      <section className="digital-section growth-boundary-section" aria-labelledby="growth-boundary-title">
        <div className="digital-section-heading">
          <span className="digital-section-icon"><Sparkle size={22} weight="fill" /></span>
          <div>
            <p>两条成长线，彼此隔离</p>
            <h2 id="growth-boundary-title">陪伴适应与数字自我</h2>
          </div>
        </div>
        <div className="growth-boundary-grid">
          <article>
            <span>它怎样陪你</span>
            <strong>默认陪伴方式：{selectedCompanionName}</strong>
            <small>更换后从下一次会话生效</small>
            <p>这里只调整回应、追问和访谈方式；伙伴说的话不会成为你的性格证据。</p>
          </article>
          <article>
            <span>数字分身怎样成长</span>
            <strong>
              {activeVersion
                ? `已积累人格材料 v${activeVersion.version_number}`
                : `已积累 ${learnedTraitCount} 条确认材料`}
            </strong>
            <p>这些仍是学习材料，不是已批准的数字分身版本，也不会冒充你。</p>
          </article>
        </div>
      </section>
    </>
  );
}
