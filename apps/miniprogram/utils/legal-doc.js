const AGREEMENT_PATH = "/legal/user-agreement.md";
const AGREEMENT_PENDING = "用户协议正在定稿";
const AGREEMENT_PENDING_MARKER = "<!-- agreement: pending -->";
const CONSENT_AGREEMENT_HINT = "用户协议正在定稿，定稿后才能勾选同意";

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
  const source = typeof markdown === "string" ? markdown : "";
  const markedPending = source.includes(AGREEMENT_PENDING_MARKER);
  const paragraphs = markedPending ? [] : agreementParagraphs(source);
  const pending = markedPending || paragraphs.length === 0;
  return {
    pending,
    notice: pending ? AGREEMENT_PENDING : "",
    paragraphs: pending ? [] : paragraphs,
  };
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
  AGREEMENT_PENDING_MARKER,
  CONSENT_AGREEMENT_HINT,
  agreementParagraphs,
  loadUserAgreement,
  presentUserAgreement,
};
