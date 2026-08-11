import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  buildDeviceBindingRequest,
  DeviceBindingFlow,
} from "./DeviceBindingFlow.jsx";
import { DEVICE_DECLARED_MODE_VALUES } from "../lib/multiSubject/contracts.js";
import { PRIMARY_RELATIONSHIPS } from "../lib/multiSubject/modeMeta.js";

const base = {
  mode: "parent_for_child",
  form: {
    childNickname: "小乐",
    childAgeBand: "under_14",
    tutorEnabled: true,
    englishPracticeEnabled: true,
    maxSessionMinutes: 30,
    quietHoursStart: "21:00",
    quietHoursEnd: "07:00",
  },
  consents: { offer_minor_voice_session_v1: true },
  personaId: "starlight",
  ownerPersonId: "person-parent",
  claimToken: "claim-1",
};

describe("DeviceBindingFlow 请求约束（buildDeviceBindingRequest）", () => {
  it("family_shared uses the owner person_id with family_member_of and never creates a fake family subject", () => {
    const request = buildDeviceBindingRequest({
      ...base,
      mode: "family_shared",
      form: { sharedPersonaEnabled: true },
      consents: { offer_family_space_v1: true },
    });
    expect(request.declared_mode).toBe("family_shared");
    expect(request.primary_subject).toEqual({
      person_id: "person-parent",
      relationship: "family_member_of",
    });
    expect(request.primary_subject.person_id).toBe("person-parent");
    expect(request.primary_subject).not.toHaveProperty("subject_draft");
    expect(request.primary_subject.person_id).not.toBe("new");
  });

  it("family_shared request never carries member lists or family names before an invite contract", () => {
    const request = buildDeviceBindingRequest({
      ...base,
      mode: "family_shared",
      form: { sharedPersonaEnabled: true },
      consents: { offer_family_space_v1: true },
    });
    const serialized = JSON.stringify(request);
    // canonical family_member_of 关系值合法；请求不得出现成员列表/家庭名等
    // 未被合同冻结的额外键。
    expect(serialized).not.toMatch(
      /"(members|member_ids|familyName|adminNickname|nicknames?)"/i,
    );
    expect(request.service_preferences).toEqual({
      memory_level: "family_shared",
      shared_persona_enabled: true,
    });
  });

  it("self_use uses the owner person_id with self and does not resubmit the nickname", () => {
    const request = buildDeviceBindingRequest({
      ...base,
      mode: "self_use",
      form: {
        displayName: "旧称呼（只读）",
        memoryLevel: "personal",
        interviewFrequency: "low",
      },
      consents: { offer_self_memory_retention_v1: true },
    });
    expect(request.primary_subject).toEqual({
      person_id: "person-parent",
      relationship: "self",
    });
    expect(request.primary_subject).not.toHaveProperty("subject_draft");
    expect(JSON.stringify(request)).not.toMatch(/旧称呼|displayName/);
  });

  it("parent_for_child and child_for_parent carry drafts with canonical relationships", () => {
    const child = buildDeviceBindingRequest(base);
    expect(child.primary_subject.person_id).toBe("new");
    expect(child.primary_subject.relationship).toBe("guardian_of");
    expect(child.primary_subject.subject_draft).toEqual({
      display_name: "小乐",
      age_band: "under_14",
    });

    const parent = buildDeviceBindingRequest({
      ...base,
      mode: "child_for_parent",
      form: {
        parentNickname: "爸爸",
        speechSpeed: "slow",
        memoryLevel: "none",
        adminVisibility: "device_status",
      },
      consents: { offer_admin_device_management_v1: true },
    });
    expect(parent.primary_subject.relationship).toBe("child_of");
    expect(parent.primary_subject.subject_draft).toEqual({
      display_name: "爸爸",
      age_band: "adult",
    });
  });

  it("uses canonical relationship values for every mode (no second mapping)", () => {
    for (const mode of DEVICE_DECLARED_MODE_VALUES) {
      const form =
        mode === "parent_for_child"
          ? {
              childNickname: "小乐",
              childAgeBand: "under_14",
              tutorEnabled: true,
              englishPracticeEnabled: true,
              maxSessionMinutes: 30,
              quietHoursStart: "21:00",
              quietHoursEnd: "07:00",
            }
          : mode === "self_use"
            ? { displayName: "X", memoryLevel: "personal", interviewFrequency: "low" }
            : mode === "child_for_parent"
              ? {
                  parentNickname: "爸爸",
                  speechSpeed: "slow",
                  memoryLevel: "none",
                  adminVisibility: "device_status",
                }
              : { sharedPersonaEnabled: true };
      const request = buildDeviceBindingRequest({
        ...base,
        mode,
        form,
        consents: {},
      });
      expect(request.primary_subject.relationship).toBe(PRIMARY_RELATIONSHIPS[mode]);
      // canonical 枚举防漂移：关系必须是 canonical RelationshipType 集合成员。
      expect(
        [
          "self",
          "parent_of",
          "child_of",
          "guardian_of",
          "ward_of",
          "spouse_of",
          "sibling_of",
          "caregiver_of",
          "emergency_contact_for",
          "delegate_for",
          "beneficiary_of",
          "co_subject_of",
          "family_member_of",
        ],
      ).toContain(request.primary_subject.relationship);
    }
  });

  it("never includes parent-self-acceptance offers or forged authorization fields", () => {
    const request = buildDeviceBindingRequest({
      ...base,
      mode: "child_for_parent",
      form: {
        parentNickname: "爸爸",
        speechSpeed: "slow",
        memoryLevel: "none",
        adminVisibility: "device_status",
      },
      consents: {
        offer_admin_device_management_v1: true,
        // 即使 UI 状态误含父母本人确认项，也不得进入请求。
        offer_senior_service_acceptance_v1: true,
      },
    });
    expect(request.consent_offer_ids).toEqual(["offer_admin_device_management_v1"]);
    const serialized = JSON.stringify(request);
    expect(serialized).not.toMatch(/policy_version|accepted|authorized|consent_granted/i);
    expect(serialized).not.toMatch(/offer_senior_/);
  });

  it("rejects unknown modes instead of inventing relationships", () => {
    expect(() =>
      buildDeviceBindingRequest({ ...base, mode: "give_to_child" }),
    ).toThrow(/declared_mode 取值无效/);
  });
});

