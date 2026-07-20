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

  it("keeps one face while speaking instead of alternating assets", () => {
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

    act(() => vi.advanceTimersByTime(900));
    expect(container.querySelector('img[src*="mascot-upset.webp"]'))
      .toHaveClass("is-visible");
    expect(container.querySelector('img[src*="mascot-neutral.webp"]'))
      .not.toHaveClass("is-visible");
    vi.useRealTimers();
  });

  it("crossfades between expressions without hard-cutting the previous frame", () => {
    vi.useFakeTimers();
    const { container, rerender } = render(
      <Mascot
        emotion="neutral"
        uiState="listening"
        onActivate={() => undefined}
        disabled={false}
      />,
    );
    expect(container.querySelector('img[src*="mascot-neutral.webp"]'))
      .toHaveClass("is-visible");

    rerender(
      <Mascot
        emotion="happy"
        uiState="listening"
        onActivate={() => undefined}
        disabled={false}
      />,
    );

    expect(container.querySelector('img[src*="mascot-happy.webp"]'))
      .toHaveClass("is-visible");
    // Previous frame held under the dissolve so eyes do not pop.
    expect(container.querySelector('img[src*="mascot-neutral.webp"]'))
      .toHaveClass("is-fading-out");

    act(() => vi.advanceTimersByTime(560));
    expect(container.querySelector('img[src*="mascot-neutral.webp"]'))
      .not.toHaveClass("is-fading-out");
    vi.useRealTimers();
  });
});
