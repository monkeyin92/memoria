import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InteractionModePanel } from "./InteractionModePanel.jsx";

afterEach(cleanup);

const modes = {
  companion: { status: "available", conversational: true },
  archive: { status: "available", conversational: false },
  self_preview: { status: "available", conversational: true },
  legacy: {
    status: "blocked",
    conversational: true,
    missing: ["frozen_digital_self_version"],
  },
};

describe("InteractionModePanel", () => {
  it("opens Self Preview only when an explicit callback is supplied", () => {
    const onOpenSelfPreview = vi.fn();
    render(
      <InteractionModePanel
        capabilities={{ selected_companion_id: "starlight", modes }}
        onOpenSelfPreview={onOpenSelfPreview}
        onOpenArchive={vi.fn()}
        onChangeCompanion={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "打开数字分身预览" }));
    expect(onOpenSelfPreview).toHaveBeenCalledOnce();
  });

  it("fails closed when Self Preview is available but the callback is missing", () => {
    render(
      <InteractionModePanel
        capabilities={{ selected_companion_id: "starlight", modes }}
      />,
    );

    expect(screen.getByRole("button", { name: "预览入口未连接" })).toBeDisabled();
  });

  it("keeps the companion boundary visible", () => {
    render(
      <InteractionModePanel
        capabilities={{
          selected_companion_id: "starlight",
          modes: {
            ...modes,
            self_preview: {
              status: "blocked",
              conversational: true,
              missing: ["approved_digital_self_version"],
            },
          },
        }}
      />,
    );

    expect(screen.getByText(/伙伴说的话不会成为你的性格证据/)).toBeInTheDocument();
    expect(screen.getByText("需要先建立并批准数字分身版本")).toBeInTheDocument();
  });
});
