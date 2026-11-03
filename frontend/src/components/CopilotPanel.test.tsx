import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import { CopilotPanel } from "@/components/CopilotPanel";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, api: { get: vi.fn(), post: vi.fn() } };
});

const mockedApi = vi.mocked(api);

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <CopilotPanel contextType="asset" contextId="asset-1" contextLabel="PUMP-01 (Test Pump)" />
    </QueryClientProvider>
  );
}

describe("CopilotPanel", () => {
  beforeEach(() => {
    mockedApi.get.mockReset();
    mockedApi.post.mockReset();
  });

  it("starts collapsed, scoped to the given context", () => {
    renderPanel();
    expect(screen.getByText(/ask the evidence copilot/i)).toBeInTheDocument();
    expect(screen.getByText(/PUMP-01/)).toBeInTheDocument();
  });

  it("shows the not-configured banner and suggested questions once expanded", async () => {
    mockedApi.get.mockImplementation(async (path: string) => {
      if (path === "/copilot/status") {
        return { provider: "none", configured: false, model: null, note: "No AI provider is configured for this deployment." };
      }
      if (path === "/copilot/suggested-questions") {
        return { context_type: "asset", context_id: "asset-1", questions: ["What sensors are on this asset?"] };
      }
      throw new Error(`unexpected GET ${path}`);
    });

    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText(/ask the evidence copilot/i));

    await waitFor(() => expect(screen.getByText(/no ai provider is configured/i)).toBeInTheDocument());
    expect(screen.getByText("What sensors are on this asset?")).toBeInTheDocument();
  });

  it("sending a question creates a conversation, posts the message, and renders the grounded answer with evidence", async () => {
    mockedApi.get.mockImplementation(async (path: string) => {
      if (path === "/copilot/status") {
        return { provider: "none", configured: false, model: null, note: "No AI provider is configured." };
      }
      if (path === "/copilot/suggested-questions") {
        return { context_type: "asset", context_id: "asset-1", questions: ["What sensors are on this asset?"] };
      }
      if (path === "/copilot/conversations/conv-1/messages") {
        return [
          { id: "m1", conversation_id: "conv-1", role: "user", content: "What sensors are on this asset?", grounding: null, provider_used: null, flagged_ungrounded: false, created_at: "2026-01-01T00:00:00Z" },
          {
            id: "m2", conversation_id: "conv-1", role: "assistant",
            content: "No AI provider is configured for this deployment, so this is the retrieved evidence itself.",
            grounding: [
              { ref: "E1", category: "observed_fact", source_type: "asset", source_id: "asset-1", label: "PUMP-01 (Test Pump)", period_start: null, period_end: null, method_version: null, text: "Asset PUMP-01 has one sensor." },
            ],
            provider_used: "none", flagged_ungrounded: false, created_at: "2026-01-01T00:00:01Z",
          },
        ];
      }
      throw new Error(`unexpected GET ${path}`);
    });
    mockedApi.post.mockImplementation(async (path: string) => {
      if (path === "/copilot/conversations") {
        return { id: "conv-1", organization_id: "org-1", user_id: "user-1", context_type: "asset", context_id: "asset-1", title: null, created_at: "2026-01-01T00:00:00Z" };
      }
      if (path === "/copilot/conversations/conv-1/messages") {
        return {
          id: "m2", conversation_id: "conv-1", role: "assistant",
          content: "No AI provider is configured for this deployment, so this is the retrieved evidence itself.",
          grounding: [{ ref: "E1", category: "observed_fact", source_type: "asset", source_id: "asset-1", label: "PUMP-01 (Test Pump)", period_start: null, period_end: null, method_version: null, text: "Asset PUMP-01 has one sensor." }],
          provider_used: "none", flagged_ungrounded: false, created_at: "2026-01-01T00:00:01Z",
        };
      }
      throw new Error(`unexpected POST ${path}`);
    });

    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText(/ask the evidence copilot/i));
    await waitFor(() => expect(screen.getByText("What sensors are on this asset?")).toBeInTheDocument());

    await user.click(screen.getByText("What sensors are on this asset?"));

    await waitFor(() => expect(mockedApi.post).toHaveBeenCalledWith("/copilot/conversations", { context_type: "asset", context_id: "asset-1" }));
    await waitFor(() =>
      expect(mockedApi.post).toHaveBeenCalledWith("/copilot/conversations/conv-1/messages", { question: "What sensors are on this asset?" })
    );

    await waitFor(() => expect(screen.getByText(/retrieved evidence itself/i)).toBeInTheDocument());
    expect(screen.getByText("[E1]")).toBeInTheDocument();
    expect(screen.getByText("Asset PUMP-01 has one sensor.")).toBeInTheDocument();
  });

  it("shows a grounding warning banner when a message comes back flagged", async () => {
    mockedApi.get.mockImplementation(async (path: string) => {
      if (path === "/copilot/status") return { provider: "anthropic", configured: true, model: "claude-3-5-haiku-20241022", note: "configured" };
      if (path === "/copilot/suggested-questions") return { context_type: "asset", context_id: "asset-1", questions: [] };
      if (path === "/copilot/conversations/conv-2/messages") {
        return [
          {
            id: "m3", conversation_id: "conv-2", role: "assistant",
            content: "This asset will fail soon, confirmed by [E1].",
            grounding: [{ ref: "E1", category: "model_output", source_type: "anomaly", source_id: "a1", label: "Anomaly", period_start: null, period_end: null, method_version: null, text: "detected" }],
            provider_used: "anthropic", flagged_ungrounded: true, created_at: "2026-01-01T00:00:01Z",
          },
        ];
      }
      throw new Error(`unexpected GET ${path}`);
    });
    mockedApi.post.mockImplementation(async (path: string) => {
      if (path === "/copilot/conversations") return { id: "conv-2", organization_id: "org-1", user_id: "user-1", context_type: "asset", context_id: "asset-1", title: null, created_at: "2026-01-01T00:00:00Z" };
      return {
        id: "m3", conversation_id: "conv-2", role: "assistant", content: "This asset will fail soon, confirmed by [E1].",
        grounding: [{ ref: "E1", category: "model_output", source_type: "anomaly", source_id: "a1", label: "Anomaly", period_start: null, period_end: null, method_version: null, text: "detected" }],
        provider_used: "anthropic", flagged_ungrounded: true, created_at: "2026-01-01T00:00:01Z",
      };
    });

    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText(/ask the evidence copilot/i));
    const textarea = await screen.findByPlaceholderText(/ask about this evidence/i);
    await user.type(textarea, "Will this fail?");
    await user.click(screen.getByRole("button", { name: /^ask$/i }));

    await waitFor(() => expect(screen.getByText(/this answer was flagged/i)).toBeInTheDocument());
  });
});
