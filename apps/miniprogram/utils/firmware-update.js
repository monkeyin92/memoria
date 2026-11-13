const ACTIVE = new Set(["waiting_device", "checking", "downloading", "restarting"]);
const LABELS = {
  waiting_device: "等待设备空闲后检查更新",
  checking: "设备正在检查更新",
  downloading: "设备正在下载更新（根据下载请求推断）",
  restarting: "下载完成，等待设备重启",
  succeeded: "更新成功，设备已回报新版本",
};
const FAILURES = { timeout: "更新等待超时，请确认设备在线后重试", rolled_back: "设备已回滚，请重试", download_interrupted: "下载中断，请重试", check_failed: "检查更新失败，请稍后重试", device_declined: "设备未接受更新，请重试" };
function firmwareView(payload) {
  const build = payload?.running?.build;
  const available = payload?.available;
  const recorded = payload?.update;
  const update = recorded && !(available?.build > recorded.target_build || (build >= recorded.target_build && recorded.state === "failed")) ? recorded : null;
  const active = ACTIVE.has(update?.state);
  let text = update?.state === "failed" ? FAILURES[update.failure] || "更新失败，请重试" : LABELS[update?.state];
  if (!text) text = build == null ? available ? "尚未收到设备版本，不能确认是否最新" : "暂无可用固件发布；尚未收到设备版本" : !available ? "暂无可用固件发布" : available.build <= build ? "已是最新版本" : `可更新至 build ${available.build}`;
  if (active && build == null) text = "旧固件无法立即触发，请重启机器人或等待自动检查";
  if (!active && payload?.presence === "offline") text += "；设备离线，请先连接设备";
  return { firmwareStatus: payload, firmwareText: text, firmwareBuild: build == null ? "未知" : `build ${build}`, firmwareActive: active };
}
module.exports = { firmwareView, ACTIVE };
