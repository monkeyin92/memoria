import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  Capability,
  PolicyObligation,
  ServiceMode,
  SpeakerState,
  SubjectCategory,
} from "../lib/multiSubject/contracts.js";
import { normalizeRuntimeProfileV2 } from "../lib/multiSubject/runtimeProfile.js";
import { DeviceSubjectPanel } from "./DeviceSubjectPanel.jsx";

const HEX64 = "a".repeat(64);

function signedProfileV2(overrides = {}) {
  return {
    signature_schema: "runtime-profile-v2",
    runtime_profile_id: "rp-1",
    device_id: "dev-1",
    session_id: "ses-authority",
    actor_id: "acct-owner",
    binding_id: "bd-1",
    binding_version: 1,
    active_subject_id: "person-child",
    subject_revision: 3,
    subject_category: SubjectCategory.Minor,
    age_band: "under_14",
    speaker_state: SpeakerState.Confirmed,
    speaker_confidence: 0.91,
    service_mode: ServiceMode.StudentMinor,
    persona_assignment_id: "pa-1",
    persona: {
      persona_id: "starlight",
      version: 4,
      relationship_stage: "familiar",
    },
    policy_bundle_version: "student-cn-v3",
    capabilities: [Capability.Chat, Capability.Tutor, Capability.MemoryRecallPrivate],
    obligations: [
      {
        code: PolicyObligation.MAXSESSIONSECONDS,
        params: {
          max_session_seconds: 1800,
          retention_ttl_seconds: null,
          quiet_hours: null,
          extras: [],
        },
      },
    ],
    policy_receipt_ids: ["receipt-1"],
    session_epoch: 7,
    issued_at: "2026-01-01T00:00:00Z",
    expires_at: "2099-01-01T00:00:00Z",
    signature: HEX64,
    ...overrides,
  };
}

function binding() {
  return {
    binding_id: "bd-1",
    device_id: "dev-1",
    declared_mode: "parent_for_child",
    binding_version: 1,
    roles: [
      { person_id: "person-parent", role: "account_owner", permissions: [] },
      { person_id: "person-parent", role: "guardian", permissions: [] },
      { person_id: "person-child", role: "primary_subject", permissions: [] },
    ],
  };
}

function resolution(overrides = {}) {
  return {
    valid: true,
    resolution: "confirmation_required",
    candidate_subjects: [
      { person_id: "person-child", display_name: "小乐", confidence: 0.71 },
      { person_id: "person-parent", display_name: "妈妈", confidence: 0.23 },
    ],
    temporary_service_mode: ServiceMode.UnknownSafe,
    allowed_confirmation_methods: ["voice_question", "app_confirm"],
    runtime_profile_id: "rp-temp-1",
    ...overrides,
  };
}

function renderPanel(overrides = {}) {
  const props = {
    binding: binding(),
    profile: normalizeRuntimeProfileV2(signedProfileV2()),
    resolution: resolution(),
    displayContext: { offline: false, multipleSpeakers: false },
    sessionId: "ses-authority",
    voiceActive: false,
    loading: false,
    error: "",
    onRefresh: vi.fn().mockResolvedValue(undefined),
    onResolveSubject: vi.fn().mockResolvedValue(resolution()),
    onSwitchSubject: vi.fn(),
    onBeforeSubjectSwitch: vi.fn().mockResolvedValue(undefined),
    onSubjectSwitched: vi.fn(),
    onRestartVoice: vi.fn().mockResolvedValue(undefined),
    onCreateSession: vi.fn().mockResolvedValue({}),
    onOpenBindFlow: vi.fn(),
    onBack: vi.fn(),
    ...overrides,
  };
  render(<DeviceSubjectPanel {...props} />);
  return props;
}

