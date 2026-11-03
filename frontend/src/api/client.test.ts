import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, NetworkError, setAuthToken } from "./client";

describe("api client", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    setAuthToken(null);
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("returns parsed JSON on a successful response", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "abc" }), { status: 200, headers: { "Content-Type": "application/json" } })
    );

    const result = await api.get<{ id: string }>("/assets/abc");
    expect(result).toEqual({ id: "abc" });
  });

  it("sends the auth token as a Bearer header once set", async () => {
    setAuthToken("test-token-123");
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    globalThis.fetch = fetchMock;

    await api.get("/auth/me");

    const [, options] = fetchMock.mock.calls[0];
    const headers = options.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer test-token-123");
  });

  it("throws ApiError with the server's code/message/request_id on a non-ok response", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ error: { code: "not_found", message: "Asset not found.", request_id: "req-1" } }),
        { status: 404 }
      )
    );

    await expect(api.get("/assets/does-not-exist")).rejects.toMatchObject({
      status: 404,
      code: "not_found",
      message: "Asset not found.",
      requestId: "req-1",
    });
  });

  it("falls back to a generic ApiError when the error body isn't JSON", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("<html>502</html>", { status: 502 }));

    let caught: unknown;
    try {
      await api.get("/anything");
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(502);
    expect((caught as ApiError).code).toBe("unknown_error");
  });

  it("throws NetworkError (not ApiError) when fetch itself fails — no response was ever received", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    let caught: unknown;
    try {
      await api.get("/assets");
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(NetworkError);
    expect(caught).not.toBeInstanceOf(ApiError);
  });

  it("translates an aborted request into a NetworkError describing a timeout", async () => {
    const abortError = new DOMException("The operation was aborted.", "AbortError");
    globalThis.fetch = vi.fn().mockRejectedValue(abortError);

    let caught: unknown;
    try {
      await api.get("/slow-endpoint");
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(NetworkError);
    expect((caught as NetworkError).message).toMatch(/timed out/i);
  });

  it("builds query strings from params, including repeated keys for arrays", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("[]", { status: 200 }));
    globalThis.fetch = fetchMock;

    await api.get("/orchestration/prioritized-signals", { limit: 10, evidence_type: ["anomaly", "degradation_signal"] });

    const [url] = fetchMock.mock.calls[0];
    expect(url).toContain("limit=10");
    expect(url).toContain("evidence_type=anomaly");
    expect(url).toContain("evidence_type=degradation_signal");
  });

  it("POSTs a JSON-encoded body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    globalThis.fetch = fetchMock;

    await api.post("/investigations", { asset_id: "a1", title: "Check bearing temp" });

    const [, options] = fetchMock.mock.calls[0];
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body as string)).toEqual({ asset_id: "a1", title: "Check bearing temp" });
  });

  it("returns undefined for a 204 No Content response without attempting to parse a body", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    const result = await api.post("/investigations/inv-1/evidence/link-1");
    expect(result).toBeUndefined();
  });
});
