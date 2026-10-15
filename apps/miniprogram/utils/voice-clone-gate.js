function isVoiceCloneOpen(deliveredCapabilities) {
  return deliveredCapabilities != null && deliveredCapabilities.voice_clone_enabled === true;
}

module.exports = { isVoiceCloneOpen };
