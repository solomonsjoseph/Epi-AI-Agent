import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import AttachmentComposer from "./AttachmentComposer";
import { setUploadChecker } from "./apiClient";

type Toggle = { on: boolean; disabled: boolean; onChange(on: boolean): void } | null;

function renderComposer(toggle: Toggle, checkerOff = false) {
  return render(
    <AttachmentComposer
      action={null}
      checkerOff={checkerOff}
      checkerToggle={toggle}
      disabled={false}
      errors={[]}
      isUploading={false}
      onDismissError={vi.fn()}
      onFilesSelected={vi.fn()}
      onRemove={vi.fn(async () => undefined)}
      staged={[]}
      zipOnly={toggle?.on ?? false}
    />,
  );
}

describe("PHI checker toggle", () => {
  it("is not shown outside optional mode", () => {
    renderComposer(null);
    expect(screen.queryByRole("switch", { name: "PHI checker" })).toBeNull();
  });

  it("shows the server state and asks for a change", () => {
    const onChange = vi.fn();
    renderComposer({ on: true, disabled: false, onChange });
    const toggle = screen.getByRole("switch", { name: "PHI checker" }) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    expect(screen.getByRole("button", { name: "Attach a study ZIP" })).toBeTruthy();
    fireEvent.click(toggle);
    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("shows the off badge next to the toggle when the user turned it off", () => {
    renderComposer({ on: false, disabled: false, onChange: vi.fn() }, true);
    const toggle = screen.getByRole("switch", { name: "PHI checker" }) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    expect(screen.getByText("PHI checker: off")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Attach files" })).toBeTruthy();
  });

  it("cannot be switched while files are staged", () => {
    const onChange = vi.fn();
    renderComposer({ on: true, disabled: true, onChange });
    const toggle = screen.getByRole("switch", { name: "PHI checker" }) as HTMLInputElement;
    expect(toggle.disabled).toBe(true);
  });
});

describe("setUploadChecker", () => {
  it("sends only the boolean and returns the server's answer", async () => {
    const answer = {
      origin: "http://127.0.0.1:8090",
      contract: "2",
      mode: "optional",
      checker_on: false,
      caps: {
        max_files: 10, max_file_bytes: 1, max_total_bytes: 1,
        max_table_rows: 1, max_table_columns: 1, max_table_cells: 1,
      },
    };
    const fetchImpl = vi.fn(async () =>
      new Response(JSON.stringify(answer), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const result = await setUploadChecker(fetchImpl as unknown as typeof fetch, "", false);
    expect(result.checker_on).toBe(false);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/upload-plug/checker");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ checker_on: false });
  });
});
