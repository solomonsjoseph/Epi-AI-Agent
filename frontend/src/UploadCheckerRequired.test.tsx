import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import AttachmentComposer from "./AttachmentComposer";
import { getUploadPlug, isUploadPlugUnconfigured } from "./apiClient";
import type { AttachmentManifestSummary } from "./types";

const MESSAGE = "Uploads are disabled until the upload checker is configured";

function renderComposer(props: {
  checkerRequired?: boolean;
  attachDisabled?: boolean;
  checkerOff?: boolean;
  staged?: AttachmentManifestSummary[];
}) {
  return render(
    <AttachmentComposer
      action={null}
      attachDisabled={props.attachDisabled}
      checkerOff={props.checkerOff}
      checkerRequired={props.checkerRequired}
      disabled={false}
      errors={[]}
      isUploading={false}
      onDismissError={vi.fn()}
      onFilesSelected={vi.fn()}
      onRemove={vi.fn(async () => undefined)}
      staged={props.staged ?? []}
    />,
  );
}

describe("Upload checker required but not configured", () => {
  it("says uploads are off and keeps the picker disabled", () => {
    renderComposer({ checkerRequired: true, attachDisabled: true });
    expect(screen.getByText(MESSAGE).getAttribute("role")).toBe("status");
    expect(screen.queryByText("PHI checker: off")).toBeNull();
    expect(
      (screen.getByRole("button", { name: "Attach files" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByTestId("attachment-file-input") as HTMLInputElement).disabled,
    ).toBe(true);
  });

  it("is not shown when the checker is simply off", () => {
    renderComposer({ checkerOff: true });
    expect(screen.queryByText(MESSAGE)).toBeNull();
    expect(screen.getByText("PHI checker: off")).toBeTruthy();
  });

  it("tells the required-but-missing answer apart from plug info and from no plug", async () => {
    expect(isUploadPlugUnconfigured({ required: true, configured: false })).toBe(true);
    expect(isUploadPlugUnconfigured(null)).toBe(false);
    expect(
      isUploadPlugUnconfigured({ origin: "http://127.0.0.1:8090", contract: "4", caps: {} }),
    ).toBe(false);
    expect(isUploadPlugUnconfigured({ required: "true", configured: false })).toBe(false);
    const fetchImpl = vi.fn(
      async () =>
        new Response(JSON.stringify({ required: true, configured: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    const answer = await getUploadPlug(fetchImpl as unknown as typeof fetch);
    expect(isUploadPlugUnconfigured(answer)).toBe(true);
  });
});

describe("Uploaded file names from the upload checker", () => {
  it("shows a name with spaces and parentheses as it was uploaded", () => {
    renderComposer({
      staged: [
        {
          id: "attachment-" + "2".repeat(32),
          filename: "Riverbend Heart (v2)_DATA.csv",
          kind: "tabular",
          mime: "text/csv",
          byte_size: 120,
          status: "staged",
          deidentified: true,
        },
      ],
    });
    expect(screen.getByText("Riverbend Heart (v2)_DATA.csv")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Remove Riverbend Heart (v2)_DATA.csv" }),
    ).toBeTruthy();
  });
});
