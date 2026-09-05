// 伙伴目录。人格与设计音色一一对应；小程序只展示名称与声音，不展示机器人形象。
const companions = Object.freeze([
  {
    id: "starlight",
    name: "星澜",
    tagline: "温暖回应 · 适度追问",
    description: "回应有温度，也会偶尔陪你把想法再理清一层。",
    voiceId: "warm_companion",
    voiceName: "暖阳青年",
  },
  {
    id: "taoxi",
    name: "桃喜",
    tagline: "轻快回应 · 少量追问",
    description: "适合轻松说说当天的事，回复更短、更轻快。",
    voiceId: "bright_peer",
    voiceName: "元气搭子",
  },
  {
    id: "mianmian",
    name: "绵绵",
    tagline: "慢慢倾听 · 留出空间",
    description: "不急着追问，等你愿意时再陪你聊得更深。",
    voiceId: "soft_confidante",
    voiceName: "温柔知己",
  },
  {
    id: "axu",
    name: "阿序",
    tagline: "直接梳理 · 清晰陪伴",
    description: "帮你把信息整理清楚，但不会替你做决定。",
    voiceId: "calm_guide",
    voiceName: "沉稳向导",
  },
  {
    id: "xuanmo",
    name: "玄墨",
    tagline: "克制回应 · 保留留白",
    description: "不主动深挖，只有你邀请时才进入更深的话题。",
    voiceId: "low_magnetic",
    voiceName: "低音笃定",
  },
]);

const defaultCompanionId = "starlight";

function companionById(companionId) {
  return companions.find((item) => item.id === companionId) || companions[0];
}

module.exports = {
  companions,
  defaultCompanionId,
  companionById,
};
