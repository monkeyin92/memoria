import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DigitalSelfVersions } from "./DigitalSelfVersions.jsx";

const testingVersion = {
  version_id: "digital-self-testing",
  version_number: 3,
  status: "testing",
  manifest_sha256: "a".repeat(64),
  manifest: {
    schema_version: "digital-self-manifest-v2",
    entries: [],
    source_summary: {
      memory_claim_count: 1,
      persona_trait_count: 1,
      cognitive_claim_count: 0,
      decision_case_count: 0,
      relationship_profile_count: 0,
      persona_version_id: null,
      source_summary_sha256: "b".repeat(64),
    },
  },
  source_summary: {
    memory_claim_count: 1,
    persona_trait_count: 1,
    cognitive_claim_count: 0,
    decision_case_count: 0,
    relationship_profile_count: 0,
    persona_version_id: null,
    source_summary_sha256: "b".repeat(64),
  },
  parent_version_id: null,
  rollback_target_version_id: null,
  created_at: "2026-07-23T00:00:00Z",
};

afterEach(cleanup);

describe("DigitalSelfVersions Fidelity gate", () => {
  it("routes testing versions to Fidelity before approval", () => {
    const onOpenFidelity = vi.fn();
    render(
      <DigitalSelfVersions
        versions={[testingVersion]}
        busy=""
        onBuild={vi.fn()}
        onTransition={vi.fn()}
        onOpenFidelity={onOpenFidelity}
      />,
    );

    expect(screen.getByRole("button", { name: "完成忠实度评测" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "批准此版本" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成忠实度评测" }));
    expect(onOpenFidelity).toHaveBeenCalledWith(testingVersion);
    expect(screen.getByText(/先完成主人忠实度评测/)).toBeInTheDocument();
  });

  it("reveals approval only after an owner approve verdict", () => {
    const onTransition = vi.fn();
    render(
      <DigitalSelfVersions
        versions={[testingVersion]}
        busy=""
        fidelityByVersion={{
          [testingVersion.version_id]: { verdict: "approve" },
        }}
        onBuild={vi.fn()}
        onTransition={onTransition}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "批准此版本" }));
    expect(screen.getByRole("alertdialog", { name: "批准此版本" })).toBeInTheDocument();
    expect(onTransition).not.toHaveBeenCalled();
  });

  it("keeps the Fidelity entry disabled when its callback is unavailable", () => {
    render(
      <DigitalSelfVersions
        versions={[testingVersion]}
        busy=""
        onBuild={vi.fn()}
        onTransition={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "完成忠实度评测" })).toBeDisabled();
  });
});
