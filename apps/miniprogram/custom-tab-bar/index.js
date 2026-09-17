Component({
  properties: {
    selected: { type: Number, value: 0 },
  },
  data: {
    tabs: [
      {
        pagePath: "/pages/home/index",
        text: "首页",
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 22V12h6v10"/></svg>',
      },
      {
        pagePath: "/pages/memory/index",
        text: "回顾",
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
      },
      {
        pagePath: "/pages/companion/index",
        text: "伙伴",
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>',
      },
      {
        pagePath: "/pages/device/index",
        text: "设备",
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2"/><path d="M12 18h.01"/></svg>',
      },
      {
        pagePath: "/pages/profile/index",
        text: "我的",
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
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
