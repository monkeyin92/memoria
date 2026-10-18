/**
 * 记忆生命周期管理页面
 * 内部交互草稿 · 仅使用虚构测试数据
 * F01 - mock implementation with v2 contract
 */

Page({
  data: {
    isDraft: true,
    draftLabel: "内部交互草稿 · 仅使用虚构测试数据 · 不构成真实同意",

    // 生命周期契约版本
    contractVersion: "B1-contract-v1",

    // 保管政策状态：unconfigured | configured
    policyStatus: "configured",
    policyVersion: "B-policy-v1-2026",
    policyHash: "sha256:abc123def456",

    // 主要场景：展示三种状态
    // 1. 有效保管 + 可续期
    // 2. 期限未配置（禁用新保管和续期）
    // 3. 已撤回 / 进行中

    scenario: "active", // active | unconfigured | withdrawn | deleting

    // === 场景 1: 有效保管 ===
    activeScenario: {
      grants: [
        {
          grantId: "grant_mock_001",
          purposeScopeId: "scope_archive_retention",
          revision: 2,
          status: "active", // active | pending | superseded | revoked | expired | failed
          effectiveAt: "2026-10-18T08:00:00Z",
          expiresAt: "2027-04-18T08:00:00Z", // 6 months from unbind
          daysTillExpiry: 183,
          errorCode: null,
        },
      ],
      policyStatus: "configured",
      canRequestNewGrant: true,
      canRenew: true,
      accessStatus: "scoped_active",
      chatStatus: "ephemeral_ready",
      chatLabel: "可继续不留存对话",
    },

    // === 场景 2: 期限未配置 ===
    unconfiguredScenario: {
      grants: [],
      policyStatus: "unconfigured",
      canRequestNewGrant: false,
      canRenew: false,
      message:
        "保管期限尚未配置，暂不能申请新保管或续期。仍可解绑设备、撤回已有保管并查看处理进度。",
    },

    // === 场景 3: 已撤回 ===
    withdrawnScenario: {
      grants: [
        {
          grantId: "grant_mock_002",
          purposeScopeId: "scope_archive_retention",
          revision: 1,
          status: "revoked",
          effectiveAt: "2026-09-20T08:00:00Z",
          revokedAt: "2026-10-18T10:30:00Z",
          errorCode: null,
        },
      ],
      operationInProgress: {
        operationId: "op_mock_003",
        action: "withdraw_memory",
        status: "in_progress", // pending | in_progress | completed | failed
        phase: "deleting", // accepted | access_stopped | deleting | completed
        acceptedAt: "2026-10-18T10:30:00Z",
        accessStoppedAt: "2026-10-18T10:31:00Z",
        message: "请求已受理，正在停止本次范围内的记忆使用。",
      },
    },

    // === 操作历史 ===
    operations: [
      {
        operationId: "op_mock_001",
        action: "unbind_device",
        status: "completed",
        completedAt: "2026-10-18T08:00:00Z",
        bindingStatus: "revoked",
        custodyStatus: "active", // 本次操作摘要：无申请 / 待处理 / 成功 / 失败
        summary: "设备解绑 · 已完成",
      },
      {
        operationId: "op_mock_002",
        action: "renew_custody",
        status: "pending",
        acceptedAt: "2026-10-17T15:00:00Z",
        custodyStatus: "pending",
        summary: "续期保管 · 进行中",
      },
    ],

    // 当前活跃场景的详情
    activeGrant: null,
    showCustodyDetail: false,
    showOperationHistory: false,
  },

  onLoad() {
    // 默认显示"有效保管"场景
    this.switchScenario("active");
  },

  switchScenario(scenario) {
    this.setData({ scenario });
    if (scenario === "active") {
      this.setData({
        policyStatus: this.data.activeScenario.policyStatus,
        grants: this.data.activeScenario.grants,
        canRequestNewGrant: this.data.activeScenario.canRequestNewGrant,
        canRenew: this.data.activeScenario.canRenew,
      });
    } else if (scenario === "unconfigured") {
      this.setData({
        policyStatus: this.data.unconfiguredScenario.policyStatus,
        grants: this.data.unconfiguredScenario.grants,
        message: this.data.unconfiguredScenario.message,
      });
    } else if (scenario === "withdrawn") {
      this.setData({
        grants: this.data.withdrawnScenario.grants,
        currentOperation: this.data.withdrawnScenario.operationInProgress,
      });
    }
  },

  // 查看保管详情
  showCustodyDetail(e) {
    const grantId = e.currentTarget.dataset.grantId;
    const grant =
      this.data.grants && this.data.grants.find((g) => g.grantId === grantId);
    if (grant) {
      this.setData({
        activeGrant: grant,
        showCustodyDetail: true,
      });
    }
  },

  closeCustodyDetail() {
    this.setData({ showCustodyDetail: false });
  },

  // 续期保管
  renewCustody() {
    wx.showToast({
      title: "[Mock] 续期请求已提交",
      icon: "success",
    });
    this.setData({ scenario: "withdrawn" });
  },

  // 撤回保管
  withdrawCustody() {
    wx.showModal({
      title: "撤回保管",
      content: "撤回这项保管后，将停止本项保管用途，并处理相应内容的删除。其他独立有效用途不在本次撤回范围内。进度可在操作记录查看。",
      confirmText: "撤回",
      cancelText: "取消",
      success: (res) => {
        if (res.confirm) {
          wx.showToast({
            title: "[Mock] 撤回已提交",
            icon: "success",
          });
          this.setData({ scenario: "withdrawn" });
        }
      },
    });
  },

  // 查看操作历史
  toggleOperationHistory() {
    this.setData({
      showOperationHistory: !this.data.showOperationHistory,
    });
  },

  // 查看操作详情
  showOperationDetail(e) {
    const operationId = e.currentTarget.dataset.operationId;
    wx.navigateTo({
      url: `/pages/memory-lifecycle/operation-detail?operationId=${operationId}`,
    });
  },

  openScenarioSelector() {
    const scenarios = ["active", "unconfigured", "withdrawn"];
    const scenarioLabels = [
      "场景 1: 有效保管",
      "场景 2: 期限未配置",
      "场景 3: 已撤回",
    ];
    wx.showActionSheet({
      itemList: scenarioLabels,
      success: (res) => {
        this.switchScenario(scenarios[res.tapIndex]);
      },
    });
  },
});
