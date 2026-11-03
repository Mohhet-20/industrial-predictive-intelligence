import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError } from "@/api/client";
import type {
  AssetResponse,
  ClosedLoopAnalytics,
  ErrorAnalysisReport,
  FeedbackDatasetBuildResponse,
  FeedbackDatasetBuildSummaryResponse,
  LabelDefinitions,
  OutcomeEvaluationRunResponse,
  OutcomeResponse,
  OutcomeSignalLinkResponse,
  OutcomeSignalType,
  OutcomeType,
  OutcomeVerificationResponse,
  Page,
  PrioritizationPolicyResponse,
  RetrainingCandidateResponse,
  SensorResponse,
  SignalAssessment,
  VerificationStatus,
} from "@/api/types";
import { EmptyState, ErrorState, LoadingState } from "@/components/StateViews";

// ---------------------------------------------------------------------------
// THIS IS A GOVERNED CLOSED-LOOP FOUNDATION, NOT A SELF-LEARNING SYSTEM.
//
// PREDICTIONS ARE NOT TRUTH. HUMAN FEEDBACK IS NOT AUTOMATICALLY GROUND
// TRUTH. MAINTENANCE EVENTS ARE NOT AUTOMATICALLY FAILURES. OUTCOMES MUST
// HAVE PROVENANCE.
//
// Deliberately no single "accuracy" card anywhere on this page — every
// metric is shown with its own computed/coverage context, and every
// outcome/link shows its full verification lineage. See
// docs/CLOSED_LOOP_FEEDBACK.md.
// ---------------------------------------------------------------------------

type TabKey = "outcomes" | "evaluation" | "retraining" | "datasets" | "policy" | "analytics";

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: "outcomes", label: "Outcomes" },
  { key: "evaluation", label: "Evaluation & Error Analysis" },
  { key: "retraining", label: "Retraining Candidates" },
  { key: "datasets", label: "Feedback Datasets" },
  { key: "policy", label: "Prioritization Policy" },
  { key: "analytics", label: "Analytics & Governance" },
];

function fmtNum(n: unknown, digits = 3): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toFixed(digits) : "—";
}

function errMsg(err: unknown): string {
  return err instanceof ApiError ? err.message : err instanceof Error ? err.message : "Unknown error";
}

export function VerificationBadge({ status }: { status: VerificationStatus }) {
  const styles: Record<VerificationStatus, string> = {
    unverified: "bg-slate-100 text-slate-700 border-slate-300",
    reviewed: "bg-blue-50 text-blue-700 border-blue-300",
    verified: "bg-emerald-50 text-emerald-700 border-emerald-300",
    rejected: "bg-red-50 text-red-700 border-red-300",
    conflicting: "bg-amber-50 text-amber-800 border-amber-300",
  };
  return <span className={`rounded border px-2 py-0.5 text-xs font-medium ${styles[status]}`}>{status}</span>;
}

export function AssessmentBadge({ assessment }: { assessment: SignalAssessment }) {
  const styles: Record<SignalAssessment, string> = {
    not_yet_assessed: "bg-slate-100 text-slate-600 border-slate-300",
    confirms: "bg-emerald-50 text-emerald-700 border-emerald-300",
    refutes: "bg-red-50 text-red-700 border-red-300",
    inconclusive: "bg-amber-50 text-amber-800 border-amber-300",
  };
  return <span className={`rounded border px-2 py-0.5 text-xs font-medium ${styles[assessment]}`}>{assessment.replace(/_/g, " ")}</span>;
}

export function ScenarioSeparationNote() {
  return (
    <p className="rounded border border-violet-300 bg-violet-50 px-3 py-2 text-xs text-violet-800">
      Simulated Scenario Lab results can be linked to a real outcome for comparison, but a scenario_run link can
      NEVER count toward real evaluation metrics — this is enforced server-side, not just by convention.
    </p>
  );
}

