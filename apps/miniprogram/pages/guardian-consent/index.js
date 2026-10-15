const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const paragraphs = require("./paragraphs");

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    paragraphs,
  },

  goBack() {
    leavePage("/pages/home/index");
  },
});
