Component({
  properties: {
    selected: { type: Number, value: 0 },
  },
  // 页面的底部弹窗盖不住 tabBar（它在页面之上的独立层），弹窗打开时由页面把它藏起来。
  data: {
    hidden: false,
    tabs: [
      {
        pagePath: "/pages/home/index",
        text: "首页",
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 10.5 12 4l8.5 6.5V19a1.5 1.5 0 0 1-1.5 1.5h-4v-6h-6v6H5A1.5 1.5 0 0 1 3.5 19z"/></svg>',
      },
      {
        pagePath: "/pages/memory/index",
        text: "回顾",
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5z"/><path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5z"/></svg>',
      },
      {
        pagePath: "/pages/companion/index",
        text: "角色",
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="6" width="16" height="13" rx="5"/><path d="M12 6V3.5"/><circle cx="12" cy="3" r=".6" fill="currentColor"/><path d="M9 12v1M15 12v1M10 16c1.2.8 2.8.8 4 0"/></svg>',
      },
      {
        pagePath: "/pages/device/index",
        text: "设备",
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="3.5" width="14" height="17" rx="4"/><circle cx="12" cy="10" r="3"/><path d="M9.5 16.5h5"/></svg>',
      },
      {
        pagePath: "/pages/profile/index",
        text: "我的",
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8.5" r="4"/><path d="M4.5 20c1.3-3.6 4.2-5.5 7.5-5.5s6.2 1.9 7.5 5.5"/></svg>',
      },
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
