// @vitest-environment jsdom
import { beforeEach, afterEach, describe, it, expect, vi } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
  act,
} from "@testing-library/react";
import { App } from "../src/App";
const fixture = vi.hoisted(() => ({
  vault: null as any,
  workers: [] as any[],
}));
vi.mock("../src/storage", () => ({
  BrowserStorage: class {
    async get() {
      return { schema: "fixture" };
    }
    async set() {}
  },
}));
vi.mock("../src/vault", () => ({
  ApiError: class extends Error {
    status = 409;
  },
  Vault: class {
    constructor() {
      return fixture.vault;
    }
  },
}));
function document() {
  return {
    id: "a".repeat(64),
    kind: "wiki",
    title: "Existing note",
    body: "Original text",
    summary: "A fixture",
    author: "owner",
    authorship_basis: "declared",
    status: "draft",
    created: "",
    updated: "",
    sources: [],
    links: [],
    tags: [],
  };
}
beforeEach(() => {
  location.hash = "";
  const doc = document(),
    second = {
      ...doc,
      id: "d".repeat(64),
      title: "Second note",
      body: "Second body",
    },
    meta = { ...doc, ordinal: 0, object: "c".repeat(64) };
  fixture.workers = [];
  vi.stubGlobal(
    "Worker",
    class {
      postMessage = vi.fn();
      terminate = vi.fn();
      onmessage: any;
      constructor() {
        fixture.workers.push(this);
      }
    },
  );
  fixture.vault = {
    unlocked: false,
    offline: false,
    recent: [],
    deviceId: "fixture-device",
    publication: {
      generation: 1,
      epoch: 1,
      vault: "fixture",
      manifest: "root1",
    },
    manifest: {
      count: 2,
      navigation: [meta, { ...second, ordinal: 1, object: "e".repeat(64) }],
      receipts: [],
    },
    unlock: vi.fn(async () => {
      fixture.vault.unlocked = true;
    }),
    lock: vi.fn(() => {
      fixture.vault.unlocked = false;
    }),
    drafts: vi.fn(async () => []),
    stage: vi.fn(async () => ({})),
    sync: vi.fn(async () => {}),
    open: vi.fn(async (id: string) => ({
      document: id === second.id ? second : doc,
      revision: null,
    })),
    backlinks: () => [],
    searchWorkerState: () => ({
      publication: fixture.vault.publication,
      manifest: fixture.vault.manifest,
    }),
    save: vi.fn(),
    newDocument: () => doc,
  };
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
async function edit() {
  render(<App />);
  fireEvent.change(screen.getByLabelText("Enrollment package"), {
    target: { value: "{}" },
  });
  fireEvent.change(screen.getByLabelText("Passphrase"), {
    target: { value: "fixture passphrase" },
  });
  fireEvent.click(screen.getByRole("button", { name: /Unlock workspace/ }));
  fireEvent.click(await screen.findByRole("button", { name: /Existing note/ }));
  fireEvent.click(await screen.findByRole("button", { name: /Edit$/ }));
  return screen.getByLabelText("Page content") as HTMLTextAreaElement;
}
function pendingSave() {
  let resolve!: (value: any) => void;
  const promise = new Promise<any>((r) => {
    resolve = r;
  });
  fixture.vault.save.mockReturnValue(promise);
  return () =>
    resolve({
      state: "remote",
      revision: { id: "b".repeat(64), parent: null },
    });
}
describe("editor async completion boundaries", () => {
  it("retains text typed during a save and extends the accepted save on the next submit", async () => {
    const resolve = pendingSave(),
      input = await edit();
    fireEvent.change(input, { target: { value: "Old draft" } });
    fireEvent.click(screen.getByRole("button", { name: /Save and sync/ }));
    await waitFor(() => expect(fixture.vault.save).toHaveBeenCalledTimes(1));
    fireEvent.change(input, { target: { value: "Newer unsaved text" } });
    await act(async () => resolve());
    expect(
      (screen.getByLabelText("Page content") as HTMLTextAreaElement).value,
    ).toBe("Newer unsaved text");
    fireEvent.click(screen.getByRole("button", { name: /Save and sync/ }));
    await waitFor(() => expect(fixture.vault.save).toHaveBeenCalledTimes(2));
    expect(fixture.vault.save.mock.calls[1][0].body).toBe("Newer unsaved text");
    expect(fixture.vault.save.mock.calls[1][1]).toBe("b".repeat(64));
  });
  it("does not reopen an editor when an earlier save finishes after navigation", async () => {
    const resolve = pendingSave(),
      input = await edit();
    fireEvent.change(input, { target: { value: "Saved before navigation" } });
    fireEvent.click(screen.getByRole("button", { name: /Save and sync/ }));
    await waitFor(() => expect(fixture.vault.save).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "▤Wiki" }));
    await waitFor(() =>
      expect(screen.queryByLabelText("Page content")).toBeNull(),
    );
    fireEvent.click(await screen.findByRole("button", { name: /Second note/ }));
    await screen.findByRole("heading", { name: "Second note" });
    await act(async () => resolve());
    expect(screen.getByRole("heading", { name: "Second note" })).toBeTruthy();
  });
  it("clears keys and the worker even when an edited draft cannot be stored", async () => {
    const input = await edit();
    fireEvent.change(input, { target: { value: "Unsaved text" } });
    fixture.vault.stage.mockRejectedValue(
      new Error("device-storage-unavailable"),
    );
    fireEvent.click(screen.getByRole("button", { name: /Lock workspace/ }));
    await screen.findByRole("button", { name: /Unlock workspace/ });
    expect(fixture.vault.unlocked).toBe(false);
    expect(fixture.workers[0].terminate).toHaveBeenCalled();
  });
  it("replaces search state after manual publication refresh", async () => {
    await edit();
    fixture.vault.sync.mockImplementation(async () => {
      fixture.vault.publication = {
        ...fixture.vault.publication,
        generation: 2,
        manifest: "root2",
      };
    });
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    await waitFor(() =>
      expect(fixture.workers.at(-1).postMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          type: "init",
          publication: expect.objectContaining({ generation: 2 }),
        }),
      ),
    );
  });
});
