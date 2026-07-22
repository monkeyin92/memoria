import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  addSelfModelClaimCounterexample: vi.fn(),
  getSelfModel: vi.fn(),
  reviewSelfModelClaim: vi.fn(),
  reviewSelfModelDecisionCase: vi.fn(),
  reviewSelfModelRelationshipProfile: vi.fn(),
}));

vi.mock("../api.js", () => ({
  addSelfModelClaimCounterexample: mocks.addSelfModelClaimCounterexample,
  getSelfModel: mocks.getSelfModel,
  reviewSelfModelClaim: mocks.reviewSelfModelClaim,
  reviewSelfModelDecisionCase: mocks.reviewSelfModelDecisionCase,
  reviewSelfModelRelationshipProfile: mocks.reviewSelfModelRelationshipProfile,
}));

import { SelfModelPanel } from "./SelfModelPanel.jsx";

const now = "2026-07-22T08:00:00Z";

let selfModel;

function source(overrides = {}) {
  return {
    source_event_id: "event-1",
    relation: "support",
    adopted: true,
    negative: false,
    speaker_class: "owner",
    occurred_at: now,
    excerpt: "我面对重要选择时，会先把事实和边界想清楚。",
    ...overrides,
  };
}

function claim(overrides = {}) {
  return {
    kind: "cognitive_claim",
    claim_id: "claim-1",
    claim_type: "belief",
    statement: "我喜欢把事情先想清楚。",
    context: "面对重要选择时会先整理信息。",
    confidence: 0.82,
    version: 1,
    status: "candidate",
    sharing_scope: "owner_only",
    unresolved_conflict: false,
    sources: [source()],
    owner_reviewed_at: null,
    step_up_verified: false,
    effective: false,
    effective_reasons: ["not_approved"],
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

function highSensitivityClaim(overrides = {}) {
  return claim({
    claim_id: "claim-high",
    claim_type: "value",
    statement: "我不会为了效率牺牲边界。",
    context: "关于价值排序的候选材料。",
    sources: [source()],
    ...overrides,
  });
}

function decisionCase(overrides = {}) {
  return {
    kind: "decision_case",
    case_id: "case-1",
    decision_kind: "real",
    context: "是否接受新的合作邀请",
    options: ["接受", "拒绝"],
    constraints: [],
    chosen_option: "拒绝",
    rejected_options: ["接受"],
    outcome: "保留时间给更重要的事情",
    reflection: "当时觉得边界更重要。",
    still_endorsed: true,
    version: 1,
    status: "candidate",
    sharing_scope: "owner_only",
    unresolved_conflict: false,
    sources: [source()],
    owner_reviewed_at: null,
    step_up_verified: false,
    effective: false,
    effective_reasons: ["not_approved"],
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

function hypotheticalDecisionCase(overrides = {}) {
  return decisionCase({
    case_id: "case-hypothetical",
    decision_kind: "hypothetical",
    context: "如果时间无限，我会怎么安排周末",
    chosen_option: "探索新地方",
    outcome: "这是情境推演",
    reflection: "只是模拟，不等于真实经历。",
    sources: [source()],
    effective_reasons: ["hypothetical_decision"],
    ...overrides,
  });
}

function relationshipProfile(overrides = {}) {
  return {
    kind: "relationship_profile",
    profile_id: "profile-1",
    version_number: 1,
    person_id: "person-1",
    relationship_id: "relationship-1",
    salutation: "小周",
    tone: "平和",
    advice_style: "先问再建议",
    boundaries: ["不代替对方做决定"],
    status: "candidate",
    sharing_scope: "owner_only",
    unresolved_conflict: false,
    sources: [source()],
    owner_reviewed_at: null,
    step_up_verified: false,
    effective: false,
    effective_reasons: ["not_approved"],
    created_at: now,
    ...overrides,
  };
}

function approvedRelationshipProfile(overrides = {}) {
  return relationshipProfile({
    status: "approved",
    effective: true,
    effective_reasons: [],
    ...overrides,
  });
}

describe("SelfModelPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    selfModel = {
      claims: [],
      decision_cases: [],
      relationship_profiles: [],
    };
    mocks.getSelfModel.mockImplementation(async () => selfModel);
  });

  afterEach(cleanup);

  it("shows loading first and keeps candidate material honestly bounded after load", async () => {
    selfModel = {
      claims: [claim()],
      decision_cases: [hypotheticalDecisionCase()],
      relationship_profiles: [relationshipProfile()],
    };

    render(<SelfModelPanel onChanged={vi.fn()} />);

    expect(screen.getByText("同步中")).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "认知、决策与关系" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/候选材料不会自动代表你/)).toBeInTheDocument();
    expect(screen.getByText("情境回答不会自动变成真实决策")).toBeInTheDocument();
    expect(screen.getByText(/不代表对方已经获得任何记忆访问权/)).toBeInTheDocument();
    expect(screen.getByText("面对重要选择时会先整理信息。")).toBeInTheDocument();
    fireEvent.click(screen.getAllByText(/本人来源 1 条/)[0]);
    expect(
      screen.getAllByText("我面对重要选择时，会先把事实和边界想清楚。").length,
    ).toBeGreaterThan(0);
  });

  it("confirms an ordinary claim without requiring a password", async () => {
    selfModel = {
      claims: [claim({ claim_type: "belief", status: "candidate", effective: false })],
      decision_cases: [],
      relationship_profiles: [],
    };
    const confirmed = claim({
      claim_type: "belief",
      status: "confirmed",
      effective: true,
      effective_reasons: [],
    });
    mocks.reviewSelfModelClaim.mockImplementation(async () => {
      selfModel = {
        ...selfModel,
        claims: [confirmed],
      };
      return confirmed;
    });
    const onChanged = vi.fn();

    render(<SelfModelPanel onChanged={onChanged} />);

    fireEvent.click(
      await screen.findByRole("button", { name: "确认认知主张：我喜欢把事情先想清楚。" }),
    );

    await waitFor(() => {
      expect(mocks.reviewSelfModelClaim).toHaveBeenCalledWith(
        "claim-1",
        "confirmed",
        1,
        expect.any(String),
        "",
      );
    });
    expect(await screen.findByText("认知主张已由你确认。")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /确认认知主张/ })).not.toBeInTheDocument();
    });
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it("requires counterexamples before a high-sensitivity claim can even be opened", async () => {
    selfModel = {
      claims: [highSensitivityClaim({ sources: [source()] })],
      decision_cases: [],
      relationship_profiles: [],
    };

    render(<SelfModelPanel onChanged={vi.fn()} />);

    const confirm = await screen.findByRole("button", {
      name: "确认认知主张：我不会为了效率牺牲边界。",
    });
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveTextContent("先补充例外");
    expect(screen.getByText(/例外\/反例 0 条/)).toBeInTheDocument();
  });

  it("records an owner counterexample without leaving the review flow", async () => {
    selfModel = {
      claims: [highSensitivityClaim({ sources: [source()] })],
      decision_cases: [],
      relationship_profiles: [],
    };
    mocks.addSelfModelClaimCounterexample.mockImplementation(async () => {
      const updated = highSensitivityClaim({
        version: 2,
        sources: [source(), source({
          source_event_id: "counterexample-1",
          relation: "counterexample",
          adopted: false,
        })],
      });
      selfModel = { ...selfModel, claims: [updated] };
      return updated;
    });

    render(<SelfModelPanel onChanged={vi.fn()} />);

    fireEvent.change(
      await screen.findByLabelText(
        "为认知主张补充例外：我不会为了效率牺牲边界。",
      ),
      {
        target: { value: "当家人安全且风险可控时，我也愿意尝试。" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "保存例外" }));

    await waitFor(() => {
      expect(mocks.addSelfModelClaimCounterexample).toHaveBeenCalledWith(
        "claim-high",
        "当家人安全且风险可控时，我也愿意尝试。",
        1,
        expect.any(String),
      );
    });
    expect(
      await screen.findByText("例外情境已保存，现在可以继续本人审核。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: "确认认知主张：我不会为了效率牺牲边界。",
    })).toBeEnabled();
  });

  it("opens a password dialog for high-sensitivity confirmation, allows Escape, and retries after a failed save", async () => {
    selfModel = {
      claims: [highSensitivityClaim({
        sources: [source(), source({ relation: "counterexample" })],
      })],
      decision_cases: [],
      relationship_profiles: [],
    };
    const confirmed = highSensitivityClaim({
      status: "confirmed",
      effective: true,
      effective_reasons: [],
      sources: [source(), source({ relation: "counterexample" })],
    });
    mocks.reviewSelfModelClaim
      .mockRejectedValueOnce(new Error("step-up denied"))
      .mockImplementationOnce(async () => {
        selfModel = {
          ...selfModel,
          claims: [confirmed],
        };
        return confirmed;
      });

    render(<SelfModelPanel onChanged={vi.fn()} />);

    const trigger = await screen.findByRole("button", {
      name: "确认认知主张：我不会为了效率牺牲边界。",
    });
    expect(trigger).toHaveTextContent("确认");
    fireEvent.click(trigger);

    const dialog = screen.getByRole("alertdialog", { name: "确认高敏认知主张" });
    const password = screen.getByLabelText("账号密码");
    expect(password).toHaveFocus();
    expect(screen.getByText(/例外\/反例 1 条/)).toBeInTheDocument();
    expect(screen.getByText(/请输入账号密码确认/)).toBeInTheDocument();

    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog", { name: "确认高敏认知主张" }))
        .not.toBeInTheDocument();
    });
    expect(trigger).toHaveFocus();

    fireEvent.click(trigger);
    fireEvent.change(screen.getByLabelText("账号密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.submit(screen.getByRole("alertdialog", { name: "确认高敏认知主张" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("step-up denied");
    });
    expect(screen.getByRole("alertdialog", { name: "确认高敏认知主张" }))
      .toBeInTheDocument();

    fireEvent.submit(screen.getByRole("alertdialog", { name: "确认高敏认知主张" }));
    await waitFor(() => {
      expect(mocks.reviewSelfModelClaim).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog", { name: "确认高敏认知主张" }))
        .not.toBeInTheDocument();
    });
    expect(screen.getByText("认知主张已由你确认。")).toBeInTheDocument();
  });

  it("does not allow hypothetical decisions to be confirmed", async () => {
    selfModel = {
      claims: [],
      decision_cases: [hypotheticalDecisionCase()],
      relationship_profiles: [],
    };

    render(<SelfModelPanel onChanged={vi.fn()} />);

    expect(
      await screen.findByText("情境回答不会自动变成真实决策"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "确认真实决策案例：如果时间无限，我会怎么安排周末" }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "撤回决策候选：如果时间无限，我会怎么安排周末" }),
    );

    await waitFor(() => {
      expect(mocks.reviewSelfModelDecisionCase).toHaveBeenCalledWith(
        "case-hypothetical",
        "retracted",
        1,
        expect.any(String),
      );
    });
  });

  it("requires passwords to approve and revoke relationships without granting access", async () => {
    selfModel = {
      claims: [],
      decision_cases: [],
      relationship_profiles: [
        relationshipProfile(),
        approvedRelationshipProfile({
          profile_id: "profile-2",
          version_number: 2,
          salutation: "阿姨",
          status: "approved",
          effective: true,
          effective_reasons: [],
        }),
      ],
    };
    const approved = approvedRelationshipProfile();
    const revoked = approvedRelationshipProfile({
      status: "revoked",
      effective: false,
      effective_reasons: ["no_longer_endorsed"],
    });
    mocks.reviewSelfModelRelationshipProfile.mockImplementation(
      async (profileId, versionNumber, status) => {
        if (status === "approved") {
          selfModel = {
            ...selfModel,
            relationship_profiles: [
              approved,
              selfModel.relationship_profiles[1],
            ],
          };
          return approved;
        }
        selfModel = {
          ...selfModel,
          relationship_profiles: [revoked, revoked],
        };
        return revoked;
      },
    );

    render(<SelfModelPanel onChanged={vi.fn()} />);

    expect(
      await screen.findByText(/不代表对方已经获得任何记忆访问权/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "批准关系画像：小周" }));
    const approveDialog = screen.getByRole("alertdialog", { name: "批准关系画像" });
    expect(screen.getByText(/仍不会授予访问权限/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("账号密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.submit(approveDialog);

    await waitFor(() => {
      expect(mocks.reviewSelfModelRelationshipProfile).toHaveBeenCalledWith(
        "profile-1",
        1,
        "approved",
        "candidate",
        expect.any(String),
        "safe-passphrase",
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "撤销关系画像：小周" }));
    expect(screen.getByRole("alertdialog", { name: "撤销关系画像" }))
      .toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("账号密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.submit(screen.getByRole("alertdialog", { name: "撤销关系画像" }));

    await waitFor(() => {
      expect(mocks.reviewSelfModelRelationshipProfile).toHaveBeenLastCalledWith(
        "profile-1",
        1,
        "revoked",
        "approved",
        expect.any(String),
        "safe-passphrase",
      );
    });
  });
});
