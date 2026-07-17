/* HTTP client for the EchoSphere backend.
   - Attaches the JWT from localStorage
   - Unwraps the standard envelope {success, data, meta}
   - Normalizes errors to Error(message) so useAsync can render them
   - On 401 clears the session and sends the user to /login */

const TOKEN_KEY = "echosphere.token";
const USER_KEY = "echosphere.session";
const BASE = "/api/v1";

export interface Paged<T> {
  items: T[];
  meta: { page: number; pageSize: number; total: number; totalPages: number };
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function handleUnauthorized() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  if (!window.location.pathname.startsWith("/login")) {
    window.location.assign("/login");
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<{ data: T; meta?: Paged<T>["meta"] }> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  /* FormData bodies must NOT get an explicit Content-Type — the browser
     sets multipart/form-data with the boundary itself. */
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";

  let resp: Response;
  try {
    resp = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
    });
  } catch {
    throw new Error("Cannot reach the server. Check that the backend is running.");
  }

  let payload: { success?: boolean; data?: T; meta?: Paged<T>["meta"]; message?: string; errors?: { field: string; message: string }[] };
  try {
    payload = await resp.json();
  } catch {
    throw new Error(`Unexpected response from the server (HTTP ${resp.status}).`);
  }

  if (resp.status === 401) {
    handleUnauthorized();
    throw new Error(payload?.message || "Session expired — please sign in again.");
  }
  if (!resp.ok || payload.success === false) {
    const detail = payload.errors?.length
      ? ` (${payload.errors.map((e) => `${e.field}: ${e.message}`).join("; ")})`
      : "";
    throw new Error((payload.message || `Request failed (HTTP ${resp.status}).`) + detail);
  }
  return { data: payload.data as T, meta: payload.meta };
}

export const http = {
  get: async <T>(path: string): Promise<T> => (await request<T>("GET", path)).data,
  getPaged: async <T>(path: string): Promise<Paged<T>> => {
    const { data, meta } = await request<T[]>("GET", path);
    return { items: data, meta: meta ?? { page: 1, pageSize: data.length, total: data.length, totalPages: 1 } };
  },
  post: async <T>(path: string, body?: unknown): Promise<T> => (await request<T>("POST", path, body)).data,
  /** Multipart POST — pass a FormData; Authorization is attached, Content-Type is left to the browser. */
  postForm: async <T>(path: string, form: FormData): Promise<T> => (await request<T>("POST", path, form)).data,
  put: async <T>(path: string, body?: unknown): Promise<T> => (await request<T>("PUT", path, body)).data,
  patch: async <T>(path: string, body?: unknown): Promise<T> => (await request<T>("PATCH", path, body)).data,
  delete: async <T>(path: string): Promise<T> => (await request<T>("DELETE", path)).data,
};
