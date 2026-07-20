import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getRawVoiceConsent: vi.fn(),
  grantRawVoiceConsent: vi.fn(),
  revokeRawVoiceConsent: vi.fn(),
}));

vi.mock("../api.js", () => mocks);

import { PrivacyDataPanel } from "./PrivacyDataPanel.jsx";

describe("PrivacyDataPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mocks.getRawVoiceConsent.mockResolvedValue({ consent: null });
  });

  afterEach(cleanup);

  it("grants raw voice archiving only after explaining its independent boundary", async () => {
    mocks.grantRawVoiceConsent.mockResolvedValue({
      consent_grant_id: "raw-consent-1",
      policy_version: "raw-voice-archive-v1",
      retention_policy: "account_lifetime",
      granted_at: "2026-07-19T12:00:00Z",
      revoked_at: null,
    });
    render(<PrivacyDataPanel onBack={vi.fn()} />);

    expect(await screen.findByRole("heading", { name: "原始语音归档" }))
      .toBeInTheDocument();
    expect(screen.getByText(/仅归档判定为账户主人的音频/)).toBeInTheDocument();
    expect(screen.getByText(/访客、不确定说话人与助手音频不会归档/)).toBeInTheDocument();
    expect(screen.getByText(/加密保存至账户存续期结束/)).toBeInTheDocument();
    const grant = screen.getByRole("button", { name: "开启原始语音归档" });
    expect(grant).toBeDisabled();

    fireEvent.click(screen.getByLabelText("我同意按上述范围加密保存本人的原始语音"));
    fireEvent.click(grant);

    await waitFor(() => expect(mocks.grantRawVoiceConsent).toHaveBeenCalledOnce());
    expect(await screen.findByText("原始语音归档已开启"))
      .toBeInTheDocument();
  });

  it("does not offer consent changes while the current state is unknown", async () => {
    mocks.getRawVoiceConsent
      .mockRejectedValueOnce(new Error("授权状态暂时无法读取"))
      .mockResolvedValueOnce({ consent: null });
    render(<PrivacyDataPanel onBack={vi.fn()} />);

    expect(await screen.findByRole("alert"))
      .toHaveTextContent("原始语音归档状态暂时无法读取，请检查网络后重试。");
    expect(screen.getByText("读取失败")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "开启原始语音归档" }))
      .not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重新读取" }));

    expect(await screen.findByRole("button", { name: "开启原始语音归档" }))
      .toBeDisabled();
    expect(screen.getByText("未开启")).toBeInTheDocument();
  });

  it("waits for the API before showing that revocation deleted historical raw audio", async () => {
    mocks.getRawVoiceConsent.mockResolvedValue({
      consent: {
        consent_grant_id: "raw-consent-1",
        policy_version: "raw-voice-archive-v1",
        retention_policy: "account_lifetime",
        granted_at: "2026-07-19T12:00:00Z",
        revoked_at: null,
      },
    });
    let finishRevoke;
    mocks.revokeRawVoiceConsent.mockImplementation(() => new Promise((resolve) => {
      finishRevoke = resolve;
    }));
    render(<PrivacyDataPanel onBack={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "撤销并删除原始音频" }));
    expect(screen.getByText(/转写与结构化记忆不会随原始音频一起删除/))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByRole("button", { name: "撤销并删除原始音频" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "撤销并删除原始音频" }));
    expect(screen.getByRole("button", { name: "取消" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "确认撤销并删除" }));
    expect(screen.queryByText("历史原始音频已删除")).not.toBeInTheDocument();

    finishRevoke({
      consent_grant_id: "raw-consent-1",
      revoked_at: "2026-07-19T13:00:00Z",
    });
    expect(await screen.findByText(/历史原始音频已删除/)).toBeInTheDocument();
  });
});
