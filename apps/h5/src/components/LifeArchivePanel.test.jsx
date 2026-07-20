import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getLifeTimeline: vi.fn(),
  getMemoryReviewQueue: vi.fn(),
  reviewMemoryClaim: vi.fn(),
  searchLifeArchive: vi.fn(),
}));

vi.mock("../api.js", () => ({
  getLifeTimeline: mocks.getLifeTimeline,
  getMemoryReviewQueue: mocks.getMemoryReviewQueue,
  reviewMemoryClaim: mocks.reviewMemoryClaim,
  searchLifeArchive: mocks.searchLifeArchive,
}));

import { LifeArchivePanel } from "./LifeArchivePanel.jsx";

describe("LifeArchivePanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getLifeTimeline.mockResolvedValue({
      items: [
        {
          timeline_id: "timeline-1",
          title: "第一次带团队完成年度项目",
          category: "work_experience",
          status: "confirmed",
          event_start: "2024-12-18T00:00:00+08:00",
          time_precision: "day",
        },
      ],
    });
    mocks.getMemoryReviewQueue.mockResolvedValue({
      items: [
        {
          item_id: "claim-1",
          kind: "claim",
          category: "family_story",
          value: "父亲常说做事要留余地",
          status: "candidate",
          reason: "conflicting_values",
        },
      ],
    });
    mocks.searchLifeArchive.mockResolvedValue({
      items: [
        {
          item_id: "knowledge-1",
          kind: "knowledge",
          title: "带团队的经验",
          snippet: "先明确目标，再给成员足够空间。",
          category: "work_experience",
          status: "confirmed",
          occurred_at: "2024-12-18T00:00:00+08:00",
        },
      ],
    });
    mocks.reviewMemoryClaim.mockResolvedValue({
      claim_id: "claim-1",
      status: "confirmed",
    });
  });

  afterEach(cleanup);

  it("shows the life timeline, searches the archive and reviews conflicts", async () => {
    render(<LifeArchivePanel />);

    expect(await screen.findByRole("heading", { name: "人生时间线" }))
      .toBeInTheDocument();
    expect(screen.getByText("第一次带团队完成年度项目")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "待你确认" })).toBeInTheDocument();
    expect(screen.getByText("这条记忆与已有内容存在冲突")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("搜索人生知识库"), {
      target: { value: "带团队" },
    });
    fireEvent.click(screen.getByRole("button", { name: "搜索记忆" }));

    await waitFor(() => {
      expect(mocks.searchLifeArchive).toHaveBeenCalledWith("带团队");
    });
    expect(await screen.findByText("先明确目标，再给成员足够空间。"))
      .toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "确认这条记忆" }));
    await waitFor(() => {
      expect(mocks.reviewMemoryClaim).toHaveBeenCalledWith("claim-1", "confirm");
    });
    expect(screen.queryByText("父亲常说做事要留余地")).not.toBeInTheDocument();
  });

  it("keeps a disputed memory in the review queue with an explicit status", async () => {
    mocks.reviewMemoryClaim.mockResolvedValue({
      claim_id: "claim-1",
      status: "disputed",
    });
    render(<LifeArchivePanel />);

    fireEvent.click(await screen.findByRole("button", { name: "标记有争议" }));

    await waitFor(() => {
      expect(mocks.reviewMemoryClaim).toHaveBeenCalledWith("claim-1", "dispute");
    });
    expect(screen.getByText("已标记为有争议，等待你之后修正或确认。"))
      .toBeInTheDocument();
  });
});
