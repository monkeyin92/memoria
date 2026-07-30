import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Mascot } from "./Mascot.jsx";

afterEach(cleanup);

describe("Mascot", () => {
  it("loads one front-facing body and shows the selected SVG expression", () => {
    const { container } = render(
      <Mascot
        emotion="happy"
        uiState="listening"
        onActivate={() => undefined}
        disabled={false}
      />,
    );

    expect(container.querySelectorAll(".mascot-body")).toHaveLength(1);
    expect(container.querySelector(".mascot-body")).toHaveAttribute(
      "src",
      expect.stringContaining("companions/alpha/starlight.webp"),
    );
    expect(screen.getByRole("button")).toHaveAttribute(
      "data-expression",
      "happy",
    );
    expect(container.querySelectorAll(".face-expression.is-visible")).toHaveLength(1);
  });

  it("keeps the voice expression independent from the thinking activity", () => {
    render(
      <Mascot
        emotion="caring"
        uiState="thinking"
        onActivate={() => undefined}
        disabled={false}
      />,
    );

    expect(screen.getByRole("button")).toHaveClass("mascot-thinking");
    expect(screen.getByRole("button")).toHaveAttribute(
      "data-expression",
      "caring",
    );
  });

  it("preserves the semantic expression while speaking", () => {
    render(
      <Mascot
        emotion="caring"
        uiState="speaking"
        onActivate={() => undefined}
        disabled={false}
      />,
    );

    expect(screen.getByRole("button")).toHaveClass("mascot-speaking");
    expect(screen.getByRole("button")).toHaveAttribute(
      "data-expression",
      "caring",
    );
  });

  it("is actionable only before a conversation starts", () => {
    const onActivate = vi.fn();
    const { rerender } = render(
      <Mascot
        emotion="neutral"
        uiState="idle"
        onActivate={onActivate}
        disabled={false}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "轻触星澜开始实时对话" }),
    );
    expect(onActivate).toHaveBeenCalledOnce();

    rerender(
      <Mascot
        emotion="happy"
        uiState="listening"
        onActivate={onActivate}
        disabled={false}
        active
      />,
    );
    expect(
      screen.getByRole("button", { name: "星澜正在陪伴" }),
    ).toBeDisabled();
  });
});
