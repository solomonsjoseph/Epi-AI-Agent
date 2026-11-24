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

describe("revision scope", () => {
  it("sends the current concept with vague feedback and checkbox exclusions", () => {
    const onDecision = vi.fn();
    render(<DbRagReview interrupt={review()} onDecision={onDecision} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve & continue" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "participants · sex" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Revision feedback" }), { target: { value: "don't choose this" } });
    fireEvent.click(screen.getByRole("button", { name: "Request revision" }));
    expect(onDecision).toHaveBeenCalledWith({ action: "revise", feedback: "don't choose this", review_concept_id: "sex", selected_column_keys: ["source::participants::age", "source::participants::race"] });
  });

  it("does not invent a current concept when all concepts are displayed", () => {
    const onDecision = vi.fn();
    render(<DbRagReview interrupt={review()} onDecision={onDecision} />);
    fireEvent.click(screen.getByRole("button", { name: "All concepts" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Revision feedback" }), { target: { value: "Remove sex" } });
    fireEvent.click(screen.getByRole("button", { name: "Request revision" }));
    expect(onDecision.mock.calls[0][0]).not.toHaveProperty("review_concept_id");
  });
});

describe("zero selections", () => {
  function clearAll() {
    fireEvent.click(screen.getByRole("button", { name: "All concepts" }));
    for (const checkbox of screen.getAllByRole("checkbox")) fireEvent.click(checkbox);
  }

  it("explains empty approval and still submits nonempty revision feedback", () => {
    const onDecision = vi.fn();
    render(<DbRagReview interrupt={review()} onDecision={onDecision} />);
    clearAll();
    expect(screen.getByRole("button", { name: "Approve plan and extract" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Select at least one variable");
    fireEvent.change(screen.getByRole("textbox", { name: "Revision feedback" }), { target: { value: "Use marital status instead" } });
    expect(screen.getByRole("button", { name: "Approve plan and extract" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Request revision" }));
    expect(onDecision).toHaveBeenCalledWith({ action: "revise", feedback: "Use marital status instead", selected_column_keys: [] });
  });

  it("requires feedback for empty revision and restores approval when a variable is selected", () => {
    const onDecision = vi.fn();
    render(<DbRagReview interrupt={review()} onDecision={onDecision} />);
    clearAll();
    fireEvent.click(screen.getByRole("button", { name: "Request revision" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Please enter revision feedback.");
    expect(onDecision).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox", { name: "participants · age" }));
    expect(screen.getByRole("button", { name: "Approve plan and extract" })).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
