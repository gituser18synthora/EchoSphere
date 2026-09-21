/** Clipboard access varies by browser, origin and per-site permission, and a
    blocked request must never leave a click with no feedback. Every copy in
    the app goes through here: the async Clipboard API when it answers, a
    legacy `execCommand` copy when it does not, and an explicit reason the
    caller can show when neither worked. */

export interface ClipboardWriteResult {
  ok: boolean;
  /** Why the async Clipboard API did not do the job — shown in fallback UI. */
  reason?: string;
}

/** Chromium leaves `writeText()` pending indefinitely when the document loses
    focus while the write is in flight, so the promise is raced against this
    budget instead of being awaited forever. Well inside the transient
    activation window, so the legacy path still counts as user-initiated. */
const WRITE_TIMEOUT_MS = 1_500;

function withWriteTimeout(promise: Promise<void>): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(
      () => reject(new Error("The browser did not answer the clipboard request.")),
      WRITE_TIMEOUT_MS,
    );
    promise.then(
      () => { window.clearTimeout(timer); resolve(); },
      (error: unknown) => { window.clearTimeout(timer); reject(error); },
    );
  });
}

/** Pre-Clipboard-API copy path: select the text inside an off-screen textarea
    and let the browser copy the selection. Works in non-secure contexts and
    wherever the async API is unavailable or denied. */
function legacyCopy(text: string): boolean {
  if (typeof document.execCommand !== "function") return false;

  const holder = document.createElement("textarea");
  holder.value = text;
  holder.setAttribute("readonly", "");
  // Off-screen, but never display:none or visibility:hidden — an unrendered
  // textarea cannot hold a selection, so the copy would silently do nothing.
  holder.style.position = "fixed";
  holder.style.top = "0";
  holder.style.left = "-9999px";
  holder.style.opacity = "0";
  document.body.appendChild(holder);

  const selection = document.getSelection();
  const previous = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;

  let copied = false;
  try {
    holder.select();
    holder.setSelectionRange(0, text.length);
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }

  holder.remove();
  if (selection && previous) {
    selection.removeAllRanges();
    selection.addRange(previous);
  }
  return copied;
}

/** Never rejects: an unusable clipboard is a result, not an exception, so the
    caller can always show either a confirmation or a manual-copy fallback. */
export async function copyToClipboard(text: string): Promise<ClipboardWriteResult> {
  let reason = "This browser did not expose clipboard access to the page.";

  const api = navigator.clipboard;
  if (api && typeof api.writeText === "function") {
    try {
      await withWriteTimeout(Promise.resolve(api.writeText(text)));
      return { ok: true };
    } catch (error) {
      reason = error instanceof Error && error.message
        ? error.message
        : "The browser refused the clipboard request.";
    }
  }

  if (legacyCopy(text)) return { ok: true };
  return { ok: false, reason };
}

/** Reading has no legacy equivalent — Firefox and Safari never grant it to a
    page — so a blocked read returns null and the caller offers manual paste. */
export async function readFromClipboard(): Promise<string | null> {
  const api = navigator.clipboard;
  if (!api || typeof api.readText !== "function") return null;
  try {
    return await api.readText();
  } catch {
    return null;
  }
}
