import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api, ApiError } from "@/api/client";
import type {
  AnnotationResponse,
  AssetIntelligenceSummary,
  AssetResponse,
  AssetTimelineResult,
  CrossSensorContextResult,
  EvidenceLinkResponse,
  EvidenceProvenanceResult,
  InvestigationAnnotationType,
  InvestigationEvidenceType,
  InvestigationResponse,
  InvestigationStatus,
  InvestigationStatusEventResponse,
  Page,
  PrioritizedSignalsResult,
  RelatedInvestigationResponse,
  SensorResponse,
} from "@/api/types";
import { CopilotPanel } from "@/components/CopilotPanel";
import { EmptyState, ErrorState, LoadingState } from "@/components/StateViews";

function isoLocal(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString();
}

const CATEGORY_STYLES: Record<string, string> = {
  observed_data: "bg-slate-100 text-slate-700",
  analytical_signal: "bg-amber-100 text-amber-800",
  model_output: "bg-blue-100 text-blue-800",
  human_interpretation: "bg-purple-100 text-purple-800",
  governance: "bg-teal-100 text-teal-800",
};
function CategoryBadge({ category }: { category: string }) {
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${CATEGORY_STYLES[category] ?? "bg-slate-100 text-slate-700"}`}>{category.replace(/_/g, " ")}</span>;
}

const STATUS_STYLES: Record<InvestigationStatus, string> = {
  open: "bg-blue-100 text-blue-800",
  under_review: "bg-amber-100 text-amber-800",
  awaiting_context: "bg-orange-100 text-orange-800",
  resolved: "bg-emerald-100 text-emerald-800",
  closed: "bg-slate-200 text-slate-600",
};
function InvestigationStatusBadge({ status }: { status: InvestigationStatus }) {
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[status]}`}>{status.replace(/_/g, " ")}</span>;
}

const ALLOWED_NEXT_STATUS: Record<InvestigationStatus, InvestigationStatus[]> = {
  open: ["under_review", "awaiting_context", "resolved", "closed"],
  under_review: ["awaiting_context", "resolved", "closed", "open"],
  awaiting_context: ["under_review", "resolved", "closed", "open"],
  resolved: ["under_review", "closed"],
  closed: ["under_review"],
};

const EVIDENCE_TYPES: InvestigationEvidenceType[] = [
  "anomaly", "degradation_signal", "data_quality_issue", "forecast_run", "component_version", "governance_event", "shift_analysis_run", "sensor", "change_candidate",
];

const ANNOTATION_TYPES: InvestigationAnnotationType[] = ["observation", "operational_context", "resolution", "false_positive_assessment", "follow_up_required"];

