function tab(pagePath, text, icon) {
  return {
    pagePath,
    text,
    icon: `/assets/ui/v4/${icon}-tab-off.svg`,
    iconOn: `/assets/ui/v4/${icon}-tab-on.svg`,
  };
}

Component({
  properties: {
    selected: { type: Number, value: 0 },
  },
  // 页面的底部弹窗盖不住 tabBar（它在页面之上的独立层），弹窗打开时由页面把它藏起来。
  data: {
    hidden: false,
    tabs: [
      tab("/pages/home/index", "首页", "home"),
      tab("/pages/memory/index", "回顾", "book"),
      tab("/pages/companion/index", "角色", "face"),
      tab("/pages/device/index", "设备", "dev"),
      tab("/pages/profile/index", "我的", "user"),
    ],
  },
  methods: {
    onTapTab(event) {
      const index = event.currentTarget.dataset.index;
      const tab = this.data.tabs[index];
      if (!tab) return;
      if (index === this.data.selected) return;
      wx.switchTab({ url: tab.pagePath });
    },
  },
});
