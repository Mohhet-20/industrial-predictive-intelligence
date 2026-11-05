import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, ApiError } from "@/api/client";
import type {
  CopilotConversationResponse,
  CopilotContextType,
  CopilotMessageResponse,
  CopilotStatusResponse,
  CopilotSuggestedQuestionsResponse,
} from "@/api/types";

/** Evidence-grounded AI copilot panel. Deliberately embedded in
 * the page it's relevant to (asset detail, an open investigation, an
 * anomaly) rather than a floating global chatbot — it only ever answers
 * about the evidence the current page's context actually has, and it is
 * never the only way to see that evidence: everything it cites is also
 * visible directly on the page around it. See docs/COPILOT.md. */

const CATEGORY_STYLES: Record<string, string> = {
  observed_fact: "bg-slate-100 text-slate-700",
  analytical_result: "bg-amber-100 text-amber-800",
  model_output: "bg-blue-100 text-blue-800",
  human_annotation: "bg-purple-100 text-purple-800",
  interpretation_limitation: "bg-teal-100 text-teal-800",
};

function EvidenceCategoryBadge({ category }: { category: string }) {
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-[11px] font-medium ${CATEGORY_STYLES[category] ?? "bg-slate-100 text-slate-700"}`}>
      {category.replace(/_/g, " ")}
    </span>
  );
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString();
}

function MessageBubble({ message }: { message: CopilotMessageResponse }) {
  const isUser = message.role === "user";
  return (
    <div className={`rounded-lg px-3 py-2 text-sm ${isUser ? "ml-6 bg-brand-50 text-slate-800" : "mr-2 bg-slate-50 text-slate-800"}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-semibold uppercase text-slate-400">{isUser ? "You" : "Copilot"}</span>
        {!isUser && message.provider_used && message.provider_used !== "none" && (
          <span className="text-[11px] text-slate-400">via {message.provider_used}</span>
        )}
      </div>
      {!isUser && message.flagged_ungrounded && (
        <div className="mt-1 rounded border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] text-amber-900">
          This answer was flagged: it either cited evidence that doesn't exist (removed below) or used language this
          platform can't support (e.g. a failure claim). Treat it with extra scrutiny and check the evidence cards yourself.
        </div>
      )}
      <p className="mt-1 whitespace-pre-wrap">{message.content}</p>
      {!isUser && message.grounding && message.grounding.length > 0 && (
        <div className="mt-2 space-y-1.5">
          <p className="text-[11px] font-semibold uppercase text-slate-400">Evidence</p>
          {message.grounding.map((item) => (
            <div key={item.ref} className="rounded border border-slate-200 bg-white p-2 text-xs">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono text-slate-400">[{item.ref}]</span>
                <EvidenceCategoryBadge category={item.category} />
                <span className="font-medium text-slate-700">{item.label}</span>
              </div>
              <p className="mt-1 text-slate-600">{item.text}</p>
              {(item.period_start || item.period_end) && (
                <p className="mt-1 text-[11px] text-slate-400">
                  {item.period_start ? fmtDate(item.period_start) : "?"} – {item.period_end ? fmtDate(item.period_end) : "?"}
                </p>
              )}
              {item.method_version && <p className="text-[11px] text-slate-400">method: {item.method_version}</p>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function CopilotPanel({
  contextType,
  contextId,
  contextLabel,
}: {
  contextType: CopilotContextType;
  contextId: string | null;
  contextLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const queryClient = useQueryClient();

  // A new context (e.g. the user picked a different asset) starts a fresh
  // conversation rather than silently carrying old evidence into a new
  // subject — the copilot's whole point is that its answers are scoped to
  // real, specific evidence, not a running general-purpose chat log.
  useEffect(() => {
    setConversationId(null);
  }, [contextType, contextId]);

  const statusQuery = useQuery({
    queryKey: ["copilot", "status"],
    queryFn: () => api.get<CopilotStatusResponse>("/copilot/status"),
    enabled: open,
    staleTime: 60_000,
  });

  const suggestedQuery = useQuery({
    queryKey: ["copilot", "suggested-questions", contextType, contextId],
    queryFn: () =>
      api.get<CopilotSuggestedQuestionsResponse>("/copilot/suggested-questions", {
        context_type: contextType,
        context_id: contextId ?? undefined,
      }),
    enabled: open,
  });

  const messagesQuery = useQuery({
    queryKey: ["copilot", "conversation", conversationId, "messages"],
    queryFn: () => api.get<CopilotMessageResponse[]>(`/copilot/conversations/${conversationId}/messages`),
    enabled: open && !!conversationId,
  });

  const sendMutation = useMutation({
    mutationFn: async (q: string) => {
      let convId = conversationId;
      if (!convId) {
        const conv = await api.post<CopilotConversationResponse>("/copilot/conversations", {
          context_type: contextType,
          context_id: contextId ?? undefined,
        });
        convId = conv.id;
        setConversationId(convId);
      }
      return api.post<CopilotMessageResponse>(`/copilot/conversations/${convId}/messages`, { question: q });
    },
    onSuccess: () => {
      setQuestion("");
      queryClient.invalidateQueries({ queryKey: ["copilot", "conversation", conversationId, "messages"] });
      // conversationId may have just been set this call — also invalidate broadly for the new id.
      queryClient.invalidateQueries({ queryKey: ["copilot", "conversation"] });
    },
  });

  const ask = (q: string) => {
    if (!q.trim() || sendMutation.isPending) return;
    sendMutation.mutate(q.trim());
  };

  // Rendered inline within the page's own layout — a collapsed bar that
  // expands into a bordered section in normal document flow, styled like
  // every other section on this page (see AssetDetailPage.tsx,
  // IntelligenceWorkspacePage.tsx). Deliberately NOT position:fixed / a
  // floating bubble over the whole app: the copilot only ever knows about
  // the page it's embedded in, and looking like a generic global chat
  // widget would misrepresent that scope.
  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex w-full items-center justify-between rounded-lg border border-dashed border-slate-300 bg-white px-4 py-3 text-left text-sm text-slate-600 hover:border-brand-300 hover:bg-slate-50"
      >
        <span>
          <span className="font-medium text-slate-800">Ask the evidence copilot</span>
          {contextLabel && <span className="text-slate-400"> — about {contextLabel}</span>}
        </span>
        <span className="text-xs text-slate-400">expand ▾</span>
      </button>
    );
  }

  return (
    <div className="flex h-[30rem] flex-col rounded-lg border border-slate-200 bg-white">
      <div className="flex items-center justify-between rounded-t-lg border-b border-slate-200 bg-slate-50 px-4 py-2.5">
        <div>
          <p className="text-sm font-semibold text-slate-900">Evidence copilot</p>
          {contextLabel && <p className="text-[11px] text-slate-500">Context: {contextLabel}</p>}
        </div>
        <button onClick={() => setOpen(false)} className="text-xs text-slate-400 hover:text-slate-700">
          collapse ▴
        </button>
      </div>

      {statusQuery.data && !statusQuery.data.configured && (
        <div className="border-b border-amber-200 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-900">{statusQuery.data.note}</div>
      )}

      <div className="flex-1 space-y-2 overflow-y-auto px-3 py-3">
        {!conversationId && (
          <>
            <p className="text-xs text-slate-500">
              Ask about the real evidence for {contextLabel ?? "this platform"} — anomalies, data quality, investigations, and
              condition history the platform has actually computed. It won't guess at anything it wasn't given.
            </p>
            {suggestedQuery.data && suggestedQuery.data.questions.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-[11px] font-semibold uppercase text-slate-400">Try asking</p>
                {suggestedQuery.data.questions.map((q) => (
                  <button
                    key={q}
                    onClick={() => ask(q)}
                    className="block w-full rounded border border-slate-200 px-2 py-1.5 text-left text-xs text-slate-700 hover:bg-slate-50"
                  >
                    {q}
                  </button>
                ))}
              </div>
            )}
          </>
        )}

        {messagesQuery.data?.map((m) => <MessageBubble key={m.id} message={m} />)}

        {sendMutation.isPending && <p className="text-xs text-slate-400">Retrieving evidence and generating an answer…</p>}
        {sendMutation.isError && (
          <p className="rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-800">
            {(sendMutation.error as ApiError)?.message ?? "Something went wrong."}
          </p>
        )}
      </div>

      <div className="flex items-end gap-2 border-t border-slate-200 p-2.5">
        <textarea
          className="h-14 flex-1 resize-none rounded border border-slate-300 px-2 py-1.5 text-sm"
          placeholder="Ask about this evidence…"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              ask(question);
            }
          }}
        />
        <button
          className="rounded bg-brand-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-700 disabled:opacity-50"
          disabled={!question.trim() || sendMutation.isPending}
          onClick={() => ask(question)}
        >
          Ask
        </button>
      </div>
    </div>
  );
}
