const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const contracts = require("../../utils/multi-subject-contracts");
const { capabilityGateMessage } = require("../../utils/device-binding");

function today() {
  const date = new Date();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function normalizeDay(item) {
  const summary = item.summary || {};
  return {
    date: item.day || item.date || "",
    count: item.message_count || 0,
    title: summary.title || item.title || "这一天的回顾",
    overview: summary.overview || item.overview || item.summary_text || "还没有生成摘要。",
    highlights: summary.highlights || item.highlights || [],
    suggestion: summary.suggestion || item.suggestion || "",
  };
}

/*
 * 实际听到的记录（PR-19）。当前没有可靠 DAC/精确声道证据时服务端不会
 * 下发 approximate=false，因此客户端默认按近似记录展示；只有显式 false
 * 才写成"精确播放记录"。
 */
function normalizeHeardTurn(item) {
  return {
    event_id: item.event_id || "",
    occurred_at: item.occurred_at || "",
    session_id: item.session_id || "",
    turn_id: item.turn_id || "",
    generation_id: item.generation_id || "",
    text: item.text || "",
    approximate: item.approximate !== false,
  };
}

function normalizeMemoryCandidate(item) {
  return {
    claim_id: item.claim_id || "",
    value: item.value || "",
    status: item.status || "",
    reason: item.reason || "",
    domain_category: item.domain_category || "",
    memory_kind: item.memory_kind || "",
    conflict_state: item.conflict_state || "",
  };
}

function normalizeConfirmedMemory(item) {
  return {
    memory_id: item.memory_id || "",
    kind: item.kind || "",
    title: item.title || "",
    snippet: item.snippet || "",
    status: item.status || "",
    domain_category: item.domain_category || "",
    memory_kind: item.memory_kind || "",
    occurred_at: item.occurred_at || "",
  };
}

function normalizeConversationReview(payload) {
  return {
    heardTurns: (payload.actual_heard || [])
      .map(normalizeHeardTurn)
      .filter((item) => Boolean(item.text)),
    memoryCandidates: (payload.memory_candidates || []).map(normalizeMemoryCandidate),
    confirmedMemories: (payload.confirmed_memories || []).map(normalizeConfirmedMemory),
  };
}

/* 私人分区在游客/门禁拒绝/加载失败时统一清空，避免陈旧或越权投影残留。 */
function privatePartitionsClear() {
  return {
    days: [],
    heardTurns: [],
    memoryCandidates: [],
    confirmedMemories: [],
    reviewingClaimId: "",
    pendingCount: 0,
    visibleMemories: [],
    conversationSessions: [],
    conversationSessionsUnavailable: false,
    selectedConversationSessionId: "",
    conversationTurns: [],
    conversationLoading: false,
  };
}

Page({
  data: {
    days: [],
    heardTurns: [],
    memoryCandidates: [],
    confirmedMemories: [],
    reviewingClaimId: "",
      pendingCount: 0,
      visibleMemories: [],
    loading: false,
    summarizing: false,
    error: "",
    selectedDate: today(),
    authenticated: false,
    memoryTab: "reviews",
    memoryFilter: "all",
    pendingCount: 0,
    visibleMemories: [],
    conversationSessions: [],
    conversationSessionsUnavailable: false,
    selectedConversationSessionId: "",
    conversationTurns: [],
    conversationLoading: false,
  },

  onLoad() {
    const app = getApp();
    if (typeof app?.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => this._enterGuestState());
    }
  },

  onShow() {
    if (typeof this.getTabBar === "function" && this.getTabBar()) { this.getTabBar().setData({ selected: 1 }); }
    const authenticated = api.hasAuthenticatedSession();
    this.setData({ authenticated });
    if (!authenticated) {
      this._enterGuestState();
      return;
    }
    this.loadDays();
  },

  onUnload() {
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
  },

  _enterGuestState() {
    this.setData({
      authenticated: false,
      days: [],
      heardTurns: [],
      memoryCandidates: [],
      confirmedMemories: [],
      reviewingClaimId: "",
      pendingCount: 0,
      visibleMemories: [],
      conversationSessions: [],
      conversationSessionsUnavailable: false,
      selectedConversationSessionId: "",
      conversationTurns: [],
      conversationLoading: false,
      loading: false,
      summarizing: false,
      error: "",
    });
  },

  onPullDownRefresh() {
    if (!api.hasAuthenticatedSession()) {
      wx.stopPullDownRefresh();
      return;
    }
    this.loadDays().finally(() => wx.stopPullDownRefresh());
  },

  /*
   * 一键分享：优先用分享按钮上 data-date 指定的日期，其次用当前选中日期。
   * 只分享已经过门禁加载的回顾标题；未登录或没有该日回顾时退回通用文案。
   */
  onShareAppMessage(options) {
    const buttonDate = options?.from === "button" ? options?.target?.dataset?.date : "";
    const date = buttonDate || this.data.selectedDate;
    const day = (this.data.days || []).find((item) => item.date === date);
    const summary = this.data.authenticated && day ? `Memoria 回顾 · ${day.title}` : "";
    return {
      title: (summary || "Memoria · 每天的对话回顾").slice(0, 80),
      path: "/pages/memory/index",
    };
  },

  /*
   * 私人回顾统一加载（PR-19）：days 与 conversation review 共用同一道
   * MemoryRecallPrivate Runtime Profile 门禁和 authEpoch 晚到保护。
   * 门禁拒绝或加载失败时清空全部私人分区，不展示陈旧或越权投影。
   */
  async loadDays() {
    const identity = api.currentIdentity();
    if (!identity) return "guest";
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, error: "" });
    try {
      const gate = await api.requireRuntimeCapability(contracts.Capability.MemoryRecallPrivate);
      if (!api.isAuthEpochCurrent(authEpoch)) return "stale";
      if (!gate.allowed) {
        this.setData({
          ...privatePartitionsClear(),
          error: capabilityGateMessage(gate, contracts.Capability.MemoryRecallPrivate),
        });
        return "denied";
      }
      const [daysResult, reviewResult] = await Promise.all([
        api.getMemoryDays(identity.user_id, 30),
        api.getConversationReview(),
      ]);
      if (!api.isAuthEpochCurrent(authEpoch)) return "stale";
      const review = normalizeConversationReview(reviewResult);
      let sessionsResult = { items: [] };
      let conversationSessionsUnavailable = false;
      if (typeof api.getConversationSessions === "function") {
        try {
          sessionsResult = await api.getConversationSessions(10);
        } catch (error) {
          // Conversation history is an optional demo enhancement. A failed
          // session index must not hide the established daily review.
          sessionsResult = { items: [] };
          conversationSessionsUnavailable = true;
        }
      }
      if (!api.isAuthEpochCurrent(authEpoch)) return "stale";
      this.setData({
        days: (daysResult.items || []).map(normalizeDay),
        heardTurns: review.heardTurns,
        memoryCandidates: review.memoryCandidates,
        confirmedMemories: review.confirmedMemories,
        pendingCount: review.memoryCandidates.length,
        visibleMemories: this._visibleMemories(
          review.memoryCandidates,
          review.confirmedMemories,
          this.data.memoryFilter,
        ),
        conversationSessions: (sessionsResult?.items || []).map((item) => ({
          session_id: item.session_id || "",
          occurred_at: item.occurred_at || "",
          occurred_label: this._formatConversationTime(item.occurred_at),
          turn_count: item.turn_count || 0,
          preview: item.preview || "",
        })),
        conversationSessionsUnavailable,
        selectedConversationSessionId: "",
        conversationTurns: [],
        conversationLoading: false,
      });
      return "ok";
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({
        ...privatePartitionsClear(),
        error: error?.message || "回顾暂时无法加载。",
      });
      return "error";
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ loading: false });
    }
  },

  _formatConversationTime(value) {
    if (!value) return "时间未知";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "时间未知";
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  },

  async openConversation(event) {
    if (!(await requireLogin({ reason: "view_memory" }))) return;
    const sessionId = event?.currentTarget?.dataset?.sessionId;
    if (!sessionId || this.data.conversationLoading) return;
    const authEpoch = api.currentAuthEpoch();
    this._conversationRequestSeq = (this._conversationRequestSeq || 0) + 1;
    const requestSeq = this._conversationRequestSeq;
    this.setData({
      conversationLoading: true,
      selectedConversationSessionId: sessionId,
      conversationTurns: [],
      error: "",
    });
    try {
      const result = await api.getConversationHistory(sessionId, 20);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ conversationTurns: result.turns || [] });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch) || requestSeq !== this._conversationRequestSeq) return;
      this.setData({
        selectedConversationSessionId: "",
        conversationTurns: [],
        error: error?.message || "对话详情暂时无法加载。",
      });
    } finally {
      if (requestSeq !== this._conversationRequestSeq) return;
      if (api.isAuthEpochCurrent(authEpoch)) {
        this.setData({ conversationLoading: false });
      } else {
        this.setData({
          conversationLoading: false,
          selectedConversationSessionId: "",
          conversationTurns: [],
        });
      }
    }
  },

  chooseDay(event) {
    this.setData({ selectedDate: event.currentTarget.dataset.date });
  },

  showReviews() {
    this.setData({ memoryTab: "reviews" });
  },

  showMemories() {
    this.setData({
      memoryTab: "memories",
      visibleMemories: this._visibleMemories(
        this.data.memoryCandidates,
        this.data.confirmedMemories,
        this.data.memoryFilter,
      ),
    });
  },

  setMemoryFilter(event) {
    const memoryFilter = event.currentTarget.dataset.filter || "all";
    this.setData({
      memoryFilter,
      visibleMemories: this._visibleMemories(
        this.data.memoryCandidates,
        this.data.confirmedMemories,
        memoryFilter,
      ),
    });
  },

  _visibleMemories(candidates, confirmed, filter) {
    const pending = (candidates || []).map((item) => ({
      id: item.claim_id,
      pending: true,
      title: item.value,
      body: item.reason || "确认后才会加入记忆档案。",
    }));
    const kept = (confirmed || []).map((item) => ({
      id: item.memory_id,
      pending: false,
      title: item.title || item.snippet,
      body: item.snippet || item.occurred_at || "你已确认的记忆",
    }));
    if (filter === "pending") return pending;
    if (filter === "confirmed") return kept;
    return pending.concat(kept);
  },

  openArchive() {
    wx.navigateTo({ url: "/pages/digital-self/index" });
  },

  goCompanion() {
    wx.switchTab({ url: "/pages/companion/index" });
  },

  async loginForReview() {
    if (!(await requireLogin({ reason: "view_memory" }))) return;
    this.setData({ authenticated: true });
    await this.loadDays();
  },

  /*
   * 候选记忆确认（PR-19）。确认成功后必须重新拉取服务端权威投影；
   * 失败时保持原状态，绝不本地把 candidate 升级为已确认。
   */
  async confirmCandidate(event) {
    if (!(await requireLogin({ reason: "view_memory" }))) return;
    const claimId = event?.currentTarget?.dataset?.claimId;
    if (typeof claimId !== "string" || !claimId) return;
    if (this.data.reviewingClaimId) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ reviewingClaimId: claimId, error: "" });
    try {
      const review = await api.reviewMemoryClaim(claimId);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const refreshed = await this.loadDays();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      if (refreshed === "ok") {
        const trace = review?.trace || {};
        const reviewEventId = trace.review_event_id || review?.review_event_id || "";
        const reviewedAt = trace.reviewed_at || "";
        if (reviewEventId) {
          wx.showModal({
            title: "记忆已确认",
            content: `追溯号：${reviewEventId}${reviewedAt ? `\n确认时间：${reviewedAt}` : ""}`,
            showCancel: false,
            confirmText: "知道了",
          });
        } else {
          wx.showToast({ title: "已确认", icon: "success" });
        }
      }
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "确认失败，请稍后再试。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ reviewingClaimId: "" });
    }
  },

  async summarizeSelectedDay() {
    if (!(await requireLogin({ reason: "generate_review" }))) return;
    this.setData({ authenticated: true });
    const gate = await api.requireRuntimeCapability(contracts.Capability.MemoryRecallPrivate);
    if (!gate.allowed) {
      this.setData({
        error: capabilityGateMessage(gate, contracts.Capability.MemoryRecallPrivate),
      });
      return;
    }
    const identity = api.currentIdentity();
    if (!identity || this.data.summarizing) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ summarizing: true, error: "" });
    try {
      await api.summarizeDay(identity.user_id, this.data.selectedDate);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      await this.loadDays();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      wx.showToast({ title: "回顾已更新", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "生成回顾失败。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ summarizing: false });
    }
  },
});