describe("DeviceSubjectPanel 主体切换原子性", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(cleanup);

  it("with an active voice session: stops media first, then switches, then reconnects", async () => {
    const props = renderPanel({
      voiceActive: true,
      onSwitchSubject: vi.fn().mockResolvedValue(
        normalizeRuntimeProfileV2(
          signedProfileV2({
            session_epoch: 8,
            active_subject_id: "person-parent",
          }),
        ),
      ),
    });

    fireEvent.click(screen.getByRole("radio", { name: /妈妈/ }));
    fireEvent.click(
      screen.getByRole("button", { name: "在应用中确认并切换" }),
    );

    await waitFor(() => expect(props.onBeforeSubjectSwitch).toHaveBeenCalledOnce());
    await waitFor(() => expect(props.onSwitchSubject).toHaveBeenCalledWith("person-parent"));
    expect(props.onBeforeSubjectSwitch.mock.invocationCallOrder[0]).toBeLessThan(
      props.onSwitchSubject.mock.invocationCallOrder[0],
    );
    await waitFor(() => expect(props.onSubjectSwitched).toHaveBeenCalledOnce());
    await waitFor(() => expect(props.onRestartVoice).toHaveBeenCalledOnce());
    expect(props.onRestartVoice.mock.invocationCallOrder[0]).toBeGreaterThan(
      props.onSwitchSubject.mock.invocationCallOrder[0],
    );
  });

  it("with a management session (no media): switches without starting the microphone", async () => {
    const props = renderPanel({
      voiceActive: false,
      onSwitchSubject: vi.fn().mockResolvedValue(
        normalizeRuntimeProfileV2(
          signedProfileV2({
            session_epoch: 8,
            active_subject_id: "person-parent",
          }),
        ),
      ),
    });

    fireEvent.click(screen.getByRole("radio", { name: /妈妈/ }));
    fireEvent.click(
      screen.getByRole("button", { name: "在应用中确认并切换" }),
    );

    await waitFor(() => expect(props.onSwitchSubject).toHaveBeenCalledWith("person-parent"));
    expect(props.onBeforeSubjectSwitch).not.toHaveBeenCalled();
    await waitFor(() => expect(props.onSubjectSwitched).toHaveBeenCalledOnce());
    expect(props.onRestartVoice).not.toHaveBeenCalled();
  });

  it("rejects epoch-regressed or stale switch results and reports the failure", async () => {
    const props = renderPanel({
      voiceActive: true,
      onSwitchSubject: vi.fn().mockResolvedValue(
        normalizeRuntimeProfileV2(
          signedProfileV2({ session_epoch: 6, active_subject_id: "person-parent" }),
        ),
      ),
    });

    fireEvent.click(screen.getByRole("radio", { name: /妈妈/ }));
    fireEvent.click(
      screen.getByRole("button", { name: "在应用中确认并切换" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /会话版本未提升，已拒绝应用/,
    );
    expect(props.onBeforeSubjectSwitch).toHaveBeenCalledOnce();
    expect(props.onSubjectSwitched).not.toHaveBeenCalled();
    expect(props.onRestartVoice).not.toHaveBeenCalled();
  });

  it("only allows switch when the server declared app_confirm", async () => {
    const props = renderPanel({
      resolution: resolution({ allowed_confirmation_methods: ["voice_question"] }),
    });
    expect(
      screen.getByText(/需要通过语音确认身份/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "在应用中确认并切换" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: /妈妈/ }));
    expect(props.onSwitchSubject).not.toHaveBeenCalled();
  });

  it("shows safe-mode guidance when the display context reports multiple speakers", () => {
    renderPanel({
      displayContext: { offline: false, multipleSpeakers: true },
    });
    expect(screen.getByRole("heading", { name: "当前处于安全模式" }))
      .toBeInTheDocument();
    expect(screen.getByText(/unknown_safe/)).toBeInTheDocument();
  });

  it("renders only server-provided candidates and shows the no-binding state", () => {
    renderPanel({
      binding: null,
      profile: null,
      resolution: null,
      sessionId: null,
    });
    expect(screen.getByRole("heading", { name: "还没有绑定设备" }))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开始首次绑定" }));
  });
});