export function IntelligenceWorkspacePage() {
  const queryClient = useQueryClient();
  const now = useMemo(() => new Date(), []);
  const daysAgo = (d: number) => isoLocal(new Date(now.getTime() - d * 24 * 3600 * 1000));
  const daysAhead = (d: number) => isoLocal(new Date(now.getTime() + d * 24 * 3600 * 1000));

  // -- Asset selection & timeline window ------------------------------------
  const [selectedAssetId, setSelectedAssetId] = useState("");
  const [timelineStart, setTimelineStart] = useState(daysAgo(30));
  const [timelineEnd, setTimelineEnd] = useState(daysAhead(1));
  const [timelineEventTypeFilter, setTimelineEventTypeFilter] = useState<string>("");

  // -- Investigations ---------------------------------------------------------
  const [selectedInvestigationId, setSelectedInvestigationId] = useState<string | null>(null);
  const [showOpenForm, setShowOpenForm] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [statusNotes, setStatusNotes] = useState("");

  // -- Evidence / annotations --------------------------------------------------
  const [evidenceType, setEvidenceType] = useState<InvestigationEvidenceType>("anomaly");
  const [evidenceId, setEvidenceId] = useState("");
  const [evidenceNote, setEvidenceNote] = useState("");
  const [annotationType, setAnnotationType] = useState<InvestigationAnnotationType>("observation");
  const [annotationContent, setAnnotationContent] = useState("");
  const [annotationFollowUp, setAnnotationFollowUp] = useState(false);
  const [provenanceTarget, setProvenanceTarget] = useState<{ evidence_type: string; evidence_id: string } | null>(null);
  const [showTechnicalProvenance, setShowTechnicalProvenance] = useState(false);

  // -- Cross-sensor context -----------------------------------------------------
  const [crossSensorIds, setCrossSensorIds] = useState<string[]>([]);
  const [crossStart, setCrossStart] = useState(daysAgo(7));
  const [crossEnd, setCrossEnd] = useState(daysAgo(0));

  // -- Data --------------------------------------------------------------------
  const assetsQuery = useQuery({ queryKey: ["assets", "workspace"], queryFn: () => api.get<Page<AssetResponse>>("/assets", { limit: 200 }) });
  const sensorsQuery = useQuery({
    queryKey: ["assets", selectedAssetId, "sensors", "workspace"],
    queryFn: () => api.get<Page<SensorResponse>>(`/assets/${selectedAssetId}/sensors`, { limit: 100 }),
    enabled: !!selectedAssetId,
  });

  const prioritizedQuery = useQuery({
    queryKey: ["orchestration", "prioritized-signals"],
    queryFn: () => api.get<{ data: PrioritizedSignalsResult }>("/orchestration/prioritized-signals", { limit: 25 }),
  });

  const summaryQuery = useQuery({
    queryKey: ["orchestration", "summary", selectedAssetId],
    queryFn: () => api.get<{ data: AssetIntelligenceSummary }>(`/orchestration/assets/${selectedAssetId}/summary`),
    enabled: !!selectedAssetId,
  });

  const timelineQuery = useQuery({
    queryKey: ["orchestration", "timeline", selectedAssetId, timelineStart, timelineEnd, timelineEventTypeFilter],
    queryFn: () =>
      api.get<{ data: AssetTimelineResult }>(`/orchestration/assets/${selectedAssetId}/timeline`, {
        start: new Date(timelineStart).toISOString(),
        end: new Date(timelineEnd).toISOString(),
        limit: 100,
        ...(timelineEventTypeFilter ? { event_types: [timelineEventTypeFilter] } : {}),
      }),
    enabled: !!selectedAssetId,
  });

  const investigationsQuery = useQuery({
    queryKey: ["investigations", selectedAssetId],
    queryFn: () => api.get<Page<InvestigationResponse>>("/investigations", { asset_id: selectedAssetId, limit: 100 }),
    enabled: !!selectedAssetId,
  });

  const selectedInvestigation = investigationsQuery.data?.items.find((i) => i.id === selectedInvestigationId) ?? null;

  const statusHistoryQuery = useQuery({
    queryKey: ["investigations", selectedInvestigationId, "status-history"],
    queryFn: () => api.get<InvestigationStatusEventResponse[]>(`/investigations/${selectedInvestigationId}/status-history`),
    enabled: !!selectedInvestigationId,
  });
  const evidenceQuery = useQuery({
    queryKey: ["investigations", selectedInvestigationId, "evidence"],
    queryFn: () => api.get<EvidenceLinkResponse[]>(`/investigations/${selectedInvestigationId}/evidence`),
    enabled: !!selectedInvestigationId,
  });
  const annotationsQuery = useQuery({
    queryKey: ["investigations", selectedInvestigationId, "annotations"],
    queryFn: () => api.get<AnnotationResponse[]>(`/investigations/${selectedInvestigationId}/annotations`),
    enabled: !!selectedInvestigationId,
  });
  const relatedQuery = useQuery({
    queryKey: ["investigations", selectedInvestigationId, "related"],
    queryFn: () => api.get<RelatedInvestigationResponse[]>(`/investigations/${selectedInvestigationId}/related`),
    enabled: !!selectedInvestigationId,
  });

  const provenanceQuery = useQuery({
    queryKey: ["orchestration", "provenance", provenanceTarget?.evidence_type, provenanceTarget?.evidence_id],
    queryFn: () => api.get<{ data: EvidenceProvenanceResult }>(`/orchestration/evidence/${provenanceTarget!.evidence_type}/${provenanceTarget!.evidence_id}/provenance`),
    enabled: !!provenanceTarget,
  });

  const crossSensorQuery = useQuery({
    queryKey: ["orchestration", "cross-sensor", selectedAssetId, crossSensorIds, crossStart, crossEnd],
    queryFn: () =>
      api.get<{ data: CrossSensorContextResult }>(`/orchestration/assets/${selectedAssetId}/cross-sensor-context`, {
        sensor_ids: crossSensorIds, start: new Date(crossStart).toISOString(), end: new Date(crossEnd).toISOString(), freq_seconds: 300,
      }),
    enabled: false,
  });

  // -- Mutations -----------------------------------------------------------
  const invalidateInvestigation = () => {
    queryClient.invalidateQueries({ queryKey: ["investigations"] });
  };

  const openInvestigationMutation = useMutation({
    mutationFn: () => api.post<InvestigationResponse>("/investigations", { asset_id: selectedAssetId, title: newTitle, description: newDescription || undefined }),
    onSuccess: (inv) => {
      setNewTitle("");
      setNewDescription("");
      setShowOpenForm(false);
      setSelectedInvestigationId(inv.id);
      invalidateInvestigation();
    },
  });

  const transitionMutation = useMutation({
    mutationFn: (to_status: InvestigationStatus) => api.post<InvestigationResponse>(`/investigations/${selectedInvestigationId}/status`, { to_status, notes: statusNotes || undefined }),
    onSuccess: () => {
      setStatusNotes("");
      invalidateInvestigation();
      queryClient.invalidateQueries({ queryKey: ["investigations", selectedInvestigationId, "status-history"] });
    },
  });

  const attachEvidenceMutation = useMutation({
    mutationFn: () => api.post<EvidenceLinkResponse>(`/investigations/${selectedInvestigationId}/evidence`, { evidence_type: evidenceType, evidence_id: evidenceId || undefined, relation_note: evidenceNote || undefined }),
    onSuccess: () => {
      setEvidenceId("");
      setEvidenceNote("");
      queryClient.invalidateQueries({ queryKey: ["investigations", selectedInvestigationId, "evidence"] });
    },
  });

  const addAnnotationMutation = useMutation({
    mutationFn: () => api.post<AnnotationResponse>(`/investigations/${selectedInvestigationId}/annotations`, { annotation_type: annotationType, content: annotationContent, follow_up_required: annotationFollowUp }),
    onSuccess: () => {
      setAnnotationContent("");
      setAnnotationFollowUp(false);
      queryClient.invalidateQueries({ queryKey: ["investigations", selectedInvestigationId, "annotations"] });
    },
  });

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Intelligence Workspace</h1>
        <p className="mt-1 text-sm text-slate-600">
          One place to move from a meaningful signal to a structured investigation: follow the evidence across data, detectors, and human review — never a fabricated health score, never an
          automated maintenance decision.
        </p>
      </div>

      {/* ---------------------------------------------------------------- */}
      {/* Prioritized signals — the entry point                            */}
      {/* ---------------------------------------------------------------- */}
      <section className="rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold text-slate-900">Prioritized items</h2>
        <p className="mt-1 text-xs text-slate-500">
          A transparent triage ORDERING — not a probability of failure. Every item shows the exact factors behind its position.
        </p>
        {prioritizedQuery.isLoading && <LoadingState label="Loading prioritized signals..." />}
        {prioritizedQuery.isError && <ErrorState message={(prioritizedQuery.error as ApiError)?.message ?? "Failed to load"} />}
        {prioritizedQuery.data && prioritizedQuery.data.data.items.length === 0 && <EmptyState title="No open signals" description="No open anomalies or degradation signals right now." />}
        {prioritizedQuery.data && prioritizedQuery.data.data.items.length > 0 && (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-slate-500">
                <tr>
                  <th className="py-1 pr-3">Type</th>
                  <th className="py-1 pr-3">Summary</th>
                  <th className="py-1 pr-3">Score</th>
                  <th className="py-1 pr-3">Cross-detector</th>
                  <th className="py-1 pr-3">DQ nearby</th>
                  <th className="py-1 pr-3">Reviewed</th>
                  <th className="py-1 pr-3" />
                </tr>
              </thead>
              <tbody>
                {prioritizedQuery.data.data.items.map((item) => (
                  <tr key={item.id} className="border-t border-slate-100">
                    <td className="py-1.5 pr-3">{item.item_type.replace(/_/g, " ")}</td>
                    <td className="py-1.5 pr-3 max-w-md truncate" title={item.summary}>{item.summary}</td>
                    <td className="py-1.5 pr-3 font-mono">{item.score}</td>
                    <td className="py-1.5 pr-3">{item.contributing_factors.cross_detector_agreement ? "yes" : "—"}</td>
                    <td className="py-1.5 pr-3">{item.contributing_factors.nearby_data_quality_issue_count}</td>
                    <td className="py-1.5 pr-3">{item.contributing_factors.human_reviewed ? "yes" : "no"}</td>
                    <td className="py-1.5 pr-3">
                      <button
                        className="text-xs text-brand-700 underline"
                        onClick={() => setSelectedAssetId(item.asset_id)}
                      >
                        Go to asset
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-xs text-slate-400">{prioritizedQuery.data.data.methodology}</p>
          </div>
        )}
      </section>

      {/* ---------------------------------------------------------------- */}
      {/* Asset selection + cross-layer summary                            */}
      {/* ---------------------------------------------------------------- */}
      <section className="rounded-lg border border-slate-200 bg-white p-5">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-semibold text-slate-900">Select asset</h2>
          <select className="rounded border border-slate-300 px-2 py-1 text-sm" value={selectedAssetId} onChange={(e) => { setSelectedAssetId(e.target.value); setSelectedInvestigationId(null); }}>
            <option value="">Choose an asset...</option>
            {(assetsQuery.data?.items ?? []).map((a) => <option key={a.id} value={a.id}>{a.name} ({a.code})</option>)}
          </select>
        </div>

        {!selectedAssetId && <p className="mt-3 text-sm text-slate-500">Select an asset to see its intelligence summary, timeline, and investigations.</p>}

        {selectedAssetId && summaryQuery.isLoading && <LoadingState label="Loading summary..." />}
        {selectedAssetId && summaryQuery.data && (
          <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-4">
            <SummaryCard title="Observed data" tone="observed_data">
              <p>{summaryQuery.data.data.observed_data.total_data_quality_issues} data-quality issue(s)</p>
              <p className="text-xs text-slate-500">critical: {summaryQuery.data.data.observed_data.issues_by_severity.critical ?? 0}</p>
            </SummaryCard>
            <SummaryCard title="Analytical signals" tone="analytical_signal">
              <p>{summaryQuery.data.data.analytical_signals.open_degradation_signal_count} open degradation signal(s)</p>
            </SummaryCard>
            <SummaryCard title="Model outputs" tone="model_output">
              <p>{summaryQuery.data.data.model_outputs.open_anomaly_count} open anomal(y/ies)</p>
              <p className="text-xs text-slate-500">forecasts justified: {summaryQuery.data.data.model_outputs.forecast_models_justified}/{summaryQuery.data.data.model_outputs.forecast_models_fitted}</p>
            </SummaryCard>
            <SummaryCard title="Human interpretation" tone="human_interpretation">
              <p>{summaryQuery.data.data.human_interpretations.investigation_count} investigation(s)</p>
              <p className="text-xs text-slate-500">{summaryQuery.data.data.human_interpretations.annotation_count} annotation(s)</p>
            </SummaryCard>
          </div>
        )}
        {selectedAssetId && summaryQuery.data && summaryQuery.data.data.limitations.length > 0 && (
          <div className="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            <p className="font-medium">Limitations</p>
            <ul className="mt-1 list-disc pl-4">
              {summaryQuery.data.data.limitations.map((l, i) => <li key={i}>{l}</li>)}
            </ul>
          </div>
        )}
      </section>

      {selectedAssetId && (
        <>
          {/* ---------------------------------------------------------------- */}
          {/* Timeline                                                          */}
          {/* ---------------------------------------------------------------- */}
          <section className="rounded-lg border border-slate-200 bg-white p-5">
            <h2 className="text-sm font-semibold text-slate-900">Asset intelligence timeline</h2>
            <div className="mt-2 flex flex-wrap items-end gap-3 text-sm">
              <label className="flex flex-col text-xs text-slate-500">Start<input type="datetime-local" className="rounded border border-slate-300 px-2 py-1" value={timelineStart} onChange={(e) => setTimelineStart(e.target.value)} /></label>
              <label className="flex flex-col text-xs text-slate-500">End<input type="datetime-local" className="rounded border border-slate-300 px-2 py-1" value={timelineEnd} onChange={(e) => setTimelineEnd(e.target.value)} /></label>
              <label className="flex flex-col text-xs text-slate-500">
                Event type
                <select className="rounded border border-slate-300 px-2 py-1" value={timelineEventTypeFilter} onChange={(e) => setTimelineEventTypeFilter(e.target.value)}>
                  <option value="">All</option>
                  {(timelineQuery.data?.data.available_event_types ?? []).map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
                </select>
              </label>
            </div>
            {timelineQuery.isLoading && <LoadingState label="Loading timeline..." />}
            {timelineQuery.isError && <ErrorState message={(timelineQuery.error as ApiError)?.message ?? "Failed to load"} />}
            {timelineQuery.data && timelineQuery.data.data.events.length === 0 && <EmptyState title="No events in this window" description="Widen the time range or clear the event-type filter." />}
            {timelineQuery.data && timelineQuery.data.data.events.length > 0 && (
              <ul className="mt-3 max-h-96 space-y-2 overflow-y-auto">
                {timelineQuery.data.data.events.map((e, i) => (
                  <li key={i} className="flex items-start gap-3 border-b border-slate-100 pb-2 text-sm">
                    <span className="w-40 shrink-0 text-xs text-slate-400">{fmtDate(e.timestamp)}</span>
                    <CategoryBadge category={e.category} />
                    <span className="flex-1">{e.summary}</span>
                    {(e.source_type === "anomaly" || e.source_type === "degradation_signal") && (
                      <button className="text-xs text-brand-700 underline" onClick={() => { setProvenanceTarget({ evidence_type: e.source_type, evidence_id: e.source_id }); setShowTechnicalProvenance(false); }}>
                        trace
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {timelineQuery.data && <p className="mt-2 text-xs text-slate-400">{timelineQuery.data.data.note}</p>}
          </section>

          {/* ---------------------------------------------------------------- */}
          {/* Investigations                                                    */}
          {/* ---------------------------------------------------------------- */}
          <section className="rounded-lg border border-slate-200 bg-white p-5">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-900">Investigations</h2>
              <button className="rounded bg-brand-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-700" onClick={() => setShowOpenForm((s) => !s)}>
                {showOpenForm ? "Cancel" : "Open investigation"}
              </button>
            </div>

            {showOpenForm && (
              <div className="mt-3 space-y-2 rounded border border-slate-200 p-3">
                <input className="w-full rounded border border-slate-300 px-2 py-1 text-sm" placeholder="Title" value={newTitle} onChange={(e) => setNewTitle(e.target.value)} />
                <textarea className="w-full rounded border border-slate-300 px-2 py-1 text-sm" placeholder="Description (optional)" value={newDescription} onChange={(e) => setNewDescription(e.target.value)} />
                <button
                  className="rounded bg-brand-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-700 disabled:opacity-50"
                  disabled={!newTitle || openInvestigationMutation.isPending}
                  onClick={() => openInvestigationMutation.mutate()}
                >
                  {openInvestigationMutation.isPending ? "Opening..." : "Open"}
                </button>
                {openInvestigationMutation.isError && <ErrorState message={(openInvestigationMutation.error as ApiError)?.message ?? "Failed"} />}
              </div>
            )}

            {investigationsQuery.isLoading && <LoadingState label="Loading investigations..." />}
            {investigationsQuery.data && investigationsQuery.data.items.length === 0 && <EmptyState title="No investigations yet" description="Open one above to start tracking evidence and human context for this asset." />}
            {investigationsQuery.data && investigationsQuery.data.items.length > 0 && (
              <ul className="mt-3 divide-y divide-slate-100">
                {investigationsQuery.data.items.map((inv) => (
                  <li key={inv.id} className={`flex cursor-pointer items-center justify-between py-2 px-2 text-sm hover:bg-slate-50 ${selectedInvestigationId === inv.id ? "bg-slate-50" : ""}`} onClick={() => setSelectedInvestigationId(inv.id)}>
                    <span>{inv.title}</span>
                    <InvestigationStatusBadge status={inv.status} />
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* ---------------------------------------------------------------- */}
          {/* Selected investigation detail                                     */}
          {/* ---------------------------------------------------------------- */}
          {selectedInvestigation && (
            <section className="rounded-lg border border-slate-200 bg-white p-5">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-semibold text-slate-900">{selectedInvestigation.title}</h2>
                  {selectedInvestigation.description && <p className="mt-1 text-xs text-slate-500">{selectedInvestigation.description}</p>}
                </div>
                <InvestigationStatusBadge status={selectedInvestigation.status} />
              </div>

              {/* Status transition */}
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <input className="w-64 rounded border border-slate-300 px-2 py-1 text-xs" placeholder="Notes (optional)" value={statusNotes} onChange={(e) => setStatusNotes(e.target.value)} />
                {ALLOWED_NEXT_STATUS[selectedInvestigation.status].map((s) => (
                  <button key={s} className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-100" disabled={transitionMutation.isPending} onClick={() => transitionMutation.mutate(s)}>
                    → {s.replace(/_/g, " ")}
                  </button>
                ))}
              </div>
              {statusHistoryQuery.data && (
                <ul className="mt-2 text-xs text-slate-500">
                  {statusHistoryQuery.data.map((h) => (
                    <li key={h.id}>{fmtDate(h.created_at)}: {h.from_status ? `${h.from_status} → ${h.to_status}` : "opened"}{h.notes ? ` — ${h.notes}` : ""}</li>
                  ))}
                </ul>
              )}

              {/* Evidence */}
              <div className="mt-5">
                <h3 className="text-xs font-semibold uppercase text-slate-500">Evidence</h3>
                <div className="mt-2 flex flex-wrap items-end gap-2">
                  <select className="rounded border border-slate-300 px-2 py-1 text-xs" value={evidenceType} onChange={(e) => setEvidenceType(e.target.value as InvestigationEvidenceType)}>
                    {EVIDENCE_TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
                  </select>
                  {evidenceType !== "change_candidate" && (
                    <input className="rounded border border-slate-300 px-2 py-1 text-xs" placeholder="evidence id" value={evidenceId} onChange={(e) => setEvidenceId(e.target.value)} />
                  )}
                  <input className="rounded border border-slate-300 px-2 py-1 text-xs" placeholder="relation note (optional)" value={evidenceNote} onChange={(e) => setEvidenceNote(e.target.value)} />
                  <button className="rounded bg-brand-600 px-2 py-1 text-xs font-medium text-white hover:bg-brand-700 disabled:opacity-50" disabled={attachEvidenceMutation.isPending} onClick={() => attachEvidenceMutation.mutate()}>
                    Attach
                  </button>
                </div>
                {evidenceType === "change_candidate" && <p className="mt-1 text-xs text-slate-400">Change candidates have no stable id — attach these from Temporal Analysis, which snapshots the candidate for you.</p>}
                {attachEvidenceMutation.isError && <ErrorState message={(attachEvidenceMutation.error as ApiError)?.message ?? "Failed"} />}

                {evidenceQuery.data && evidenceQuery.data.length === 0 && <p className="mt-2 text-xs text-slate-400">No evidence linked yet.</p>}
                {evidenceQuery.data && evidenceQuery.data.length > 0 && (
                  <ul className="mt-2 space-y-1">
                    {evidenceQuery.data.map((link) => (
                      <li key={link.id} className="flex items-center justify-between rounded border border-slate-100 px-2 py-1 text-xs">
                        <span>
                          <span className="font-medium">{link.evidence_type.replace(/_/g, " ")}</span>
                          {link.evidence_id ? ` — ${link.evidence_id.slice(0, 8)}...` : " (snapshot)"}
                          {link.relation_note && ` — ${link.relation_note}`}
                        </span>
                        {(link.evidence_type === "anomaly" || link.evidence_type === "degradation_signal") && link.evidence_id && (
                          <button className="text-brand-700 underline" onClick={() => { setProvenanceTarget({ evidence_type: link.evidence_type, evidence_id: link.evidence_id! }); setShowTechnicalProvenance(false); }}>
                            trace provenance
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {/* Provenance viewer */}
              {provenanceTarget && (
                <div className="mt-4 rounded border border-slate-200 bg-slate-50 p-3">
                  <div className="flex items-center justify-between">
                    <h4 className="text-xs font-semibold uppercase text-slate-500">Provenance — {provenanceTarget.evidence_type}</h4>
                    <div className="flex items-center gap-2">
                      <button className="text-xs text-brand-700 underline" onClick={() => setShowTechnicalProvenance((s) => !s)}>{showTechnicalProvenance ? "Hide technical detail" : "Show technical detail"}</button>
                      <button className="text-xs text-slate-400" onClick={() => setProvenanceTarget(null)}>close</button>
                    </div>
                  </div>
                  {provenanceQuery.isLoading && <LoadingState label="Tracing..." />}
                  {provenanceQuery.data && (
                    <div className="mt-2 space-y-1 text-xs text-slate-700">
                      {Object.entries(provenanceQuery.data.data.summary).map(([k, v]) => (
                        <p key={k}><span className="font-medium">{k.replace(/_/g, " ")}:</span> {typeof v === "object" ? JSON.stringify(v) : String(v)}</p>
                      ))}
                      {showTechnicalProvenance && (
                        <pre className="mt-2 max-h-64 overflow-auto rounded bg-white p-2 text-[11px]">{JSON.stringify(provenanceQuery.data.data.technical, null, 2)}</pre>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* Annotations */}
              <div className="mt-5">
                <h3 className="text-xs font-semibold uppercase text-slate-500">Human context (annotations)</h3>
                <p className="text-xs text-slate-400">Notes here are human interpretations, not verified training labels — recording one never changes an anomaly's or signal's own status.</p>
                <div className="mt-2 flex flex-wrap items-end gap-2">
                  <select className="rounded border border-slate-300 px-2 py-1 text-xs" value={annotationType} onChange={(e) => setAnnotationType(e.target.value as InvestigationAnnotationType)}>
                    {ANNOTATION_TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
                  </select>
                  <textarea className="w-72 rounded border border-slate-300 px-2 py-1 text-xs" placeholder="What did you observe?" value={annotationContent} onChange={(e) => setAnnotationContent(e.target.value)} />
                  <label className="flex items-center gap-1 text-xs text-slate-600">
                    <input type="checkbox" checked={annotationFollowUp} onChange={(e) => setAnnotationFollowUp(e.target.checked)} /> follow-up required
                  </label>
                  <button className="rounded bg-brand-600 px-2 py-1 text-xs font-medium text-white hover:bg-brand-700 disabled:opacity-50" disabled={!annotationContent || addAnnotationMutation.isPending} onClick={() => addAnnotationMutation.mutate()}>
                    Add
                  </button>
                </div>
                {annotationsQuery.data && annotationsQuery.data.length > 0 && (
                  <ul className="mt-2 space-y-1">
                    {annotationsQuery.data.map((a) => (
                      <li key={a.id} className="rounded border border-slate-100 px-2 py-1 text-xs">
                        <span className="font-medium">[{a.annotation_type.replace(/_/g, " ")}]</span> {a.content} {a.follow_up_required && <span className="text-amber-600">(follow-up required)</span>}
                        <span className="ml-2 text-slate-400">{fmtDate(a.created_at)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {relatedQuery.data && relatedQuery.data.length > 0 && (
                <div className="mt-4">
                  <h3 className="text-xs font-semibold uppercase text-slate-500">Related investigations</h3>
                  <ul className="mt-1 text-xs text-slate-600">
                    {relatedQuery.data.map((r) => <li key={r.id}>{r.related_investigation_id.slice(0, 8)}... {r.note && `— ${r.note}`}</li>)}
                  </ul>
                </div>
              )}
            </section>
          )}

          {/* ---------------------------------------------------------------- */}
          {/* Evidence copilot — bound to the most specific context in view:   */}
          {/* the open investigation if one is selected, otherwise the asset.  */}
          {/* ---------------------------------------------------------------- */}
          {selectedInvestigation ? (
            <CopilotPanel contextType="investigation" contextId={selectedInvestigation.id} contextLabel={selectedInvestigation.title} />
          ) : (
            <CopilotPanel
              contextType="asset"
              contextId={selectedAssetId}
              contextLabel={summaryQuery.data ? `${summaryQuery.data.data.asset_code} (${summaryQuery.data.data.asset_name})` : undefined}
            />
          )}

          {/* ---------------------------------------------------------------- */}
          {/* Cross-sensor context                                              */}
          {/* ---------------------------------------------------------------- */}
          <section className="rounded-lg border border-slate-200 bg-white p-5">
            <h2 className="text-sm font-semibold text-slate-900">Cross-sensor context</h2>
            <p className="mt-1 text-xs text-slate-500">Association, never causation — a shared time window is described as a temporal relationship, not a cause.</p>
            <div className="mt-2 flex flex-wrap items-end gap-3 text-sm">
              <label className="flex flex-col text-xs text-slate-500">
                Sensors (select 2+)
                <select multiple className="h-20 rounded border border-slate-300 px-2 py-1" value={crossSensorIds} onChange={(e) => setCrossSensorIds(Array.from(e.target.selectedOptions, (o) => o.value))}>
                  {(sensorsQuery.data?.items ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </label>
              <label className="flex flex-col text-xs text-slate-500">Start<input type="datetime-local" className="rounded border border-slate-300 px-2 py-1" value={crossStart} onChange={(e) => setCrossStart(e.target.value)} /></label>
              <label className="flex flex-col text-xs text-slate-500">End<input type="datetime-local" className="rounded border border-slate-300 px-2 py-1" value={crossEnd} onChange={(e) => setCrossEnd(e.target.value)} /></label>
              <button
                className="rounded bg-brand-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-700 disabled:opacity-50"
                disabled={crossSensorIds.length < 2}
                onClick={() => crossSensorQuery.refetch()}
              >
                Analyze
              </button>
            </div>
            {crossSensorQuery.isFetching && <LoadingState label="Analyzing..." />}
            {crossSensorQuery.isError && <ErrorState message={(crossSensorQuery.error as ApiError)?.message ?? "Failed"} />}
            {crossSensorQuery.data && (
              <div className="mt-3 text-xs text-slate-700">
                <pre className="max-h-48 overflow-auto rounded bg-slate-50 p-2">{JSON.stringify(crossSensorQuery.data.data.correlation, null, 2)}</pre>
                {crossSensorQuery.data.data.observed_associations.length > 0 && (
                  <ul className="mt-2 list-disc pl-4">
                    {crossSensorQuery.data.data.observed_associations.map((a, i) => <li key={i}>{a}</li>)}
                  </ul>
                )}
                <p className="mt-2 text-slate-400">{crossSensorQuery.data.data.note}</p>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function SummaryCard({ title, tone, children }: { title: string; tone: string; children: React.ReactNode }) {
  return (
    <div className="rounded border border-slate-200 p-3">
      <div className="mb-1"><CategoryBadge category={tone} /></div>
      <p className="text-xs font-semibold text-slate-700">{title}</p>
      <div className="mt-1 text-sm text-slate-600">{children}</div>
    </div>
  );
}
