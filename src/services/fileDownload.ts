import { getToken } from "@/services/http";

export interface FileDownloadRequest {
  url: string;
  fallbackFilename: string;
  accept?: string;
  expectedContentTypes?: string[];
}

function safeClientFilename(value: string, fallback: string): string {
  const leaf = value.split(/[\\/]/).pop()?.trim() ?? "";
  const safe = leaf.replace(/[\u0000-\u001f\u007f<>:"|?*]/g, "-");
  return safe || fallback;
}

export function filenameFromDisposition(
  disposition: string | null,
  fallback: string,
): string {
  if (!disposition) return fallback;

  const encoded = disposition.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      return safeClientFilename(
        decodeURIComponent(encoded.trim().replace(/^"|"$/g, "")),
        fallback,
      );
    } catch {
      // Fall through to the plain filename form.
    }
  }
  const plain = disposition.match(/filename\s*=\s*(?:"([^"]+)"|([^;]+))/i);
  return safeClientFilename((plain?.[1] ?? plain?.[2] ?? "").trim(), fallback);
}

export async function downloadResponseError(response: Response): Promise<Error> {
  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
  if (contentType.includes("json")) {
    try {
      const payload = await response.json() as {
        message?: string;
        errors?: Array<string | { field?: string; message?: string }>;
      };
      const details = (payload.errors ?? [])
        .map((item) => typeof item === "string"
          ? item
          : [item.field, item.message].filter(Boolean).join(": "))
        .filter(Boolean);
      return new Error(
        `${payload.message || `Download failed (HTTP ${response.status}).`}`
        + `${details.length ? ` (${details.join("; ")})` : ""}`,
      );
    } catch {
      // Use the status-only error below.
    }
  }
  return new Error(`Download failed (HTTP ${response.status}).`);
}

export async function downloadFile(request: FileDownloadRequest): Promise<string> {
  const headers: Record<string, string> = {};
  if (request.accept) headers.Accept = request.accept;
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(request.url, { method: "GET", headers });
  } catch {
    throw new Error("Cannot reach the server. Check that the backend is running.");
  }
  if (!response.ok) throw await downloadResponseError(response);

  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
  const expectedTypes = request.expectedContentTypes ?? [];
  if (
    contentType.includes("json")
    || (
      expectedTypes.length > 0
      && !expectedTypes.some((expected) => contentType.includes(expected.toLowerCase()))
    )
  ) {
    throw new Error("The server returned an unexpected file type. Nothing was downloaded.");
  }

  const blob = await response.blob();
  const filename = filenameFromDisposition(
    response.headers.get("content-disposition"),
    safeClientFilename(request.fallbackFilename, "echosphere-download"),
  );
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoking synchronously can cancel a download in some browsers.
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
  return filename;
}
