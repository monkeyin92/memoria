const companions = Object.freeze([
  {
    id: "starlight",
    name: "Memoria",
    tagline: "统一默认声音",
    description: "所有机器人共用同一个默认声音。表达方式独立设置。",
    voiceId: "warm_companion",
    voiceName: "默认声音",
    tone: "自然、清晰",
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