export function OutcomeLearningCenterPage() {
  const [tab, setTab] = useState<TabKey>("outcomes");

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">Outcome & Learning Center</h1>
        <p className="text-sm text-slate-600">
          A governed closed-loop foundation: model/detector output → human investigation → operational outcome →
          verification → evaluation → feedback. Not automatic retraining, not online learning.
        </p>
      </div>

      <div className="flex gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`border-b-2 px-3 py-2 text-sm font-medium ${
              tab === t.key ? "border-brand-600 text-brand-700" : "border-transparent text-slate-600 hover:text-slate-900"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "outcomes" && <OutcomesTab />}
      {tab === "evaluation" && <EvaluationTab />}
      {tab === "retraining" && <RetrainingTab />}
      {tab === "datasets" && <DatasetsTab />}
      {tab === "policy" && <PolicyTab />}
      {tab === "analytics" && <AnalyticsTab />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Outcomes tab: report, list, verify, link, assess
// ---------------------------------------------------------------------------

function OutcomesTab() {
  const queryClient = useQueryClient();
  const [selectedOutcomeId, setSelectedOutcomeId] = useState<string | null>(null);
  const [reportOpen, setReportOpen] = useState(false);
  const [assetId, setAssetId] = useState("");
  const [sensorId, setSensorId] = useState("");
  const [outcomeType, setOutcomeType] = useState<OutcomeType>("other");
  const [description, setDescription] = useState("");
  const [outcomeTimestamp, setOutcomeTimestamp] = useState("");

  const assetsQuery = useQuery({ queryKey: ["assets", "outcomes"], queryFn: () => api.get<Page<AssetResponse>>("/assets", { limit: 200 }) });
  const sensorsQuery = useQuery({
    queryKey: ["sensors", "outcomes", assetId],
    queryFn: () => api.get<Page<SensorResponse>>(`/assets/${assetId}/sensors`, { limit: 100 }),
    enabled: Boolean(assetId),
  });
  const outcomesQuery = useQuery({ queryKey: ["outcomes"], queryFn: () => api.get<Page<OutcomeResponse>>("/outcomes", { limit: 100 }) });

  const reportMutation = useMutation({
    mutationFn: () =>
      api.post<OutcomeResponse>("/outcomes", {
        asset_id: assetId,
        sensor_id: sensorId || undefined,
        outcome_type: outcomeType,
        description,
        outcome_timestamp: outcomeTimestamp ? new Date(outcomeTimestamp).toISOString() : new Date().toISOString(),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["outcomes"] });
      setReportOpen(false);
      setDescription("");
    },
  });

  if (outcomesQuery.isLoading) return <LoadingState label="Loading outcomes..." />;
  if (outcomesQuery.isError) return <ErrorState message={errMsg(outcomesQuery.error)} />;

  const outcomes = outcomesQuery.data?.items ?? [];

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-900">Reported outcomes</h2>
          <button onClick={() => setReportOpen((v) => !v)} className="rounded bg-brand-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-700">
            {reportOpen ? "Cancel" : "Report outcome"}
          </button>
        </div>

        {reportOpen && (
          <div className="space-y-2 rounded border border-slate-200 bg-white p-3">
            <select value={assetId} onChange={(e) => setAssetId(e.target.value)} className="w-full rounded border border-slate-300 px-2 py-1 text-sm">
              <option value="">Select asset...</option>
              {(assetsQuery.data?.items ?? []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} ({a.code})
                </option>
              ))}
            </select>
            <select value={sensorId} onChange={(e) => setSensorId(e.target.value)} className="w-full rounded border border-slate-300 px-2 py-1 text-sm" disabled={!assetId}>
              <option value="">Sensor (optional)...</option>
              {(sensorsQuery.data?.items ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.code})
                </option>
              ))}
            </select>
            <select value={outcomeType} onChange={(e) => setOutcomeType(e.target.value as OutcomeType)} className="w-full rounded border border-slate-300 px-2 py-1 text-sm">
              {(["maintenance_event", "operational_event", "inspection_result", "failure_event", "recovery_event", "other"] as OutcomeType[]).map((t) => (
                <option key={t} value={t}>
                  {t.replace(/_/g, " ")}
                </option>
              ))}
            </select>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Describe what was observed..."
              className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
              rows={2}
            />
            <input
              type="datetime-local"
              value={outcomeTimestamp}
              onChange={(e) => setOutcomeTimestamp(e.target.value)}
              className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
            />
            <button
              onClick={() => reportMutation.mutate()}
              disabled={!assetId || !description || reportMutation.isPending}
              className="w-full rounded bg-brand-600 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
            >
              Submit report
            </button>
            {reportMutation.isError && <p className="text-xs text-red-600">{errMsg(reportMutation.error)}</p>}
          </div>
        )}

        {outcomes.length === 0 ? (
          <EmptyState title="No outcomes reported yet" description="Report an outcome to start the closed loop." />
        ) : (
          <div className="space-y-1">
            {outcomes.map((o) => (
              <button
                key={o.id}
                onClick={() => setSelectedOutcomeId(o.id)}
                className={`block w-full rounded border px-3 py-2 text-left text-sm ${
                  selectedOutcomeId === o.id ? "border-brand-400 bg-brand-50" : "border-slate-200 bg-white hover:bg-slate-50"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium text-slate-800">{o.outcome_type.replace(/_/g, " ")}</span>
                  <VerificationBadge status={o.verification_status} />
                </div>
                <p className="mt-1 truncate text-xs text-slate-500">{o.description}</p>
              </button>
            ))}
          </div>
        )}
      </div>

      <div>{selectedOutcomeId ? <OutcomeDetail outcomeId={selectedOutcomeId} /> : <EmptyState title="Select an outcome" description="Choose a reported outcome to see its verification, links, and assessments." />}</div>
    </div>
  );
}

function OutcomeDetail({ outcomeId }: { outcomeId: string }) {
  const queryClient = useQueryClient();
  const [evidenceNote, setEvidenceNote] = useState("");
  const [toStatus, setToStatus] = useState<VerificationStatus>("verified");
  const [signalType, setSignalType] = useState<OutcomeSignalType>("anomaly");
  const [signalId, setSignalId] = useState("");
  const [horizonHours, setHorizonHours] = useState("24");

  const outcomeQuery = useQuery({ queryKey: ["outcome", outcomeId], queryFn: () => api.get<OutcomeResponse>(`/outcomes/${outcomeId}`) });
  const verificationsQuery = useQuery({
    queryKey: ["outcome-verifications", outcomeId],
    queryFn: () => api.get<Page<OutcomeVerificationResponse>>(`/outcomes/${outcomeId}/verifications`),
  });
  const linksQuery = useQuery({
    queryKey: ["outcome-links", outcomeId],
    queryFn: () => api.get<Page<OutcomeSignalLinkResponse>>(`/outcomes/${outcomeId}/links`),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["outcome", outcomeId] });
    queryClient.invalidateQueries({ queryKey: ["outcome-verifications", outcomeId] });
    queryClient.invalidateQueries({ queryKey: ["outcome-links", outcomeId] });
    queryClient.invalidateQueries({ queryKey: ["outcomes"] });
  };

  const verifyMutation = useMutation({
    mutationFn: () => api.post<OutcomeResponse>(`/outcomes/${outcomeId}/verify`, { to_status: toStatus, evidence_note: evidenceNote || undefined }),
    onSuccess: () => {
      invalidate();
      setEvidenceNote("");
    },
  });

  const linkMutation = useMutation({
    mutationFn: () =>
      api.post<OutcomeSignalLinkResponse>(`/outcomes/${outcomeId}/links`, {
        signal_type: signalType,
        signal_id: signalId,
        horizon_seconds: Number(horizonHours) * 3600,
      }),
    onSuccess: () => {
      invalidate();
      setSignalId("");
    },
  });

  const assessMutation = useMutation({
    mutationFn: ({ linkId, assessment }: { linkId: string; assessment: SignalAssessment }) =>
      api.post<OutcomeSignalLinkResponse>(`/outcomes/links/${linkId}/assess`, { signal_assessment: assessment }),
    onSuccess: () => invalidate(),
  });

  if (outcomeQuery.isLoading) return <LoadingState label="Loading outcome..." />;
  if (outcomeQuery.isError) return <ErrorState message={errMsg(outcomeQuery.error)} />;
  const outcome = outcomeQuery.data;
  if (!outcome) return null;

  return (
    <div className="space-y-3 rounded border border-slate-200 bg-white p-3">
      <div>
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-900">{outcome.outcome_type.replace(/_/g, " ")}</h3>
          <VerificationBadge status={outcome.verification_status} />
        </div>
        <p className="mt-1 text-sm text-slate-700">{outcome.description}</p>
        <p className="mt-1 text-xs text-slate-500">Reported outcome timestamp: {outcome.outcome_timestamp}</p>
      </div>

      <div className="space-y-1 border-t border-slate-100 pt-2">
        <h4 className="text-xs font-semibold uppercase text-slate-500">Verification history</h4>
        {(verificationsQuery.data?.items ?? []).length === 0 ? (
          <p className="text-xs text-slate-400">No verification actions yet — this outcome is still UNVERIFIED.</p>
        ) : (
          (verificationsQuery.data?.items ?? []).map((v) => (
            <div key={v.id} className="rounded bg-slate-50 px-2 py-1 text-xs text-slate-600">
              {v.from_status} → {v.to_status} by {v.reviewer_role ?? "unknown"} — {v.evidence_note ?? "no evidence note"}
            </div>
          ))
        )}
        <div className="flex gap-2 pt-1">
          <select value={toStatus} onChange={(e) => setToStatus(e.target.value as VerificationStatus)} className="rounded border border-slate-300 px-2 py-1 text-xs">
            {(["reviewed", "verified", "rejected", "conflicting", "unverified"] as VerificationStatus[]).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <input
            value={evidenceNote}
            onChange={(e) => setEvidenceNote(e.target.value)}
            placeholder="Evidence note (required for verified/rejected)"
            className="flex-1 rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <button onClick={() => verifyMutation.mutate()} disabled={verifyMutation.isPending} className="rounded bg-slate-800 px-3 py-1 text-xs font-medium text-white">
            Submit
          </button>
        </div>
        {verifyMutation.isError && <p className="text-xs text-red-600">{errMsg(verifyMutation.error)}</p>}
      </div>

      <div className="space-y-1 border-t border-slate-100 pt-2">
        <h4 className="text-xs font-semibold uppercase text-slate-500">Linked signals</h4>
        {(linksQuery.data?.items ?? []).length === 0 ? (
          <p className="text-xs text-slate-400">No signals linked yet.</p>
        ) : (
          (linksQuery.data?.items ?? []).map((link) => (
            <div key={link.id} className="rounded border border-slate-200 px-2 py-1 text-xs">
              <div className="flex items-center justify-between">
                <span>
                  {link.signal_type} {link.signal_id.slice(0, 8)} — within_horizon={String(link.within_horizon)}
                </span>
                <AssessmentBadge assessment={link.signal_assessment} />
              </div>
              {link.signal_type === "scenario_run" && <ScenarioSeparationNote />}
              {outcome.verification_status === "verified" || outcome.verification_status === "rejected" ? (
                <div className="mt-1 flex gap-1">
                  {(["confirms", "refutes", "inconclusive"] as SignalAssessment[]).map((a) => (
                    <button
                      key={a}
                      onClick={() => assessMutation.mutate({ linkId: link.id, assessment: a })}
                      className="rounded border border-slate-300 px-2 py-0.5 text-[11px] hover:bg-slate-50"
                    >
                      {a}
                    </button>
                  ))}
                </div>
              ) : (
                <p className="mt-1 text-[11px] text-slate-400">Assessment requires the outcome to be VERIFIED or REJECTED first.</p>
              )}
            </div>
          ))
        )}
        <div className="flex gap-2 pt-1">
          <select value={signalType} onChange={(e) => setSignalType(e.target.value as OutcomeSignalType)} className="rounded border border-slate-300 px-2 py-1 text-xs">
            {(["anomaly", "degradation_signal", "forecast_run", "scenario_run"] as OutcomeSignalType[]).map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <input value={signalId} onChange={(e) => setSignalId(e.target.value)} placeholder="Signal id" className="flex-1 rounded border border-slate-300 px-2 py-1 text-xs" />
          <input
            value={horizonHours}
            onChange={(e) => setHorizonHours(e.target.value)}
            type="number"
            min={1}
            className="w-20 rounded border border-slate-300 px-2 py-1 text-xs"
            title="Horizon (hours)"
          />
          <button onClick={() => linkMutation.mutate()} disabled={!signalId || linkMutation.isPending} className="rounded bg-slate-800 px-3 py-1 text-xs font-medium text-white">
            Link
          </button>
        </div>
        {linkMutation.isError && <p className="text-xs text-red-600">{errMsg(linkMutation.error)}</p>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Evaluation & error analysis tab
// ---------------------------------------------------------------------------

function EvaluationTab() {
  const [sensorId, setSensorId] = useState("");
  const queryClient = useQueryClient();
  const runsQuery = useQuery({
    queryKey: ["evaluation-runs", sensorId],
    queryFn: () => api.get<Page<OutcomeEvaluationRunResponse>>("/feedback/evaluation-runs", { sensor_id: sensorId || undefined, limit: 50 }),
  });
  const errorAnalysisQuery = useQuery({
    queryKey: ["error-analysis", sensorId],
    queryFn: () => api.get<{ data: ErrorAnalysisReport }>("/feedback/error-analysis", { sensor_id: sensorId || undefined }),
  });
  const labelDefsQuery = useQuery({ queryKey: ["label-definitions"], queryFn: () => api.get<{ data: LabelDefinitions }>("/feedback/label-definitions") });

  const createRunMutation = useMutation({
    mutationFn: (payload: { signal_type: OutcomeSignalType; evaluation_period_start: string; evaluation_period_end: string; sensor_id?: string }) =>
      api.post<OutcomeEvaluationRunResponse>("/feedback/evaluation-runs", payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["evaluation-runs"] }),
  });

  const runs = runsQuery.data?.items ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <input value={sensorId} onChange={(e) => setSensorId(e.target.value)} placeholder="Filter by sensor id (optional)" className="rounded border border-slate-300 px-2 py-1 text-sm" />
        <button
          onClick={() =>
            createRunMutation.mutate({
              signal_type: "anomaly",
              evaluation_period_start: new Date(Date.now() - 1000 * 3600 * 24 * 365).toISOString(),
              evaluation_period_end: new Date().toISOString(),
              sensor_id: sensorId || undefined,
            })
          }
          className="rounded bg-brand-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-700"
        >
          Run anomaly evaluation (last 365d)
        </button>
      </div>
      {createRunMutation.isError && <p className="text-xs text-red-600">{errMsg(createRunMutation.error)}</p>}

      <div>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">Evaluation runs</h3>
        {runsQuery.isLoading ? (
          <LoadingState />
        ) : runs.length === 0 ? (
          <EmptyState title="No evaluation runs yet" description="Create one above once outcomes are linked and verified." />
        ) : (
          <div className="space-y-1">
            {runs.map((r) => (
              <div key={r.id} className="rounded border border-slate-200 bg-white px-3 py-2 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-medium">
                    {r.evaluation_kind} — {r.signal_type}
                  </span>
                  <span className="text-slate-500">{r.metrics.coverage_classification}</span>
                </div>
                <p className="mt-1 text-slate-600">
                  {r.metrics.computed ? `precision=${fmtNum(r.metrics.precision)} (tp=${r.metrics.true_positive_count}, fp=${r.metrics.false_positive_count})` : `Not computed — ${r.metrics.reason}`}
                </p>
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">Error analysis (candidates for review — never automatic conclusions)</h3>
        {errorAnalysisQuery.isLoading ? (
          <LoadingState />
        ) : errorAnalysisQuery.data ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <div className="rounded border border-slate-200 bg-white p-2 text-xs">
              <p className="font-semibold text-slate-700">Late detections</p>
              <p className="text-slate-500">{errorAnalysisQuery.data.data.late_detection_count}</p>
            </div>
            <div className="rounded border border-slate-200 bg-white p-2 text-xs">
              <p className="font-semibold text-slate-700">Duplicate candidates</p>
              <p className="text-slate-500">{errorAnalysisQuery.data.data.duplicate_candidate_count}</p>
            </div>
            <div className="rounded border border-slate-200 bg-white p-2 text-xs">
              <p className="font-semibold text-slate-700">False-negative candidates</p>
              <p className="text-slate-500">{errorAnalysisQuery.data.data.false_negative_candidate_count}</p>
            </div>
          </div>
        ) : null}
      </div>

      {labelDefsQuery.data && (
        <details className="rounded border border-slate-200 bg-white p-3 text-xs">
          <summary className="cursor-pointer font-semibold text-slate-700">Label definitions (v{labelDefsQuery.data.data.version})</summary>
          <div className="mt-2 space-y-2">
            {Object.entries(labelDefsQuery.data.data.verification_status).map(([k, v]) => (
              <p key={k}>
                <span className="font-medium">{k}:</span> {v}
              </p>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Retraining candidates tab
// ---------------------------------------------------------------------------

function RetrainingTab() {
  const queryClient = useQueryClient();
  const candidatesQuery = useQuery({ queryKey: ["retraining-candidates"], queryFn: () => api.get<Page<RetrainingCandidateResponse>>("/feedback/retraining-candidates", { limit: 50 }) });

  const reviewMutation = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approved" | "dismissed" }) =>
      api.post<RetrainingCandidateResponse>(`/feedback/retraining-candidates/${id}/review`, { decision }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["retraining-candidates"] }),
  });

  if (candidatesQuery.isLoading) return <LoadingState />;
  if (candidatesQuery.isError) return <ErrorState message={errMsg(candidatesQuery.error)} />;
  const candidates = candidatesQuery.data?.items ?? [];

  return (
    <div className="space-y-2">
      <p className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
        Approving a candidate only creates a REQUESTED retraining request through the existing MLOps workflow — it
        never executes training or activates a new version automatically. Those remain separate, later, human
        actions on the MLOps &amp; Governance page.
      </p>
      {candidates.length === 0 ? (
        <EmptyState title="No retraining candidates" description="Candidates are proposed from the MLOps & Governance or Evaluation workflows." />
      ) : (
        candidates.map((c) => (
          <div key={c.id} className="rounded border border-slate-200 bg-white p-3 text-sm">
            <div className="flex items-center justify-between">
              <span className="font-medium text-slate-800">Component {c.component_id.slice(0, 8)}</span>
              <span className="rounded border border-slate-300 bg-slate-100 px-2 py-0.5 text-xs">{c.status}</span>
            </div>
            <p className="mt-1 text-xs text-slate-600">{c.trigger_reason}</p>
            {c.status === "proposed" && (
              <div className="mt-2 flex gap-2">
                <button onClick={() => reviewMutation.mutate({ id: c.id, decision: "approved" })} className="rounded bg-emerald-600 px-3 py-1 text-xs font-medium text-white">
                  Approve
                </button>
                <button onClick={() => reviewMutation.mutate({ id: c.id, decision: "dismissed" })} className="rounded border border-slate-300 px-3 py-1 text-xs font-medium text-slate-700">
                  Dismiss
                </button>
              </div>
            )}
            {c.resulting_retraining_request_id && <p className="mt-1 text-xs text-slate-500">Retraining request: {c.resulting_retraining_request_id.slice(0, 8)} (requested, not executed)</p>}
          </div>
        ))
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Feedback datasets tab
// ---------------------------------------------------------------------------

function DatasetsTab() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const buildsQuery = useQuery({ queryKey: ["dataset-builds"], queryFn: () => api.get<Page<FeedbackDatasetBuildSummaryResponse>>("/feedback/datasets", { limit: 50 }) });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const detailQuery = useQuery({
    queryKey: ["dataset-build", selectedId],
    queryFn: () => api.get<FeedbackDatasetBuildResponse>(`/feedback/datasets/${selectedId}`),
    enabled: Boolean(selectedId),
  });

  const buildMutation = useMutation({
    mutationFn: () => api.post<FeedbackDatasetBuildResponse>("/feedback/datasets", { name }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dataset-builds"] });
      setName("");
    },
  });

  const builds = buildsQuery.data?.items ?? [];

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div className="space-y-2">
        <div className="flex gap-2">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Dataset name" className="flex-1 rounded border border-slate-300 px-2 py-1 text-sm" />
          <button onClick={() => buildMutation.mutate()} disabled={!name || buildMutation.isPending} className="rounded bg-brand-600 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">
            Build dataset
          </button>
        </div>
        <p className="text-xs text-slate-500">
          Only includes VERIFIED/REJECTED outcomes with a CONFIRMS/REFUTES link assessment — never unverified feedback.
        </p>
        {buildMutation.isError && <p className="text-xs text-red-600">{errMsg(buildMutation.error)}</p>}
        {builds.length === 0 ? (
          <EmptyState title="No dataset builds yet" />
        ) : (
          builds.map((b) => (
            <button key={b.id} onClick={() => setSelectedId(b.id)} className="block w-full rounded border border-slate-200 bg-white px-3 py-2 text-left text-sm hover:bg-slate-50">
              {b.name} v{b.dataset_version} — {b.row_count} rows
            </button>
          ))
        )}
      </div>
      <div>
        {selectedId && detailQuery.data ? (
          <pre className="max-h-96 overflow-auto rounded border border-slate-200 bg-slate-50 p-3 text-xs">{JSON.stringify(detailQuery.data.rows, null, 2)}</pre>
        ) : (
          <EmptyState title="Select a dataset build" />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Prioritization policy tab
// ---------------------------------------------------------------------------

function PolicyTab() {
  const queryClient = useQueryClient();
  const [rationale, setRationale] = useState("");
  const policiesQuery = useQuery({ queryKey: ["prioritization-policies"], queryFn: () => api.get<Page<PrioritizationPolicyResponse>>("/feedback/prioritization-policies", { limit: 50 }) });

  const createMutation = useMutation({
    mutationFn: () => api.post<PrioritizationPolicyResponse>("/feedback/prioritization-policies", { config: {}, rationale }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["prioritization-policies"] });
      setRationale("");
    },
  });
  const activateMutation = useMutation({
    mutationFn: (id: string) => api.post<PrioritizationPolicyResponse>(`/feedback/prioritization-policies/${id}/activate`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["prioritization-policies"] }),
  });

  const policies = policiesQuery.data?.items ?? [];
  const hasActive = policies.some((p) => p.status === "active");

  return (
    <div className="space-y-2">
      <p className="rounded border border-slate-300 bg-slate-50 px-3 py-2 text-xs text-slate-700">
        With no active policy (the default), the existing signal prioritization formula is completely unchanged.
        Activating a policy here is explicit and versioned — never silent.
      </p>
      {!hasActive && (
        <div className="flex gap-2">
          <input value={rationale} onChange={(e) => setRationale(e.target.value)} placeholder="Rationale for a new policy" className="flex-1 rounded border border-slate-300 px-2 py-1 text-sm" />
          <button onClick={() => createMutation.mutate()} disabled={!rationale || createMutation.isPending} className="rounded bg-brand-600 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">
            Create draft policy
          </button>
        </div>
      )}
      {policies.length === 0 ? (
        <EmptyState title="No prioritization policy exists yet" description="Default prioritization is unchanged." />
      ) : (
        policies.map((p) => (
          <div key={p.id} className="flex items-center justify-between rounded border border-slate-200 bg-white px-3 py-2 text-sm">
            <div>
              <span className="font-medium">v{p.version}</span> — {p.rationale}
            </div>
            <div className="flex items-center gap-2">
              <span className={`rounded border px-2 py-0.5 text-xs ${p.status === "active" ? "border-emerald-300 bg-emerald-50 text-emerald-700" : "border-slate-300 bg-slate-100 text-slate-600"}`}>{p.status}</span>
              {p.status === "draft" && (
                <button onClick={() => activateMutation.mutate(p.id)} className="rounded bg-slate-800 px-2 py-1 text-xs font-medium text-white">
                  Activate
                </button>
              )}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Analytics & governance tab
// ---------------------------------------------------------------------------

function AnalyticsTab() {
  const analyticsQuery = useQuery({ queryKey: ["closed-loop-analytics"], queryFn: () => api.get<{ data: ClosedLoopAnalytics }>("/feedback/analytics") });
  const separationQuery = useQuery({ queryKey: ["scenario-separation"], queryFn: () => api.get<{ data: Record<string, unknown> }>("/feedback/scenario-separation") });

  if (analyticsQuery.isLoading) return <LoadingState />;
  if (analyticsQuery.isError) return <ErrorState message={errMsg(analyticsQuery.error)} />;
  const data = analyticsQuery.data?.data;
  if (!data) return null;

  return (
    <div className="space-y-4">
      <p className="rounded border border-slate-300 bg-slate-50 px-3 py-2 text-xs text-slate-700">{data.note}</p>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="rounded border border-slate-200 bg-white p-3">
          <h4 className="mb-2 text-xs font-semibold uppercase text-slate-500">Outcomes by verification status</h4>
          {Object.entries(data.outcomes_by_verification_status).map(([k, v]) => (
            <div key={k} className="flex justify-between text-sm">
              <span className="text-slate-600">{k}</span>
              <span className="font-medium">{v}</span>
            </div>
          ))}
        </div>
        <div className="rounded border border-slate-200 bg-white p-3">
          <h4 className="mb-2 text-xs font-semibold uppercase text-slate-500">Links by signal assessment</h4>
          {Object.entries(data.links_by_signal_assessment).map(([k, v]) => (
            <div key={k} className="flex justify-between text-sm">
              <span className="text-slate-600">{k.replace(/_/g, " ")}</span>
              <span className="font-medium">{v}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded border border-slate-200 bg-white p-3 text-center">
          <p className="text-2xl font-semibold text-slate-900">{data.evaluation_run_count}</p>
          <p className="text-xs text-slate-500">Evaluation runs</p>
        </div>
        <div className="rounded border border-slate-200 bg-white p-3 text-center">
          <p className="text-2xl font-semibold text-slate-900">{data.feedback_dataset_build_count}</p>
          <p className="text-xs text-slate-500">Dataset builds</p>
        </div>
        <div className="rounded border border-slate-200 bg-white p-3 text-center">
          <p className="text-2xl font-semibold text-slate-900">{data.retraining_candidates_by_status.proposed ?? 0}</p>
          <p className="text-xs text-slate-500">Pending retraining candidates</p>
        </div>
        <div className="rounded border border-slate-200 bg-white p-3 text-center">
          <p className="text-2xl font-semibold text-slate-900">{data.active_prioritization_policy_id ? "Yes" : "No"}</p>
          <p className="text-xs text-slate-500">Active prioritization policy</p>
        </div>
      </div>

      {separationQuery.data && (
        <div className="rounded border border-violet-300 bg-violet-50 p-3 text-xs text-violet-800">
          Scenario/real separation invariant: {String(separationQuery.data.data.invariant_holds)} ({String(separationQuery.data.data.scenario_links_counting_toward_evaluation)} scenario links
          counting toward evaluation — must always be 0)
        </div>
      )}
    </div>
  );
}
