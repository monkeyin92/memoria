const MARKER = "[memoria.custom_persona.v1]";
const MAX_NAME = 16;
const MAX_TEXT = 420;

function encodeCustomPersona({ name, text } = {}) {
  const trimmedName = String(name || "")
    .trim()
    .slice(0, MAX_NAME);
  const trimmedText = String(text || "")
    .trim()
    .slice(0, MAX_TEXT);
  if (!trimmedName || !trimmedText) return "";
  return `${MARKER}\nname: ${trimmedName}\n---\n${trimmedText}`;
}

function parseCustomPersona(bio) {
  const raw = String(bio || "");
  if (!raw.startsWith(MARKER)) {
    return { active: false, name: "", text: "" };
  }
  const rest = raw.slice(MARKER.length).replace(/^\n/, "");
  const parts = rest.split("\n---\n");
  const header = parts[0] || "";
  const text = parts.slice(1).join("\n---\n").trim();
  const match = header.match(/^name:\s*(.+)$/m);
  const name = (match ? match[1] : "").trim().slice(0, MAX_NAME);
  if (!name || !text) {
    return { active: false, name: "", text: "" };
  }
  return { active: true, name, text: text.slice(0, MAX_TEXT) };
}

module.exports = {
  MARKER,
  MAX_NAME,
  MAX_TEXT,
  encodeCustomPersona,
  parseCustomPersona,
};
