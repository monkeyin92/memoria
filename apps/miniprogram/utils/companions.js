// 伙伴目录。人格与设计音色一一对应；角色名只用于识别，不替代设备上的对话。
const companions = Object.freeze([
  {
    id: "starlight",
    name: "星澜",
    tagline: "温和清晰，留一点想象",
    description: "回应有温度，也会偶尔陪你把想法再理清一层。",
    voiceId: "warm_companion",
    voiceName: "暖阳青年",
    tone: "清澈、自然",
  },
  {
    id: "taoxi",
    name: "桃喜",
    tagline: "明朗轻快，分享小乐趣",
    description: "适合轻松说说当天的事，回复更短、更轻快。",
    voiceId: "bright_peer",
    voiceName: "元气搭子",
    tone: "明亮、轻盈",
  },
  {
    id: "mianmian",
    name: "绵绵",
    tagline: "柔和耐心，不催促回应",
    description: "不急着追问，等你愿意时再陪你聊得更深。",
    voiceId: "soft_confidante",
    voiceName: "温柔知己",
    tone: "轻柔、舒缓",
  },
  {
    id: "axu",
    name: "阿序",
    tagline: "条理分明，认真听你说",
    description: "帮你把信息整理清楚，但不会替你做决定。",
    voiceId: "calm_guide",
    voiceName: "沉稳向导",
    tone: "平稳、利落",
  },
  {
    id: "xuanmo",
    name: "玄墨",
    tagline: "沉静克制，回应有余地",
    description: "不主动深挖，只有你邀请时才进入更深的话题。",
    voiceId: "low_magnetic",
    voiceName: "低音笃定",
    tone: "低沉、从容",
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
