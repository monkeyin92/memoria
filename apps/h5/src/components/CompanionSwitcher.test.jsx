import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ updateProfile: vi.fn() }));

vi.mock("../api.js", () => ({ updateProfile: mocks.updateProfile }));

import { CompanionSwitcher } from "./CompanionSwitcher.jsx";

describe("CompanionSwitcher", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.updateProfile.mockResolvedValue({ companion_id: "xuanmo" });
  });

  afterEach(cleanup);

  it("lets an onboarded user change only the companion profile for the next session", async () => {
    const onComplete = vi.fn();
    render(
      <CompanionSwitcher
        userId="owner-1"
        currentCompanionId="starlight"
        onBack={vi.fn()}
        onComplete={onComplete}
      />,
    );

    expect(screen.getByText(/不需要重录声纹，也不会改变数字分身/)).toBeInTheDocument();
    expect(screen.getByText("当前会话不变；保存后从下一次会话生效。")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("radio", { name: "选择玄墨，克制回应 · 很少追问 · 1–2 句" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "保存玄墨的陪伴方式" }));

    await waitFor(() => {
      expect(mocks.updateProfile).toHaveBeenCalledWith("owner-1", {
        companion_id: "xuanmo",
      });
      expect(onComplete).toHaveBeenCalledWith({ companion_id: "xuanmo" });
    });
  });
});
