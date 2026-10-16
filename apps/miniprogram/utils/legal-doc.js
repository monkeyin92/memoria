const AGREEMENT_PATH = "/legal/user-agreement.md";
const AGREEMENT_PENDING = "用户协议正在定稿";

function agreementParagraphs(markdown) {
  if (typeof markdown !== "string") return [];
  const marker = "## 正文\n";
  const start = markdown.indexOf(marker);
  if (start === -1) return [];
  let rest = markdown.slice(start + marker.length);
  if (rest.startsWith("\n")) rest = rest.slice(1);
  const end = rest.search(/\n## /);
  const body = (end === -1 ? rest : rest.slice(0, end)).trim();
  if (!body) return [];
  return body.split(/\n\n+/).map((paragraph) => paragraph.trim()).filter(Boolean);
}

function presentUserAgreement(markdown) {
  const paragraphs = agreementParagraphs(markdown);
  if (!paragraphs.length) return { pending: true, notice: AGREEMENT_PENDING, paragraphs: [] };
  return { pending: false, notice: "", paragraphs };
}

function readPackagedText(filePath) {
  try {
    const manager = typeof wx !== "undefined" && wx.getFileSystemManager && wx.getFileSystemManager();
    if (!manager || typeof manager.readFileSync !== "function") return "";
    const data = manager.readFileSync(filePath, "utf8");
    return typeof data === "string" ? data : "";
  } catch {
    return "";
  }
}

function loadUserAgreement() {
  return presentUserAgreement(readPackagedText(AGREEMENT_PATH));
}

module.exports = {
  AGREEMENT_PATH,
  AGREEMENT_PENDING,
  agreementParagraphs,
  loadUserAgreement,
  presentUserAgreement,
};