describe("DeviceBindingFlow 键盘与可访问性", () => {
  afterEach(cleanup);

  function renderFlow() {
    const props = {
      identity: {
        user_id: "person-parent",
        display_name: "妈妈",
      },
      onCreateBinding: vi.fn(),
      onComplete: vi.fn(),
      onBack: vi.fn(),
    };
    render(<DeviceBindingFlow {...props} />);
    return props;
  }

  it("exposes the four canonical modes as keyboard-operable radios with Chinese titles", async () => {
    renderFlow();
    fireEvent.change(screen.getByLabelText("设备码"), {
      target: { value: "dev-claim-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "继续" }));

    const group = screen.getByRole("radiogroup", { name: "主要使用方式" });
    expect(group).toBeInTheDocument();
    const radios = screen.getAllByRole("radio");
    expect(radios.map((radio) => radio.textContent)).toEqual(
      expect.arrayContaining([
        expect.stringContaining("给孩子使用"),
        expect.stringContaining("给自己使用"),
        expect.stringContaining("给父母使用"),
        expect.stringContaining("家庭共同使用"),
      ]),
    );
    for (const radio of radios) {
      expect(radio).toHaveAttribute("aria-checked", "false");
      expect(radio).not.toBeDisabled();
    }
  });

  it("marks parent-self-acceptance offers as pending instead of checkable", async () => {
    renderFlow();
    fireEvent.change(screen.getByLabelText("设备码"), {
      target: { value: "dev-claim-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    fireEvent.click(screen.getByRole("radio", { name: /给父母使用/ }));

    expect(
      screen.getByText(/需父母本人在设备上确认（本次不会代为同意）/),
    ).toBeInTheDocument();
    expect(screen.getAllByText("待父母确认").length).toBeGreaterThan(0);
    // 父母本人确认项不能勾选：它们不是 checkbox。
    expect(
      screen.queryByRole("checkbox", { name: /服务与数据规则（父母本人确认）/ }),
    ).not.toBeInTheDocument();
    // 子女可代确认项仍是可勾选 checkbox。
    expect(
      screen.getByRole("checkbox", { name: /子女设备管理/ }),
    ).toBeInTheDocument();
  });

  it("requires a claim token before proceeding and blocks submission without persona", async () => {
    const props = renderFlow();
    expect(screen.getByRole("button", { name: "继续" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("设备码"), {
      target: { value: "dev-claim-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    fireEvent.click(screen.getByRole("radio", { name: /给自己使用/ }));

    const next = screen.getByRole("button", { name: "查看确认摘要" });
    expect(next).toBeDisabled(); // 未选择伙伴
    fireEvent.click(screen.getByRole("radio", { name: /星澜/ }));
    expect(next).toBeEnabled();
    fireEvent.click(next);
    expect(
      await screen.findByRole("heading", { name: "确认摘要" }),
    ).toBeInTheDocument();
    expect(props.onCreateBinding).not.toHaveBeenCalled();
  });
});
