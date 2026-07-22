import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  enrollSpeakerProfiles: vi.fn(),
  updateProfile: vi.fn(),
  createSpeakerPcmRecorder: vi.fn(),
}));

vi.mock("../api.js", () => ({
  enrollSpeakerProfiles: mocks.enrollSpeakerProfiles,
  updateProfile: mocks.updateProfile,
}));

vi.mock("../lib/audioEnrollment.js", () => ({
  createSpeakerPcmRecorder: mocks.createSpeakerPcmRecorder,
}));

import { CompanionOnboarding } from "./CompanionOnboarding.jsx";
import { companions } from "../lib/companions.js";

describe("CompanionOnboarding", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [{ stop: vi.fn() }],
        }),
      },
    });
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: vi.fn(),
    });
    mocks.createSpeakerPcmRecorder.mockImplementation(() => ({
      start: vi.fn().mockResolvedValue(undefined),
      stop: vi.fn().mockResolvedValue({
        audio_base64: "AAAA",
        sample_rate: 16_000,
        device: "h5-web-audio",
        scene: "owner-enrollment",
      }),
      cancel: vi.fn().mockResolvedValue(undefined),
    }));
    mocks.enrollSpeakerProfiles.mockResolvedValue({ profile_id: "speaker-1" });
    mocks.updateProfile.mockResolvedValue({
      user_id: "owner-1",
      companion_id: "xuanmo",
    });
  });

  afterEach(() => {
    cleanup();
    delete HTMLElement.prototype.scrollIntoView;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("maps every companion to its versioned Doubao preview", () => {
    expect(companions.map(({ id, voiceName, voicePreview }) => ({
      id,
      voiceName,
      voicePreview,
    }))).toEqual([
      { id: "starlight", voiceName: "阳光青年 2.0", voicePreview: "/assets/voices/starlight-doubao-v1.wav" },
      { id: "taoxi", voiceName: "甜美桃子 2.0", voicePreview: "/assets/voices/taoxi-doubao-v1.wav" },
      { id: "mianmian", voiceName: "温柔小雅 2.0", voicePreview: "/assets/voices/mianmian-doubao-v1.wav" },
      { id: "axu", voiceName: "高冷沉稳 2.0", voicePreview: "/assets/voices/axu-doubao-v1.wav" },
      { id: "xuanmo", voiceName: "深夜播客 2.0", voicePreview: "/assets/voices/xuanmo-doubao-v1.wav" },
    ]);
  });

  it("lets the user browse all five companions and demo expressions and voice", async () => {
    const { container } = render(
      <CompanionOnboarding userId="owner-1" onComplete={() => undefined} />,
    );

    expect(screen.getAllByRole("button", { name: /^选择/ })).toHaveLength(5);
    expect(screen.getByRole("heading", { name: "选择喜欢的陪伴方式" }))
      .toBeInTheDocument();
    expect(screen.getByLabelText("陪伴方式与数字分身的边界"))
      .toHaveTextContent("陪伴方式决定助手怎样回应，不代表你的性格。");
    expect(screen.getByLabelText("陪伴方式与数字分身的边界"))
      .toHaveTextContent("伙伴说的话不会成为你的证据。");
    fireEvent.click(screen.getByRole("button", { name: "下一个伙伴" }));
    expect(screen.getByRole("heading", { name: "桃喜" })).toBeInTheDocument();
    expect(screen.getByText("甜美桃子 2.0")).toBeInTheDocument();
    expect(screen.getByText("清甜、灵动，回应里带一点自然上扬")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "开心" }));
    expect(container.querySelector('[data-companion="taoxi"][data-expression="happy"]'))
      .toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "试听桃喜的声音" }));
    await waitFor(() => expect(HTMLMediaElement.prototype.play).toHaveBeenCalled());
    expect(container.querySelector("audio")).toHaveAttribute(
      "src",
      "/assets/voices/taoxi-doubao-v1.wav",
    );
  });

  it("shows a recoverable message when microphone permission is denied", async () => {
    navigator.mediaDevices.getUserMedia.mockRejectedValue(
      Object.assign(new Error("denied"), { name: "NotAllowedError" }),
    );
    render(<CompanionOnboarding userId="owner-1" onComplete={() => undefined} />);

    fireEvent.click(screen.getByRole("button", { name: "让 星澜 陪我" }));
    fireEvent.click(screen.getByRole("checkbox", { name: /声纹模板/ }));
    fireEvent.click(screen.getByRole("button", { name: "录制第 1 段" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("需要麦克风权限");
  });

  it("resets the onboarding scroll position when changing steps", async () => {
    render(<CompanionOnboarding userId="owner-1" onComplete={() => undefined} />);
    const chooseStep = screen.getByRole("region", { name: "选择陪伴方式" });
    chooseStep.scrollTop = 160;

    fireEvent.click(screen.getByRole("button", { name: "让 星澜 陪我" }));

    const enrollmentStep = screen.getByRole("region", { name: "录制声纹" });
    await waitFor(() => expect(enrollmentStep.scrollTop).toBe(0));
    enrollmentStep.scrollTop = 120;
    fireEvent.click(screen.getByRole("button", { name: "重新选择" }));

    await waitFor(() => {
      expect(screen.getByRole("region", { name: "选择陪伴方式" }).scrollTop)
        .toBe(0);
    });
  });

  it("restores the selected card when returning from voiceprint enrollment", () => {
    render(<CompanionOnboarding userId="owner-1" onComplete={() => undefined} />);

    fireEvent.click(
      screen.getByRole("button", { name: "选择玄墨，克制回应、很少追问、1–2 句" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "让 玄墨 陪我" }));
    HTMLElement.prototype.scrollIntoView.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "重新选择" }));

    expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalledWith({
      behavior: "auto",
      inline: "center",
      block: "nearest",
    });
    expect(HTMLElement.prototype.scrollIntoView.mock.instances[0]).toHaveAccessibleName(
      "选择玄墨，克制回应、很少追问、1–2 句",
    );
  });

  it("records three clips, creates a shadow voiceprint, then persists the companion", async () => {
    const onComplete = vi.fn();
    render(<CompanionOnboarding userId="owner-1" onComplete={onComplete} />);

    fireEvent.click(
      screen.getByRole("button", { name: "选择玄墨，克制回应、很少追问、1–2 句" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "让 玄墨 陪我" }));
    fireEvent.click(screen.getByRole("checkbox", { name: /声纹模板/ }));

    for (let index = 1; index <= 3; index += 1) {
      fireEvent.click(screen.getByRole("button", { name: `录制第 ${index} 段` }));
      fireEvent.click(
        await screen.findByRole("button", { name: `停止第 ${index} 段录音` }),
      );
      if (index < 3) {
        await waitFor(() => {
          expect(screen.getByRole("button", { name: `录制第 ${index + 1} 段` }))
            .toBeEnabled();
        });
      }
    }

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "建立声纹并开始陪伴" })).toBeEnabled();
    });

    fireEvent.click(screen.getByRole("button", { name: "建立声纹并开始陪伴" }));

    await waitFor(() => {
      expect(mocks.createSpeakerPcmRecorder).toHaveBeenCalledTimes(3);
      expect(mocks.enrollSpeakerProfiles).toHaveBeenCalledWith([
        {
          audio_base64: "AAAA",
          sample_rate: 16_000,
          device: "h5-web-audio",
          scene: "owner-enrollment",
        },
        {
          audio_base64: "AAAA",
          sample_rate: 16_000,
          device: "h5-web-audio",
          scene: "owner-enrollment",
        },
        {
          audio_base64: "AAAA",
          sample_rate: 16_000,
          device: "h5-web-audio",
          scene: "owner-enrollment",
        },
      ]);
      expect(mocks.updateProfile).toHaveBeenCalledWith("owner-1", {
        companion_id: "xuanmo",
      });
      expect(onComplete).toHaveBeenCalledWith(expect.objectContaining({
        companion_id: "xuanmo",
      }));
    });
  });
});
