import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createGrowthTask: vi.fn(),
  getGrowthOverview: vi.fn(),
  getGrowthTasks: vi.fn(),
  respondGrowthTask: vi.fn(),
  reviewGrowthOwnerAction: vi.fn(),
  transitionGrowthTask: vi.fn(),
}));

vi.mock("../api.js", () => mocks);

import { GrowthMapPanel } from "./GrowthMapPanel.jsx";

const dimension = {
  key: "expression",
  status: "supported",
  adopted_sources: [{
    event_id: "event-expression",
    kind: "chat",
    event_type: "owner.action_recorded",
    target_kind: "memory_claim",
    target_id: "trait-1",
    label: "我习惯先讲结论",
    weight: "strong",
    occurred_at: "2026-07-22T00:00:00Z",
  }],
  rejected_reason_counts: { uncertain: 2 },
  conflicts: [{
    target_kind: "persona_trait",
    target_id: "trait-2",
    event_id: "event-conflict",
    action: "保留待确认",
  }],
  recent_changes: [{
    event_id: "event-recent",
    event_type: "trait_confirmed",
    occurred_at: "2026-07-21T00:00:00Z",
  }],
  dependency_blockers: ["等待下一次确认"],
  version_readiness: { status: "stale", version_id: "version-1" },
};

const task = (overrides = {}) => ({
  task_id: "task-1",
  kind: "life_interview",
  status: "active",
  revision: 1,
  prompt_id: "prompt-1",
  prompt: "哪段经历最影响你？",
  created_at: "2026-07-22T00:00:00Z",
  updated_at: "2026-07-22T00:00:00Z",
  ...overrides,
});

