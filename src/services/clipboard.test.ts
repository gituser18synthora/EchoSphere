import { afterEach, describe, expect, it, vi } from "vitest";
import { copyToClipboard, readFromClipboard } from "./clipboard";

type ClipboardStub = Partial<Record<"writeText" | "readText", unknown>> | undefined;

function stubClipboard(value: ClipboardStub) {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value });
}

function stubExecCommand(impl: (() => boolean) | null) {
  if (impl === null) {
    delete (document as { execCommand?: unknown }).execCommand;
    return;
  }
  Object.defineProperty(document, "execCommand", { configurable: true, value: impl });
}

afterEach(() => {
  vi.useRealTimers();
  stubClipboard(undefined);
  stubExecCommand(null);
});

describe("copyToClipboard", () => {
  it("uses the async Clipboard API when the browser answers it", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard({ writeText });

    await expect(copyToClipboard("hello")).resolves.toEqual({ ok: true });
    expect(writeText).toHaveBeenCalledWith("hello");
  });

  it("falls back to a legacy copy when the async API is refused, and leaves no stray node", async () => {
    stubClipboard({ writeText: vi.fn().mockRejectedValue(new Error("Write permission denied.")) });
    const exec = vi.fn(() => true);
    stubExecCommand(exec);

    await expect(copyToClipboard("hello")).resolves.toEqual({ ok: true });
    expect(exec).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("copies the legacy way when the page has no Clipboard API at all", async () => {
    stubClipboard(undefined);
    stubExecCommand(() => true);

    await expect(copyToClipboard("hello")).resolves.toEqual({ ok: true });
  });

  it("reports the browser's own reason when no path works", async () => {
    stubClipboard({ writeText: vi.fn().mockRejectedValue(new Error("Write permission denied.")) });
    stubExecCommand(() => false);

    await expect(copyToClipboard("hello")).resolves.toEqual({
      ok: false,
      reason: "Write permission denied.",
    });
  });

  it("never rejects when the Clipboard API throws synchronously", async () => {
    stubClipboard({ writeText: () => { throw new TypeError("illegal invocation"); } });
    stubExecCommand(() => false);

    await expect(copyToClipboard("hello")).resolves.toEqual({
      ok: false,
      reason: "illegal invocation",
    });
  });

  it("gives up on a clipboard promise that never settles instead of hanging the click", async () => {
    vi.useFakeTimers();
    stubClipboard({ writeText: () => new Promise<void>(() => {}) });
    const exec = vi.fn(() => true);
    stubExecCommand(exec);

    const pending = copyToClipboard("hello");
    await vi.advanceTimersByTimeAsync(1_600);

    await expect(pending).resolves.toEqual({ ok: true });
    expect(exec).toHaveBeenCalledWith("copy");
  });
});

describe("readFromClipboard", () => {
  it("returns the clipboard text when the browser grants the read", async () => {
    stubClipboard({ readText: vi.fn().mockResolvedValue("pasted") });
    await expect(readFromClipboard()).resolves.toBe("pasted");
  });

  it("returns null when the read is blocked or unsupported", async () => {
    stubClipboard({ readText: vi.fn().mockRejectedValue(new Error("denied")) });
    await expect(readFromClipboard()).resolves.toBeNull();

    stubClipboard(undefined);
    await expect(readFromClipboard()).resolves.toBeNull();
  });
});
