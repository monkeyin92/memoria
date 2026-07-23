import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SelfPreviewPanel } from "./SelfPreviewPanel.jsx";

const approvedVersion = {
  version_id: "digital-self-1",
  version_number: 1,
  status: "approved",
  manifest_sha256: "a".repeat(64),
  source_summary: {
    memory_claim_count: 2,
    persona_trait_count: 1,
    cognitive_claim_count: 1,
    decision_case_count: 1,
    relationship_profile_count: 1,
  },
};

const capability = {
  status: "available",
  registered_owner: true,
  active_owner_voice: true,
};

afterEach(cleanup);

describe("SelfPreviewPanel", () => {
  it("fails closed when the capability is blocked", () => {
    render(
      <SelfPreviewPanel
        versions={[approvedVersion]}
        capability={{
          status: "blocked",
          registered_owner: true,
          missing: ["self_preview_runtime"],
        }}
        busy=""
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("数字分身预览当前不可用")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "开始数字分身预览" })).not.toBeInTheDocument();
    expect(screen.getByText(/数字自我预览运行时将在后续阶段开放/)).toBeInTheDocument();
  });

  it("requires the owner step-up and starts the exact approved version", async () => {
    const onStart = vi.fn();
    render(
      <SelfPreviewPanel
        versions={[approvedVersion]}
        capability={capability}
        busy=""
        onStart={onStart}
        onClose={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText("预览版本"), {
      target: { value: "digital-self-1" },
    });
    fireEvent.change(screen.getByLabelText("当前账号密码", {
      selector: "#self-preview-password",
    }), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "朋友视角" }));
    fireEvent.click(screen.getByRole("button", { name: "开始数字分身预览" }));

    await waitFor(() => {
      expect(onStart).toHaveBeenCalledWith({
        versionId: "digital-self-1",
        manifestSha256: "a".repeat(64),
        password: "safe-passphrase",
        perspective: "friend",
      });
    });
  });

  it("keeps the disclosure visible and allows stop, sources and high-weight feedback", async () => {
    const onStop = vi.fn();
    const onExpandSources = vi.fn();
    const onSubmitFeedback = vi.fn();
    const answer = {
      answer_id: "answer-1",
      turn_id: 4,
      generation_id: 2,
      text: "我会先确认事实，再做决定。",
      epistemic_status: "inference",
      source_refs: [{ kind: "decision_case", item_id: "decision-1" }],
    };
    render(
      <SelfPreviewPanel
        versions={[approvedVersion]}
        capability={capability}
        activePreview={{
          status: "running",
          version_id: approvedVersion.version_id,
          manifest_sha256: approvedVersion.manifest_sha256,
          perspective: "owner",
        }}
        answers={[answer]}
        busy=""
        onStop={onStop}
        onExpandSources={onExpandSources}
        onSubmitFeedback={onSubmitFeedback}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getAllByText("数字分身预览，不代表本人").length).toBeGreaterThan(0);
    expect(screen.getByText("孩子/朋友视角不会授予访问权")).toBeInTheDocument();
    expect(screen.getByText("伙伴风格不进入数字分身")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "停止预览" }));
    expect(onStop).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "展开回答来源" }));
    expect(onExpandSources).toHaveBeenCalledWith(answer);

    fireEvent.click(screen.getByRole("button", { name: "不像我" }));
    await waitFor(() => {
      expect(onSubmitFeedback).toHaveBeenCalledWith(answer, {
        action: "not_like_me",
        correction: "",
      });
    });
  });

  it("does not expose enabled controls without corresponding callbacks", () => {
    render(
      <SelfPreviewPanel
        versions={[approvedVersion]}
        capability={capability}
        activePreview={{
          status: "running",
          version_id: approvedVersion.version_id,
          manifest_sha256: approvedVersion.manifest_sha256,
          perspective: "owner",
        }}
        answers={[
          {
            answer_id: "answer-1",
            text: "没有回调就不能提交。",
            source_refs: [{ kind: "memory_claim", item_id: "memory-1" }],
          },
        ]}
        fidelitySummary={{
          status: "ready",
          current_item: {
            item_id: "holdout-1",
            prompt: "哪一个更像你？",
            category: "fact",
            slots: { A: "回答 A", B: "回答 B" },
          },
        }}
        busy=""
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "开始数字分身预览" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "展开回答来源" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "不像我" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "开始忠实度评测" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "选择回答 A" })).toBeDisabled();
  });

  it("renders blind A/B answer bodies and submits lowercase slot ids", () => {
    const onSubmitFidelity = vi.fn();
    render(
      <SelfPreviewPanel
        versions={[approvedVersion]}
        capability={capability}
        fidelitySummary={{
          status: "ready",
          current_item: {
            item_id: "holdout-1",
            category: "fact",
            prompt: "哪一个更像你？",
            slot_a: "回答来自数字分身。",
            slot_b: "回答来自通用助手。",
          },
        }}
        busy=""
        onSubmitFidelity={onSubmitFidelity}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("回答来自数字分身。")).toBeInTheDocument();
    expect(screen.getByText("回答来自通用助手。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "选择回答 A" }));
    expect(onSubmitFidelity).toHaveBeenCalledWith({
      itemId: "holdout-1",
      preferredSlot: "a",
      versionId: "digital-self-1",
    });
  });

  it("keeps fidelity controls closed for an unavailable coverage state", () => {
    const onStartFidelity = vi.fn();
    render(
      <SelfPreviewPanel
        versions={[approvedVersion]}
        capability={capability}
        fidelitySummary={{ status: "coverage_gap" }}
        busy=""
        onStartFidelity={onStartFidelity}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText(/当前版本覆盖不足/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始忠实度评测" })).toBeDisabled();
  });

  it("keeps unknown or privacy answers out of source correction feedback", () => {
    render(
      <SelfPreviewPanel
        versions={[approvedVersion]}
        capability={capability}
        activePreview={{
          status: "running",
          version_id: approvedVersion.version_id,
          manifest_sha256: approvedVersion.manifest_sha256,
          perspective: "owner",
        }}
        answers={[{
          answer_id: "unknown-1",
          text: "我没有足够的已批准资料来确定回答。",
          epistemic_status: "unknown",
          disclosures: ["digital_identity", "unknown"],
          source_refs: [],
        }]}
        onSubmitFeedback={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "不像我" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "纠正这句话（可选） 提交纠正" })).toBeDisabled();
    expect(screen.getByText(/未知或隐私回答没有可纠正来源/)).toBeInTheDocument();
  });

  it("keeps testing versions out of preview while allowing Fidelity to target them", async () => {
    const testingVersion = {
      ...approvedVersion,
      version_id: "digital-self-testing",
      version_number: 2,
      status: "testing",
    };
    const onStartFidelity = vi.fn();
    render(
      <SelfPreviewPanel
        versions={[testingVersion, approvedVersion]}
        previewVersions={[approvedVersion]}
        evaluationVersions={[testingVersion, approvedVersion]}
        capability={capability}
        busy=""
        fidelitySummary={{ status: "ready", version_id: testingVersion.version_id }}
        onStartFidelity={onStartFidelity}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("combobox", { name: "预览版本" })).not.toHaveTextContent("v2");
    expect(screen.getByRole("combobox", { name: "评测版本" })).toHaveValue(
      "digital-self-testing",
    );
    fireEvent.change(screen.getByLabelText("当前账号密码", {
      selector: "#fidelity-password",
    }), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始忠实度评测" }));
    await waitFor(() => {
      expect(onStartFidelity).toHaveBeenCalledWith(
        testingVersion,
        "safe-passphrase",
      );
    });
  });

  it("requires an explicit owner verdict after every available Fidelity trial", async () => {
    const onCompleteFidelity = vi.fn();
    render(
      <SelfPreviewPanel
        versions={[approvedVersion]}
        capability={capability}
        fidelitySummary={{
          evaluation_id: "evaluation-1",
          version_id: approvedVersion.version_id,
          status: "active",
          all_answered: true,
          current_item: null,
          coverage_gaps: [],
          gates: {
            coverage_complete: true,
            unsupported_fact: true,
            unknown: true,
            privacy: true,
            identity_disclosure: true,
            decision_inference_disclosure: true,
            owner_blind_preference: true,
          },
        }}
        onCompleteFidelity={onCompleteFidelity}
        onClose={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText("结论说明（可选）"), {
      target: { value: "这组回答整体更像我。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "批准此轮评测" }));

    await waitFor(() => {
      expect(onCompleteFidelity).toHaveBeenCalledWith({
        evaluationId: "evaluation-1",
        verdict: "approve",
        rationale: "这组回答整体更像我。",
      });
    });
  });

  it("allows rejection but keeps approval closed when the blind preference gate fails", () => {
    render(
      <SelfPreviewPanel
        versions={[approvedVersion]}
        capability={capability}
        fidelitySummary={{
          evaluation_id: "evaluation-1",
          version_id: approvedVersion.version_id,
          status: "active",
          all_answered: true,
          current_item: null,
          coverage_gaps: [],
          gates: {
            coverage_complete: true,
            unsupported_fact: true,
            unknown: true,
            privacy: true,
            identity_disclosure: true,
            decision_inference_disclosure: true,
            owner_blind_preference: false,
          },
        }}
        onCompleteFidelity={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "批准此轮评测" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "否决此轮评测" })).toBeEnabled();
  });
});