describe("GrowthMapPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getGrowthOverview.mockResolvedValue({ dimensions: [dimension] });
    mocks.getGrowthTasks.mockResolvedValue({ items: [] });
    mocks.reviewGrowthOwnerAction.mockResolvedValue({ status: "recorded" });
    mocks.createGrowthTask.mockResolvedValue(task({ status: "draft", revision: 0 }));
    mocks.respondGrowthTask.mockResolvedValue(task({ status: "active", revision: 2 }));
    mocks.transitionGrowthTask.mockImplementation((_taskId, _eventId, toStatus, revision) => (
      Promise.resolve(task({ status: toStatus, revision: revision + 1 }))
    ));
  });

  afterEach(cleanup);

  it("shows qualitative dimensions, source corrections and task choices", async () => {
    render(<GrowthMapPanel onStartChat={vi.fn()} />);

    expect(await screen.findByRole("heading", { name: "成长地图" })).toBeInTheDocument();
    expect(screen.getByText("已有支持")).toBeInTheDocument();
    expect(screen.getByText("采用来源 1 条")).toBeInTheDocument();
    expect(screen.getByText("这里展示的是定性成长状态和证据来源，不是完成百分比；每一条材料都可以被你纠正。")).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: "不像我：表达方式来源 1，我习惯先讲结论",
    })).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: "我不会这样说：表达方式来源 1，我习惯先讲结论",
    })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始自然聊天" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "开始任务" })).toHaveLength(3);
  });

  it("keeps a stable event id through create, answer and completion", async () => {
    render(<GrowthMapPanel onStartChat={vi.fn()} />);
    fireEvent.click((await screen.findAllByRole("button", { name: "开始任务" }))[0]);
    await waitFor(() => expect(mocks.createGrowthTask).toHaveBeenCalledOnce());
    const createEventId = mocks.createGrowthTask.mock.calls[0][0];
    expect(createEventId).toMatch(/[0-9a-f-]{10,}/i);
    await waitFor(() => expect(mocks.transitionGrowthTask).toHaveBeenCalledOnce());
    const startEventId = mocks.transitionGrowthTask.mock.calls[0][1];
    expect(startEventId).not.toBe(createEventId);

    const answer = await screen.findByLabelText("你的回答");
    fireEvent.change(answer, { target: { value: "我会先确认事实再决定。" } });
    fireEvent.click(screen.getByRole("button", { name: "保存回答" }));
    await waitFor(() => expect(mocks.respondGrowthTask).toHaveBeenCalledOnce());
    const responseEventId = mocks.respondGrowthTask.mock.calls[0][1];
    expect(responseEventId).not.toBe(createEventId);
    expect(responseEventId).not.toBe(startEventId);
    await waitFor(() => expect(mocks.transitionGrowthTask).toHaveBeenCalledTimes(2));
    const completeEventId = mocks.transitionGrowthTask.mock.calls[1][1];
    expect(completeEventId).not.toBe(createEventId);
    expect(completeEventId).not.toBe(startEventId);
    expect(completeEventId).not.toBe(responseEventId);

  });

  it("records an owner correction and refreshes the map", async () => {
    render(<GrowthMapPanel onStartChat={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", {
      name: "不像我：表达方式来源 1，我习惯先讲结论",
    }));
    await waitFor(() => expect(mocks.reviewGrowthOwnerAction).toHaveBeenCalledWith(
      expect.any(String),
      "not_me",
      "memory_claim",
      "trait-1",
    ));
    const feedbackEventId = mocks.reviewGrowthOwnerAction.mock.calls[0][0];
    expect(feedbackEventId).not.toBe("event-expression");
    expect(mocks.getGrowthOverview).toHaveBeenCalledTimes(2);
  });

  it("reuses one feedback event id when the same correction is retried", async () => {
    mocks.reviewGrowthOwnerAction
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ status: "recorded" });
    render(<GrowthMapPanel onStartChat={vi.fn()} />);
    const correction = await screen.findByRole("button", {
      name: "不像我：表达方式来源 1，我习惯先讲结论",
    });
    fireEvent.click(correction);
    await waitFor(() => expect(mocks.reviewGrowthOwnerAction).toHaveBeenCalledOnce());
    fireEvent.click(correction);
    await waitFor(() => expect(mocks.reviewGrowthOwnerAction).toHaveBeenCalledTimes(2));
    expect(mocks.reviewGrowthOwnerAction.mock.calls[1][0]).toBe(
      mocks.reviewGrowthOwnerAction.mock.calls[0][0],
    );
  });

  it("keeps the completed task visible and offers another start", async () => {
    mocks.getGrowthTasks.mockResolvedValue({
      items: [task({ status: "completed", revision: 3 })],
    });
    render(<GrowthMapPanel onStartChat={vi.fn()} />);
    expect((await screen.findAllByText("已完成")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "再次开始" })).toBeInTheDocument();
  });

  it("gives each source correction a unique accessible name", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    mocks.getGrowthOverview.mockResolvedValue({
      dimensions: [{
        ...dimension,
        adopted_sources: [
          dimension.adopted_sources[0],
          {
            ...dimension.adopted_sources[0],
            target_id: "trait-2",
            label: "我会先说明背景",
          },
        ],
      }],
    });
    render(<GrowthMapPanel onStartChat={vi.fn()} />);

    expect(await screen.findByRole("button", {
      name: "不像我：表达方式来源 1，我习惯先讲结论",
    })).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: "不像我：表达方式来源 2，我会先说明背景",
    })).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: "我不会这样说：表达方式来源 1，我习惯先讲结论",
    })).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: "我不会这样说：表达方式来源 2，我会先说明背景",
    })).toBeInTheDocument();
    expect(consoleError.mock.calls.flat().join(" ")).not.toContain(
      "Encountered two children with the same key",
    );
    consoleError.mockRestore();
  });

  it("requires draft and paused tasks to become active before accepting an answer", async () => {
    mocks.getGrowthTasks.mockResolvedValue({
      items: [task({ status: "paused", revision: 2 })],
    });
    render(<GrowthMapPanel onStartChat={vi.fn()} />);

    const resume = await screen.findByRole("button", { name: "继续任务" });
    expect(screen.queryByLabelText("你的回答")).not.toBeInTheDocument();
    fireEvent.click(resume);

    await waitFor(() => expect(mocks.transitionGrowthTask).toHaveBeenCalledWith(
      "task-1",
      expect.any(String),
      "active",
      2,
    ));
  });

  it("renders all qualitative states and keeps partial failures retryable", async () => {
    mocks.getGrowthOverview.mockResolvedValue({
      dimensions: [
        { ...dimension, key: "life_chapters", status: "empty" },
        { ...dimension, key: "important_people", status: "emerging" },
        { ...dimension, key: "expression", status: "supported" },
        {
          ...dimension,
          key: "decision_cases",
          status: "conflicted",
          version_readiness: { status: "dependency_pending" },
        },
      ],
    });
    mocks.getGrowthTasks.mockRejectedValue(new Error("tasks offline"));
    render(<GrowthMapPanel onStartChat={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法同步");
    expect(screen.getByText("尚未形成")).toBeInTheDocument();
    expect(screen.getByText("正在形成")).toBeInTheDocument();
    expect(screen.getByText("已有支持")).toBeInTheDocument();
    expect(screen.getByText("存在冲突")).toBeInTheDocument();
    expect(screen.getByText("等待依赖")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重试/ })).toBeInTheDocument();
  });

  it("creates and activates a natural chat task before returning home", async () => {
    const onStartChat = vi.fn();
    mocks.createGrowthTask.mockResolvedValueOnce(task({
      task_id: "natural-task",
      kind: "natural_chat",
      status: "draft",
      revision: 0,
      prompt: "聊聊今天发生的事。",
    }));
    mocks.transitionGrowthTask.mockResolvedValueOnce(task({
      task_id: "natural-task",
      kind: "natural_chat",
      status: "active",
      revision: 1,
      prompt: "聊聊今天发生的事。",
    }));
    render(<GrowthMapPanel onStartChat={onStartChat} />);
    fireEvent.click(await screen.findByRole("button", { name: "开始自然聊天" }));
    await waitFor(() => expect(mocks.createGrowthTask).toHaveBeenCalledWith(
      expect.any(String),
      "natural_chat",
    ));
    await waitFor(() => expect(mocks.transitionGrowthTask).toHaveBeenCalledWith(
      "natural-task",
      expect.any(String),
      "active",
      0,
    ));
    expect(onStartChat).toHaveBeenCalledWith(expect.objectContaining({
      task_id: "natural-task",
      kind: "natural_chat",
      status: "active",
    }));
  });

  it("does not start a second natural-chat task while a voice session is active", async () => {
    render(<GrowthMapPanel onStartChat={vi.fn()} voiceSessionActive />);

    const button = await screen.findByRole("button", {
      name: "请先结束当前对话",
    });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(mocks.createGrowthTask).not.toHaveBeenCalled();
  });
});
