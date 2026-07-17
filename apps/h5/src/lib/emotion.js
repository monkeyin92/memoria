const emotionRules = [
  {
    emotion: "happy",
    terms: [
      "开心", "高兴", "太棒", "喜欢", "谢谢", "哈哈", "顺利",
      "成功", "期待", "幸福", "真好", "恭喜",
    ],
  },
  {
    emotion: "upset",
    terms: [
      "生气", "讨厌", "烦", "愤怒", "不公平", "委屈", "欺负",
      "难受", "气死", "可恶",
    ],
  },
  {
    emotion: "curious",
    terms: [
      "为什么", "怎么", "什么", "不知道", "也许", "可能", "疑惑",
      "好奇", "怎么办", "真的吗", "？", "?",
    ],
  },
];

export function classifyEmotion(text = "") {
  const normalized = text.toLowerCase();
  for (const rule of emotionRules) {
    if (rule.terms.some((term) => normalized.includes(term))) {
      return rule.emotion;
    }
  }
  return "neutral";
}

export const emotionMeta = {
  neutral: { label: "平静", color: "#6c82a8" },
  happy: { label: "开心", color: "#4b86db" },
  curious: { label: "好奇", color: "#8570c8" },
  upset: { label: "有点生气", color: "#c58555" },
};
