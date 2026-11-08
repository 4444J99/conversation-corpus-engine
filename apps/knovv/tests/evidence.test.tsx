// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { EvidencePicker } from "../src/EvidencePicker";
afterEach(cleanup);
const base = () => ({
  id: "a".repeat(64),
  title: "Current",
  body: "Retain later editing",
  kind: "wiki" as const,
  summary: "",
  author: "owner",
  authorship_basis: "declared",
  status: "draft",
  created: "",
  updated: "",
  sources: [],
  links: [],
  tags: [],
});
describe("source-bound evidence and live links", () => {
  it("uses Unicode code points and never extends beyond a source span", async () => {
    const body = "😀".repeat(100) + "needle" + "😀".repeat(100),
      ref = {
        uid: "synthetic",
        version: "1.0.0",
        message: 0,
        start: 8000,
        end: 8206,
      };
    const source = {
      ...base(),
      id: "b".repeat(64),
      kind: "source",
      title: "Emoji source",
      body,
      sources: [ref],
      ordinal: 0,
      object: "cipher",
    };
    const vault = {
      baseSearch: async () => ({ ordinals: [0] }),
      meta: async () => source,
      overlaySearch: async () => [],
      open: async () => ({ document: source }),
    };
    let value: any = base();
    const onChange = (change: any) => {
      value = typeof change === "function" ? change(value) : change;
    };
    render(
      <EvidencePicker vault={vault as any} value={value} onChange={onChange} />,
    );
    fireEvent.click(screen.getByText("＋ Attach evidence or link a page"));
    fireEvent.change(screen.getByLabelText("Find evidence"), {
      target: { value: "needle" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /Emoji source/ }),
    );
    await vi.waitFor(() => expect(value.sources).toHaveLength(1));
    expect(value.sources[0]).toMatchObject({ start: 8000, end: 8206 });
  });
  it("links a new synced note that is absent from the published index", async () => {
    const note = {
      ...base(),
      id: "d".repeat(64),
      title: "New synced idea",
      ordinal: -1,
      object: "cipher",
    };
    const vault = {
      baseSearch: async () => ({ ordinals: [] }),
      meta: vi.fn(),
      overlaySearch: async () => [note],
    };
    let value: any = base();
    render(
      <EvidencePicker
        vault={vault as any}
        value={value}
        onChange={(change: any) => {
          value = typeof change === "function" ? change(value) : change;
        }}
      />,
    );
    fireEvent.click(screen.getByText("＋ Attach evidence or link a page"));
    fireEvent.change(screen.getByLabelText("Find evidence"), {
      target: { value: "New synced idea" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /New synced idea/ }),
    );
    expect(value.links).toEqual([note.id]);
  });
});
