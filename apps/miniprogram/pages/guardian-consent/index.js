const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const { releaseChip } = require("../../utils/legal-release");
const paragraphs = require("./paragraphs");

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    paragraphs,
    // 同意书正文还写着「待律师审核，还不是已经生效的同意书」，所以它不跟着用户协议的生效日期走。
    releaseChip: releaseChip(""),
  },

  goBack() {
    leavePage("/pages/home/index");
  },
});
