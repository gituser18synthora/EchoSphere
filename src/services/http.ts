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

/** Envelope meta — pagination fields on list endpoints, warnings on some saves. */
export interface ResponseMeta {
  page?: number;
  pageSize?: number;
  total?: number;
  totalPages?: number;
  warnings?: string[];
}

/** Error thrown for failed requests; `errors` carries the backend's error list. */
export interface ApiRequestError extends Error {
  status?: number;
  errors?: string[];
  /** Structured {field: message} map when the backend returned field errors. */
  fieldErrors?: Record<string, string>;
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

async function request<T>(method: string, path: string, body?: unknown): Promise<{ data: T; meta?: ResponseMeta }> {
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

  /* Backend errors are either plain strings (catalog validation) or {field, message} pairs. */
  let payload: { success?: boolean; data?: T; meta?: ResponseMeta; message?: string; errors?: (string | { field: string; message: string })[] };
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
    const errorList = (payload.errors ?? []).map((e) => (typeof e === "string" ? e : `${e.field}: ${e.message}`));
    const detail = errorList.length ? ` (${errorList.join("; ")})` : "";
    const error = new Error((payload.message || `Request failed (HTTP ${resp.status}).`) + detail) as ApiRequestError;
    error.status = resp.status;
    if (errorList.length) error.errors = errorList;
    const fieldErrors: Record<string, string> = {};
    for (const e of payload.errors ?? []) {
      if (typeof e !== "string" && e.field && !(e.field in fieldErrors)) fieldErrors[e.field] = e.message;
    }
    if (Object.keys(fieldErrors).length) error.fieldErrors = fieldErrors;
    throw error;
  }
  return { data: payload.data as T, meta: payload.meta };
}

/** Raw request that also returns the envelope's meta (e.g. save warnings). */
export const requestWithMeta = request;

export const http = {
  get: async <T>(path: string): Promise<T> => (await request<T>("GET", path)).data,
  getPaged: async <T>(path: string): Promise<Paged<T>> => {
    const { data, meta } = await request<T[]>("GET", path);
    return {
      items: data,
      meta: {
        page: meta?.page ?? 1,
        pageSize: meta?.pageSize ?? data.length,
        total: meta?.total ?? data.length,
        totalPages: meta?.totalPages ?? 1,
      },
    };
  },
  post: async <T>(path: string, body?: unknown): Promise<T> => (await request<T>("POST", path, body)).data,
  /** Multipart POST — pass a FormData; Authorization is attached, Content-Type is left to the browser. */
  postForm: async <T>(path: string, form: FormData): Promise<T> => (await request<T>("POST", path, form)).data,
  put: async <T>(path: string, body?: unknown): Promise<T> => (await request<T>("PUT", path, body)).data,
  patch: async <T>(path: string, body?: unknown): Promise<T> => (await request<T>("PATCH", path, body)).data,
  delete: async <T>(path: string): Promise<T> => (await request<T>("DELETE", path)).data,
};
