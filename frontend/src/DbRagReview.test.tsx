import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import DbRagReview from "./DbRagReview";
import type { ActiveInterrupt } from "./types";

afterEach(cleanup);

function review(): Extract<ActiveInterrupt, { type: "dataset_plan_review" }> {
  return {
    id: "review-1",
    type: "dataset_plan_review",
    artifact: { id: "plan", kind: "dataset_plan", version: 1, expected_status: "draft" },
    view: {
      dataset_title: "Participants",
      goal: "Review participant fields",
      concept_groups: ["age", "sex", "race"].map((name) => ({
        concept_id: name,
        concept_label: name,
        columns: [{ key: `source::participants::${name}`, table: "participants", column: name, roles: ["requested"], selected: true }],
      })),
      selected_fields: ["age", "sex", "race"].map((name) => `source::participants::${name}`),
      filters: [], joins: [], unresolved_scientific_choices: [],
    },
  };
}

describe("concept selection navigation", () => {
  it("continues after clearing the only variable and preserves exclusion on return and approval", () => {
    const onDecision = vi.fn();
    render(<DbRagReview interrupt={review()} onDecision={onDecision} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "participants · age" }));
    expect(screen.getByRole("button", { name: "Approve & continue" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Approve & continue" }));
    expect(screen.getByRole("heading", { name: "sex" })).toBeVisible();
    expect(onDecision).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Previous concept" }));
    expect(screen.getByRole("checkbox", { name: "participants · age" })).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "All concepts" }));
    fireEvent.click(screen.getByRole("button", { name: "Approve plan and extract" }));
    expect(onDecision).toHaveBeenCalledWith({ action: "approve", selected_column_keys: ["source::participants::race", "source::participants::sex"] });
  });

  it("preserves an empty concept when switching between both review modes", () => {
    render(<DbRagReview interrupt={review()} onDecision={vi.fn()} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "participants · age" }));
    fireEvent.click(screen.getByRole("button", { name: "All concepts" }));
    fireEvent.click(screen.getByRole("button", { name: "Step by step" }));
    expect(screen.getByRole("checkbox", { name: "participants · age" })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Approve & continue" })).toBeEnabled();
  });

  it("keeps navigation disabled while a review request is in flight", () => {
    render(<DbRagReview disabled interrupt={review()} onDecision={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Approve & continue" })).toBeDisabled();
  });
});
