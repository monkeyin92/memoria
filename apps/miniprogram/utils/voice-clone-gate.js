/**
 * Delivered capabilities open voice clone only with a literal boolean true.
 * A missing payload, a string, or a number keeps every surface shut.
 */
function isVoiceCloneOpen(deliveredCapabilities) {
  return deliveredCapabilities != null && deliveredCapabilities.voice_clone_enabled === true;
}

module.exports = { isVoiceCloneOpen };
