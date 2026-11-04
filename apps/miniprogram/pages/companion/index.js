const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");

Page({
  data: { statusBarStyle: statusBarStyle() },
  goBack() {
    leavePage("/pages/device/index");
  },
});
