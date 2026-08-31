// 伙伴目录。face/chest 坐标用于小程序头像渲染。
// 以吉祥物方画布百分比描述脸屏位置，供 CSS 表情骨架定位。
const companions = Object.freeze([
  {
    id: "starlight",
    name: "星澜",
    tagline: "温暖回应 · 适度追问",
    description: "回应有温度，也会偶尔陪你把想法再理清一层。",
    face: {
      left: "30.3%",
      top: "34.2%",
      width: "39.4%",
      height: "25.4%",
      ink: "#162b4b",
      eyeTop: "#111f3d",
      eyeBottom: "#5bb6ef",
      glow: "rgba(101, 196, 246, 0.7)",
      tone: "light",
    },
    chest: { top: "73.7%" },
  },
  {
    id: "taoxi",
    name: "桃喜",
    tagline: "轻快回应 · 少量追问",
    description: "适合轻松说说当天的事，回复更短、更轻快。",
    face: {
      left: "30.2%",
      top: "39.1%",
      width: "39.6%",
      height: "28.2%",
      ink: "#6a3551",
      eyeTop: "#6a3551",
      eyeBottom: "#d46d9e",
      glow: "rgba(255, 220, 239, 0.82)",
      tone: "pink",
    },
  },
  {
    id: "mianmian",
    name: "绵绵",
    tagline: "慢慢倾听 · 留出空间",
    description: "不急着追问，等你愿意时再陪你聊得更深。",
    face: {
      left: "30.1%",
      top: "35.5%",
      width: "39.8%",
      height: "24.8%",
      ink: "#51354b",
      eyeTop: "#4d3048",
      eyeBottom: "#d583a6",
      glow: "rgba(245, 170, 199, 0.62)",
      tone: "light",
    },
  },
  {
    id: "axu",
    name: "阿序",
    tagline: "直接梳理 · 清晰陪伴",
    description: "帮你把信息整理清楚，但不会替你做决定。",
    face: {
      left: "27.2%",
      top: "35.2%",
      width: "45.6%",
      height: "32.2%",
      ink: "#bdf5ff",
      eyeTop: "#d9fbff",
      eyeBottom: "#20b9f2",
      glow: "rgba(63, 214, 255, 0.78)",
      tone: "dark",
    },
  },
  {
    id: "xuanmo",
    name: "玄墨",
    tagline: "克制回应 · 保留留白",
    description: "不主动深挖，只有你邀请时才进入更深的话题。",
    face: {
      left: "30.1%",
      top: "34.5%",
      width: "39.8%",
      height: "25.8%",
      ink: "#baf5ff",
      eyeTop: "#d8fbff",
      eyeBottom: "#20b7f2",
      glow: "rgba(38, 207, 255, 0.82)",
      tone: "dark",
    },
    chest: { top: "71.8%" },
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
