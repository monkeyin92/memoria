/**
 * 操作详情页面
 * 查看单个解绑/撤回/续期操作的完整状态与结果
 */

Page({
  data: {
    isDraft: true,
    draftLabel: "内部交互草稿 · 仅使用虚构测试数据 · 不构成真实同意",
    contractVersion: "B1-contract-v1",

    operationId: null,
    operation: null,

    // Mock 操作数据
    mockOperations: {
      op_mock_001: {
        operationId: "op_mock_001",
        action: "unbind_device",
        actionLabel: "设备解绑",
        status: "completed",
        acceptedAt: "2026-10-18T08:00:00Z",
        accessStoppedAt: "2026-10-18T08:05:00Z",
        completedAt: "2026-10-18T08:10:00Z",
        phase: "completed",
        bindingStatus: {
          status: "revoked",
          bindingId: "bd_mock_001",
          revokedAt: "2026-10-18T08:10:00Z",
        },
        memoryAccessStatus: {
          status: "stopped",
          scopeStops: [
            {
              scopeId: "scope_memory_retention",
              accessStatus: "stopped",
              stoppedAt: "2026-10-18T08:05:00Z",
            },
          ],
        },
        custodyStatus: {
          status: "active",
          grants: [
            {
              grantId: "grant_mock_001",
              status: "active",
              effectiveAt: "2026-10-18T08:00:00Z",
              expiresAt: "2027-04-18T08:00:00Z",
            },
          ],
        },
        childLayersPurged: "completed",
        subjectDeletion: "not_requested",
        consentsWithdrawn: 1,
        domains: [
          {
            domain: "dialogue_summary",
            status: "purged",
            message: "近期摘要已清除",
          },
          {
            domain: "child_archive",
            status: "purged",
            message: "长期档案已清除",
          },
        ],
        chatTransitionStatus: "ephemeral_ready",
        message: "设备解绑已完成，保管已生效。",
      },
      op_mock_002: {
        operationId: "op_mock_002",
        action: "renew_custody",
        actionLabel: "续期保管",
        status: "in_progress",
        acceptedAt: "2026-10-17T15:00:00Z",
        phase: "access_stopped",
        custodyStatus: {
          status: "pending",
          grants: [
            {
              grantId: "grant_mock_002",
              status: "pending",
              effectiveAt: "2026-10-17T15:00:00Z",
              expiresAt: "2027-04-17T15:00:00Z",
            },
          ],
        },
        message: "续期请求已受理，正在处理中。",
      },
      op_mock_003: {
        operationId: "op_mock_003",
        action: "withdraw_memory",
        actionLabel: "撤回记忆",
        status: "in_progress",
        acceptedAt: "2026-10-18T10:30:00Z",
        accessStoppedAt: "2026-10-18T10:31:00Z",
        phase: "deleting",
        memoryAccessStatus: {
          status: "stopping",
          scopeStops: [
            {
              scopeId: "scope_memory_retention",
              accessStatus: "stopping",
            },
          ],
        },
        message: "请求已受理，正在停止本次范围内的记忆使用。",
      },
    },
  },

  onLoad(options) {
    const operationId = options.operationId;
    this.setData({ operationId });

    const operation = this.data.mockOperations[operationId];
    if (operation) {
      this.setData({ operation });
    } else {
      wx.showToast({
        title: "未找到操作",
        icon: "error",
      });
      setTimeout(() => {
        wx.navigateBack();
      }, 1500);
    }
  },

  retryOperation() {
    wx.showToast({
      title: "[Mock] 重试请求已提交",
      icon: "success",
    });
  },

  goBack() {
    wx.navigateBack();
  },
});
