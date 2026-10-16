const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const { loadUserAgreement } = require("../../utils/legal-doc");

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    ...loadUserAgreement(),
  },

  onShow() {
    this.setData(loadUserAgreement());
  },

  goBack() {
    leavePage("/pages/home/index");
  },
});
