"use strict";

/* A short-lived in-memory byte buffer.  It is never passed to wx storage,
 * request data, telemetry, or error objects. */
class SensitiveBuffer {
  constructor() {
    this._bytes = null;
  }

  setText(value) {
    this.clear();
    if (typeof value !== "string") throw new TypeError("敏感输入必须是字符串");
    const encoded = unescape(encodeURIComponent(value));
    this._bytes = new Uint8Array(encoded.length);
    for (let index = 0; index < encoded.length; index += 1) {
      this._bytes[index] = encoded.charCodeAt(index);
    }
  }

  getText() {
    if (!this._bytes) return "";
    let binary = "";
    for (const byte of this._bytes) binary += String.fromCharCode(byte);
    try {
      return decodeURIComponent(
        [...binary].map((character) => `%${character.charCodeAt(0).toString(16).padStart(2, "0")}`).join(""),
      );
    } catch {
      return "";
    }
  }

  clear() {
    if (this._bytes) this._bytes.fill(0);
    this._bytes = null;
  }

  get length() {
    return this._bytes?.byteLength || 0;
  }
}

module.exports = {
  SensitiveBuffer,
};
