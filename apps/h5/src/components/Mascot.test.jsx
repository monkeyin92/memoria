import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Mascot } from "./Mascot.jsx";

describe("Mascot", () => {
  it("shows the semantic expression selected from the conversation", () => {
    const { container } = render(
      <Mascot
        emotion="happy"
        uiState="listening"
        onActivate={() => undefined}
        disabled={false}
      />,
    );

    expect(screen.getByRole("button", { name: "轻触吉祥物开始实时对话" }))
      .toBeEnabled();
    expect(container.querySelector('img[src*="mascot-happy.webp"]'))
      .toHaveClass("is-visible");
  });

  it("uses the curious face while the assistant is thinking", () => {
    const { container } = render(
      <Mascot
        emotion="upset"
        uiState="thinking"
        onActivate={() => undefined}
        disabled={false}
      />,
    );

    expect(container.querySelector('img[src*="mascot-curious.webp"]'))
      .toHaveClass("is-visible");
  });

  it("animates speaking by alternating mouth and eye artwork", () => {
    vi.useFakeTimers();
    const { container } = render(
      <Mascot
        emotion="upset"
        uiState="speaking"
        onActivate={() => undefined}
        disabled={false}
      />,
    );
    expect(container.querySelector('img[src*="mascot-upset.webp"]'))
      .toHaveClass("is-visible");

    act(() => vi.advanceTimersByTime(470));
    expect(container.querySelector('img[src*="mascot-neutral.webp"]'))
      .toHaveClass("is-visible");
    vi.useRealTimers();
  });
});
