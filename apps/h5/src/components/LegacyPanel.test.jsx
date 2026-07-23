import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LegacyPanel } from "./LegacyPanel.jsx";

const digest = "a".repeat(64);

function grant(overrides = {}) {
  return {
    grant_id: "legacy-grant-1",
    role: "owner",
    grantee_username: "family-member",
    version_id: "digital-self-7",
    version_number: 7,
    manifest_sha256: "b".repeat(64),
    relationship_profile_id: "relationship-1",
    allowed_items: [
      { kind: "memory_claim", item_id: "memory-1" },
      { kind: "decision_case", item_id: "decision-1" },
    ],
    scope_sha256: "c".repeat(64),
    grant_snapshot_sha256: digest,
    voice_allowed: false,
    expires_at: "2026-08-01T00:00:00Z",
    status: "pending",
    revision: 1,
    created_at: "2026-07-23T00:00:00Z",
    activated_at: null,
    revoked_at: null,
    shell: null,
    ...overrides,
  };
}

afterEach(cleanup);

describe("LegacyPanel", () => {
  it("keeps identity disclosure and exact grant metadata visible during an owner preview", () => {
    const startLegacy = vi.fn();
    render(
      <LegacyPanel
        role="owner"
        grants={[grant()]}
        versions={[]}
        relationships={[]}
        startLegacy={startLegacy}
      />,
    );

    expect(
      screen.getByText("基于冻结资料生成的数字分身，不是本人"),
    ).toBeInTheDocument();
    expect(screen.getByText("数字分身版本 v7 · digital-self-7")).toBeInTheDocument();
    expect(screen.getByText(`授权摘要 ${digest}`)).toBeInTheDocument();
    expect(screen.getByText("授权范围 2 项")).toBeInTheDocument();
    expect(screen.queryByText("memory-1")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "在世预演" }));
    expect(startLegacy).toHaveBeenCalledWith({
      interactionMode: "legacy",
      legacyGrantId: "legacy-grant-1",
      legacyActorRole: "owner_preview",
    });
  });

  it("creates an owner grant from frozen metadata without exposing private text", () => {
    const create = vi.fn();
    render(
      <LegacyPanel
        role="owner"
        grants={[]}
        versions={[{
          version_id: "digital-self-7",
          version_number: 7,
          status: "frozen",
          manifest_sha256: "b".repeat(64),
          manifest: {
            entries: [
              {
                entry_type: "memory_claim",
                claim_id: "memory-1",
                value: "这段私人正文不得显示",
                sensitive_domain: "family",
              },
              {
                entry_type: "decision_case",
                case_id: "decision-1",
                sharing_scope: "family",
              },
              {
                entry_type: "cognitive_claim",
                claim_id: "private-belief",
                sharing_scope: "private",
              },
              { entry_type: "persona_trait", trait_id: "unknown-style" },
            ],
          },
        }]}
        relationships={[{
          relationship_profile_id: "relationship-1",
          version_number: 3,
          status: "approved",
          step_up_verified: true,
          sharing_scope: "family",
          label: "家人",
          private_notes: "关系禁区正文不得显示",
        }, {
          relationship_profile_id: "private-relationship",
          version_number: 1,
          status: "approved",
          step_up_verified: true,
          sharing_scope: "private",
          label: "私密关系",
        }]}
        create={create}
      />,
    );

    expect(screen.queryByText("这段私人正文不得显示")).not.toBeInTheDocument();
    expect(screen.queryByText("关系禁区正文不得显示")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /private-belief/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /unknown-style/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "私密关系" })).not.toBeInTheDocument();
    expect(screen.getByText(/私密或范围未知的内容不会进入授权/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("接收人用户名"), {
      target: { value: "family-member" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /memory_claim/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /decision_case/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: "允许使用版本绑定的个人声音" }));
    fireEvent.change(screen.getByLabelText("授权到期时间"), {
      target: { value: "2026-08-01T08:00" },
    });
    fireEvent.change(screen.getByLabelText("当前账号密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建传承授权" }));

    expect(create).toHaveBeenCalledWith({
      grantee_username: "family-member",
      version_id: "digital-self-7",
      relationship_profile_id: "relationship-1",
      allowed_items: [
        { kind: "memory_claim", item_id: "memory-1" },
        { kind: "decision_case", item_id: "decision-1" },
      ],
      voice_allowed: true,
      expires_at: new Date("2026-08-01T08:00").toISOString(),
      password: "safe-passphrase",
      idempotency_key: expect.any(String),
    });
  });

  it("uses the exact grant snapshot for owner activation and revocation", () => {
    const activate = vi.fn();
    const revoke = vi.fn();
    const { rerender } = render(
      <LegacyPanel
        role="owner"
        grants={[grant()]}
        activate={activate}
        revoke={revoke}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "激活授权" }));
    const activationPassword = screen.getByLabelText("激活授权的账号密码");
    expect(activationPassword).toHaveFocus();
    fireEvent.change(activationPassword, { target: { value: "safe-passphrase" } });
    fireEvent.click(screen.getByRole("button", { name: "确认激活" }));
    expect(activate).toHaveBeenCalledWith("legacy-grant-1", {
      expected_grant_snapshot_sha256: digest,
      password: "safe-passphrase",
      idempotency_key: expect.any(String),
    });

    rerender(
      <LegacyPanel
        role="owner"
        grants={[grant({ status: "active", activated_at: "2026-07-23T01:00:00Z" })]}
        activate={activate}
        revoke={revoke}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "撤销授权" }));
    fireEvent.change(screen.getByLabelText("撤销授权的账号密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认撤销" }));
    expect(revoke).toHaveBeenCalledWith("legacy-grant-1", {
      expected_grant_snapshot_sha256: digest,
      password: "safe-passphrase",
      idempotency_key: expect.any(String),
    });
  });

  it("lets a grantee enter only an active, unexpired and unrevoked grant", () => {
    const startLegacy = vi.fn();
    render(
      <LegacyPanel
        role="grantee"
        grants={[
          grant({ grant_id: "pending", role: "grantee" }),
          grant({ grant_id: "active", role: "grantee", status: "active" }),
          grant({ grant_id: "expired", role: "grantee", status: "expired" }),
          grant({ grant_id: "revoked", role: "grantee", status: "revoked" }),
        ]}
        startLegacy={startLegacy}
      />,
    );

    expect(screen.getAllByRole("button", { name: "进入传承对话" })).toHaveLength(1);
    expect(screen.getByText("授权已过期，无法进入。")).toBeInTheDocument();
    expect(screen.getByText("授权已撤销，无法进入。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "激活授权" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "进入传承对话" }));
    expect(startLegacy).toHaveBeenCalledWith({
      interactionMode: "legacy",
      legacyGrantId: "active",
      legacyActorRole: "grantee",
    });
  });

  it("updates only relationship-shell preferences with revision CAS", () => {
    const updatePreferences = vi.fn();
    render(
      <LegacyPanel
        role="grantee"
        grants={[grant({
          role: "grantee",
          status: "active",
          shell: {
            shell_id: "shell-1",
            revision: 4,
            preferred_response_length: "balanced",
            question_frequency: "occasional",
          },
        })]}
        updatePreferences={updatePreferences}
      />,
    );

    fireEvent.change(screen.getByLabelText("回答长度"), {
      target: { value: "brief" },
    });
    fireEvent.change(screen.getByLabelText("提问频率"), {
      target: { value: "rare" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存互动偏好" }));
    expect(updatePreferences).toHaveBeenCalledWith("shell-1", {
      expected_revision: 4,
      preferred_response_length: "brief",
      question_frequency: "rare",
      idempotency_key: expect.any(String),
    });
    expect(screen.getByText(/这些偏好只作用于接收人的关系外壳/)).toBeInTheDocument();
  });

  it("traps keyboard focus and closes on Escape", () => {
    const onClose = vi.fn();
    render(<LegacyPanel role="owner" grants={[]} onClose={onClose} />);

    const close = screen.getByRole("button", { name: "关闭传承模式" });
    const ownerTab = screen.getByRole("tab", { name: "授权人视角" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(ownerTab).toHaveFocus();
    fireEvent.keyDown(ownerTab, { key: "ArrowRight" });
    const granteeTab = screen.getByRole("tab", { name: "接收人视角" });
    expect(granteeTab).toHaveFocus();
    expect(granteeTab).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(granteeTab, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });
});
