import type { ApiErrorBody } from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";
const REQUEST_TIMEOUT_MS = 20_000;

export class ApiError extends Error {
  code: string;
  status: number;
  requestId: string | null;

  constructor(status: number, code: string, message: string, requestId: string | null) {
    super(message);
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

/** Thrown when the request never got a response at all — offline, DNS
 * failure, CORS rejection, or the client-side timeout below. Distinct from
 * ApiError (which means "the server answered, and the answer was an
 * error") so a caller that wants to can tell "the API is unreachable"
 * apart from "the API rejected this request" — see StateViews.tsx's
 * NetworkErrorState and docs/RELIABILITY_AND_OPERATIONS.md §9. */
export class NetworkError extends Error {
  cause: unknown;
  constructor(message: string, cause?: unknown) {
    super(message);
    this.cause = cause;
  }
}

let authToken: string | null = null;

export function setAuthToken(token: string | null) {
  authToken = token;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Content-Type", "application/json");
  if (authToken) {
    headers.set("Authorization", `Bearer ${authToken}`);
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { ...options, headers, signal: controller.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new NetworkError(`Request to ${path} timed out after ${REQUEST_TIMEOUT_MS / 1000}s.`, err);
    }
    // fetch() rejects (rather than resolving with a response) only when the
    // request never reached a server at all — offline, DNS failure, a
    // dropped connection, or a CORS rejection. There is no HTTP status to
    // report because there was no HTTP response.
    throw new NetworkError("Could not reach the API. Check your connection and try again.", err);
  } finally {
    clearTimeout(timeoutId);
  }

  if (!response.ok) {
    let body: ApiErrorBody | null = null;
    try {
      body = await response.json();
    } catch {
      // response body wasn't JSON — fall through to a generic error below
    }
    const err = body?.error;
    throw new ApiError(
      response.status,
      err?.code ?? "unknown_error",
      err?.message ?? `Request failed with status ${response.status}`,
      err?.request_id ?? null
    );
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function buildQuery(params: Record<string, string | number | string[] | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) {
      value.forEach((v) => search.append(key, v));
    } else {
      search.append(key, String(value));
    }
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

export const api = {
  get: <T>(path: string, params: Record<string, string | number | string[] | undefined> = {}) =>
    request<T>(`${path}${buildQuery(params)}`),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body !== undefined ? JSON.stringify(body) : undefined }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
