const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const { releaseChip } = require("../../utils/legal-release");
const paragraphs = require("./paragraphs");

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    paragraphs,
    releaseChip: releaseChip(),
  },

  goBack() {
    leavePage("/pages/home/index");
  },
});
