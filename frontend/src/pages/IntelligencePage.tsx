import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { api, ApiError } from "@/api/client";
import type {
  AnalysisEnvelope,
  AnomalyContext,
  AnomalyInvestigationResponse,
  AnomalyMonitoringSummary,
  AnomalyResponse,
  AnomalySeverityTier,
  AnomalyStatus,
  AssetResponse,
  DetectionRunResponse,
  DetectorResponse,
  DetectorType,
  Page,
  SensorResponse,
} from "@/api/types";
import { EmptyState, ErrorState, LoadingState } from "@/components/StateViews";

function isoLocal(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fmt(n: number | null | undefined, digits = 3): string {
  return n === null || n === undefined ? "—" : n.toFixed(digits);
}

const SEVERITY_STYLES: Record<AnomalySeverityTier, string> = {
  observed_unusual_behavior: "bg-slate-100 text-slate-700",
  candidate_anomaly: "bg-amber-100 text-amber-800",
  high_priority_anomaly: "bg-red-100 text-red-800",
};
const SEVERITY_LABELS: Record<AnomalySeverityTier, string> = {
  observed_unusual_behavior: "Observed unusual",
  candidate_anomaly: "Candidate anomaly",
  high_priority_anomaly: "High priority",
};

function SeverityTierBadge({ tier }: { tier: AnomalySeverityTier }) {
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${SEVERITY_STYLES[tier]}`}>{SEVERITY_LABELS[tier]}</span>;
}

const DETECTOR_TYPE_LABELS: Record<DetectorType, string> = {
  robust_zscore: "Robust z-score",
  isolation_forest: "Isolation Forest",
  mahalanobis: "Mahalanobis distance",
};
function detectorTypeLabel(detectorType: string): string {
  return DETECTOR_TYPE_LABELS[detectorType as DetectorType] ?? detectorType;
}
// Any detector that is not single-sensor robust z-score requires 2+ sensors
// selected on a shared time grid (see backend/app/services/anomaly_service.py).
function isMultiSensorDetector(detectorType: DetectorType): boolean {
  return detectorType !== "robust_zscore";
}

const STATUS_STYLES: Record<AnomalyStatus, string> = {
  detected: "bg-blue-100 text-blue-800",
  under_review: "bg-amber-100 text-amber-800",
  acknowledged: "bg-purple-100 text-purple-800",
  resolved: "bg-emerald-100 text-emerald-800",
  false_positive: "bg-slate-200 text-slate-600",
};

function StatusBadge({ status }: { status: AnomalyStatus }) {
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[status]}`}>{status.replace(/_/g, " ")}</span>;
}

function JobStatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    queued: "bg-slate-100 text-slate-600",
    running: "bg-blue-100 text-blue-800",
    succeeded: "bg-emerald-100 text-emerald-800",
    trained: "bg-emerald-100 text-emerald-800",
    failed: "bg-red-100 text-red-800",
  };
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${styles[status] ?? "bg-slate-100 text-slate-600"}`}>{status}</span>;
}

const ALLOWED_NEXT_STATUS: Record<AnomalyStatus, AnomalyStatus[]> = {
  detected: ["under_review", "acknowledged", "false_positive", "resolved"],
  under_review: ["acknowledged", "false_positive", "resolved", "detected"],
  acknowledged: ["resolved", "false_positive", "under_review"],
  resolved: ["under_review"],
  false_positive: ["under_review"],
};

export function IntelligencePage() {
  const queryClient = useQueryClient();
  const now = useMemo(() => new Date(), []);

  // -- Detector training form state -----------------------------------------------------
  const [showTrainForm, setShowTrainForm] = useState(false);
  const [trainAssetId, setTrainAssetId] = useState("");
  const [trainSensorIds, setTrainSensorIds] = useState<string[]>([]);
  const [trainName, setTrainName] = useState("");
  const [trainType, setTrainType] = useState<DetectorType>("robust_zscore");
  const [trainRefStart, setTrainRefStart] = useState(isoLocal(new Date(now.getTime() - 14 * 24 * 3600 * 1000)));
  const [trainRefEnd, setTrainRefEnd] = useState(isoLocal(new Date(now.getTime() - 1 * 24 * 3600 * 1000)));
  const [trainStepMinutes, setTrainStepMinutes] = useState(30);

  // -- Anomaly filters --------------------------------------------------------------------
  const [filterAssetId, setFilterAssetId] = useState("");
  const [filterSensorId, setFilterSensorId] = useState("");
  const [filterStatus, setFilterStatus] = useState<AnomalyStatus | "">("");
  const [filterSeverity, setFilterSeverity] = useState<AnomalySeverityTier | "">("");

  // -- Selected detector (for run/evaluate) and selected anomaly (for detail) -------------
  const [selectedDetectorId, setSelectedDetectorId] = useState<string | null>(null);
  const [runScoringStart, setRunScoringStart] = useState(isoLocal(new Date(now.getTime() - 1 * 24 * 3600 * 1000)));
  const [runScoringEnd, setRunScoringEnd] = useState(isoLocal(now));
  const [allowRetrospective, setAllowRetrospective] = useState(false);
  const [selectedAnomalyId, setSelectedAnomalyId] = useState<string | null>(null);
  const [reviewNotes, setReviewNotes] = useState("");

  const assetsQuery = useQuery({
    queryKey: ["assets", "anomaly"],
    queryFn: () => api.get<Page<AssetResponse>>("/assets", { limit: 200 }),
  });

  const trainSensorsQuery = useQuery({
    queryKey: ["assets", trainAssetId, "sensors", "anomaly-train"],
    queryFn: () => api.get<Page<SensorResponse>>(`/assets/${trainAssetId}/sensors`, { limit: 100 }),
    enabled: !!trainAssetId,
  });

  const filterSensorsQuery = useQuery({
    queryKey: ["assets", filterAssetId, "sensors", "anomaly-filter"],
    queryFn: () => api.get<Page<SensorResponse>>(`/assets/${filterAssetId}/sensors`, { limit: 100 }),
    enabled: !!filterAssetId,
  });

  const monitoringQuery = useQuery({
    queryKey: ["anomaly", "monitoring"],
    queryFn: () => api.get<AnalysisEnvelope<AnomalyMonitoringSummary>>("/anomaly/monitoring"),
    refetchInterval: 15000,
  });
  const monitoring = monitoringQuery.data?.data;

  const detectorsQuery = useQuery({
    queryKey: ["anomaly", "detectors"],
    queryFn: () => api.get<Page<DetectorResponse>>("/anomaly/detectors", { limit: 100 }),
  });
  const detectors = detectorsQuery.data?.items ?? [];
  const selectedDetector = detectors.find((d) => d.id === selectedDetectorId) ?? null;

  const anomaliesQuery = useQuery({
    queryKey: ["anomaly", "anomalies", filterAssetId, filterSensorId, filterStatus, filterSeverity],
    queryFn: () =>
      api.get<Page<AnomalyResponse>>("/anomaly/anomalies", {
        asset_id: filterAssetId || undefined,
        sensor_id: filterSensorId || undefined,
        status: filterStatus || undefined,
        severity_tier: filterSeverity || undefined,
        limit: 50,
      }),
  });
  const anomalies = anomaliesQuery.data?.items ?? [];
  const selectedAnomaly = anomalies.find((a) => a.id === selectedAnomalyId) ?? null;

  const contextQuery = useQuery({
    queryKey: ["anomaly", "context", selectedAnomalyId],
    queryFn: () => api.get<AnalysisEnvelope<AnomalyContext>>(`/anomaly/anomalies/${selectedAnomalyId}/context`),
    enabled: !!selectedAnomalyId,
  });
  const context = contextQuery.data?.data;

  const investigationsQuery = useQuery({
    queryKey: ["anomaly", "investigations", selectedAnomalyId],
    queryFn: () => api.get<AnomalyInvestigationResponse[]>(`/anomaly/anomalies/${selectedAnomalyId}/investigations`),
    enabled: !!selectedAnomalyId,
  });

  const trainMutation = useMutation({
    mutationFn: () =>
      api.post<DetectorResponse>("/anomaly/detectors", {
        name: trainName,
        detector_type: trainType,
        sensor_ids: trainSensorIds,
        reference_start: new Date(trainRefStart).toISOString(),
        reference_end: new Date(trainRefEnd).toISOString(),
        step_seconds: isMultiSensorDetector(trainType) ? trainStepMinutes * 60 : undefined,
      }),
    onSuccess: (detector) => {
      queryClient.invalidateQueries({ queryKey: ["anomaly", "detectors"] });
      setSelectedDetectorId(detector.id);
      setShowTrainForm(false);
    },
  });

  const runMutation = useMutation({
    mutationFn: () =>
      api.post<DetectionRunResponse>(`/anomaly/detectors/${selectedDetectorId}/runs`, {
        scoring_start: new Date(runScoringStart).toISOString(),
        scoring_end: new Date(runScoringEnd).toISOString(),
        allow_retrospective_scoring: allowRetrospective,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["anomaly", "anomalies"] });
      queryClient.invalidateQueries({ queryKey: ["anomaly", "monitoring"] });
    },
  });

  const evaluateMutation = useMutation({
    mutationFn: () => api.post<AnalysisEnvelope<Record<string, unknown>>>(`/anomaly/detectors/${selectedDetectorId}/evaluate`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["anomaly", "detectors"] }),
  });

  const reviewMutation = useMutation({
    mutationFn: (toStatus: AnomalyStatus) =>
      api.post<AnomalyResponse>(`/anomaly/anomalies/${selectedAnomalyId}/review`, { to_status: toStatus, notes: reviewNotes || undefined }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["anomaly", "anomalies"] });
      queryClient.invalidateQueries({ queryKey: ["anomaly", "investigations", selectedAnomalyId] });
      queryClient.invalidateQueries({ queryKey: ["anomaly", "monitoring"] });
      setReviewNotes("");
    },
  });

  const contextChartData = (context?.series ?? []).map((p) => ({ timestamp: new Date(p.timestamp).getTime(), value: p.value }));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Anomaly Intelligence</h1>
        <p className="mt-1 text-sm text-slate-500">
          Detectors trained on real stored sensor data flag periods that are statistically unusual relative to a
          declared reference period. An anomaly is a candidate for human investigation, not a confirmed fault — see
          each detector's method and each anomaly's explanation before drawing conclusions.
        </p>
      </div>

      {/* ---------------------------------------------------------------- Monitoring strip */}
      {monitoring && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <StatCard title="Trained detectors" value={String(monitoring.detector_count_by_status.trained ?? 0)} sub={monitoring.detector_count_by_status.failed ? `${monitoring.detector_count_by_status.failed} failed` : undefined} />
          <StatCard title="Runs succeeded" value={String(monitoring.run_count_by_status.succeeded ?? 0)} sub={monitoring.run_count_by_status.failed ? `${monitoring.run_count_by_status.failed} failed` : undefined} />
          <StatCard title="High-priority anomalies" value={String(monitoring.anomaly_count_by_severity_tier.high_priority_anomaly ?? 0)} sub={`${monitoring.anomaly_count_by_severity_tier.candidate_anomaly ?? 0} candidate`} />
          <StatCard title="Awaiting review" value={String((monitoring.anomaly_count_by_status.detected ?? 0) + (monitoring.anomaly_count_by_status.under_review ?? 0))} sub="detected + under review" />
        </div>
      )}

      {/* ---------------------------------------------------------------- Detectors panel */}
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-slate-900">Detectors</h3>
          <button
            onClick={() => setShowTrainForm((v) => !v)}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            {showTrainForm ? "Cancel" : "Train new detector"}
          </button>
        </div>

        {showTrainForm && (
          <div className="mt-4 grid grid-cols-1 gap-3 rounded-lg border border-slate-200 bg-slate-50 p-4 sm:grid-cols-3">
            <div>
              <label className="block text-xs font-medium text-slate-500">Name</label>
              <input value={trainName} onChange={(e) => setTrainName(e.target.value)} placeholder="e.g. pump-01-temp-baseline" className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-500">Method</label>
              <select value={trainType} onChange={(e) => setTrainType(e.target.value as DetectorType)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                <option value="robust_zscore">Robust z-score (single sensor)</option>
                <option value="isolation_forest">Isolation Forest (multivariate)</option>
                <option value="mahalanobis">Mahalanobis distance (sensor relationships, 2-6 sensors)</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-500">Asset</label>
              <select
                value={trainAssetId}
                onChange={(e) => {
                  setTrainAssetId(e.target.value);
                  setTrainSensorIds([]);
                }}
                className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              >
                <option value="">Select an asset…</option>
                {(assetsQuery.data?.items ?? []).map((a) => (
                  <option key={a.id} value={a.id}>{a.name}</option>
                ))}
              </select>
            </div>
            <div className="sm:col-span-2">
              <label className="block text-xs font-medium text-slate-500">
                Sensor{trainType === "mahalanobis" ? "s (select 2 to 6)" : trainType === "isolation_forest" ? "s (select one or more)" : " (select exactly one)"}
              </label>
              <div className="mt-1 flex flex-wrap gap-2">
                {(trainSensorsQuery.data?.items ?? []).map((s) => {
                  const checked = trainSensorIds.includes(s.id);
                  return (
                    <label key={s.id} className={`cursor-pointer rounded-md border px-2 py-1 text-xs ${checked ? "border-brand-500 bg-brand-50 text-brand-700" : "border-slate-300 text-slate-600"}`}>
                      <input
                        type="checkbox"
                        className="mr-1"
                        checked={checked}
                        onChange={() => {
                          if (trainType === "robust_zscore") {
                            setTrainSensorIds(checked ? [] : [s.id]);
                          } else {
                            setTrainSensorIds(checked ? trainSensorIds.filter((id) => id !== s.id) : [...trainSensorIds, s.id]);
                          }
                        }}
                      />
                      {s.name}
                    </label>
                  );
                })}
                {trainAssetId && (trainSensorsQuery.data?.items ?? []).length === 0 && <span className="text-xs text-slate-400">No sensors on this asset.</span>}
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-500">Reference start</label>
              <input type="datetime-local" value={trainRefStart} onChange={(e) => setTrainRefStart(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-500">Reference end</label>
              <input type="datetime-local" value={trainRefEnd} onChange={(e) => setTrainRefEnd(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </div>
            {isMultiSensorDetector(trainType) && (
              <div>
                <label className="block text-xs font-medium text-slate-500">Grid step (minutes)</label>
                <input type="number" min={5} value={trainStepMinutes} onChange={(e) => setTrainStepMinutes(Number(e.target.value))} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              </div>
            )}
            <div className="flex items-end sm:col-span-3">
              <button
                onClick={() => trainMutation.mutate()}
                disabled={trainMutation.isPending || !trainName || trainSensorIds.length === 0}
                className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50"
              >
                {trainMutation.isPending ? "Training…" : "Train detector"}
              </button>
            </div>
            {trainMutation.isError && (
              <p className="text-sm text-red-700 sm:col-span-3">{trainMutation.error instanceof ApiError ? trainMutation.error.message : "Failed to train detector."}</p>
            )}
          </div>
        )}

        {detectorsQuery.isLoading && <LoadingState label="Loading detectors..." />}
        {!detectorsQuery.isLoading && detectors.length === 0 && (
          <EmptyState title="No detectors trained yet" description="Train a detector above against a reference period of real stored data." />
        )}

        {detectors.length > 0 && (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="pb-2">Name / version</th>
                  <th className="pb-2">Method</th>
                  <th className="pb-2">Status</th>
                  <th className="pb-2">Reference period</th>
                  <th className="pb-2">Observations</th>
                  <th className="pb-2">Evaluation</th>
                  <th className="pb-2" />
                </tr>
              </thead>
              <tbody>
                {detectors.map((d) => (
                  <tr key={d.id} className={`border-t border-slate-100 ${selectedDetectorId === d.id ? "bg-brand-50/50" : ""}`}>
                    <td className="py-2 font-medium text-slate-900">{d.name} v{d.version}</td>
                    <td className="py-2 text-slate-600">{detectorTypeLabel(d.detector_type)}</td>
                    <td className="py-2"><JobStatusBadge status={d.status} /></td>
                    <td className="py-2 text-xs text-slate-500">{new Date(d.reference_period_start).toLocaleDateString()} – {new Date(d.reference_period_end).toLocaleDateString()}</td>
                    <td className="py-2 text-slate-600">{d.reference_observation_count}</td>
                    <td className="py-2 text-xs text-slate-500">
                      {d.evaluation_results ? `precision ${fmt(d.evaluation_results.mean_precision, 2)}, recall ${fmt(d.evaluation_results.mean_recall, 2)} (synthetic)` : "not evaluated"}
                    </td>
                    <td className="py-2 text-right">
                      <button
                        onClick={() => setSelectedDetectorId(selectedDetectorId === d.id ? null : d.id)}
                        className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                        disabled={d.status !== "trained"}
                      >
                        {selectedDetectorId === d.id ? "Close" : "Select"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {selectedDetector && (
          <div className="mt-4 rounded-lg border border-brand-200 bg-brand-50/40 p-4">
            <p className="text-sm font-medium text-slate-900">Run detection — {selectedDetector.name} v{selectedDetector.version}</p>
            <p className="mt-1 text-xs text-slate-500">
              Scoring must start at or after this detector's reference period ends ({new Date(selectedDetector.reference_period_end).toLocaleString()})
              unless you explicitly allow a retrospective run.
            </p>
            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-4">
              <div>
                <label className="block text-xs font-medium text-slate-500">Scoring start</label>
                <input type="datetime-local" value={runScoringStart} onChange={(e) => setRunScoringStart(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-500">Scoring end</label>
                <input type="datetime-local" value={runScoringEnd} onChange={(e) => setRunScoringEnd(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              </div>
              <div className="flex items-end">
                <label className="flex items-center gap-1.5 text-xs text-slate-600">
                  <input type="checkbox" checked={allowRetrospective} onChange={(e) => setAllowRetrospective(e.target.checked)} />
                  Allow retrospective scoring
                </label>
              </div>
              <div className="flex items-end gap-2">
                <button onClick={() => runMutation.mutate()} disabled={runMutation.isPending} className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50">
                  {runMutation.isPending ? "Running…" : "Run detection"}
                </button>
                <button onClick={() => evaluateMutation.mutate()} disabled={evaluateMutation.isPending} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
                  {evaluateMutation.isPending ? "Evaluating…" : "Evaluate (synthetic)"}
                </button>
              </div>
            </div>
            {runMutation.isError && <p className="mt-2 text-sm text-red-700">{runMutation.error instanceof ApiError ? runMutation.error.message : "Detection run failed."}</p>}
            {runMutation.data && (
              <p className="mt-2 text-sm text-slate-700">
                Run <JobStatusBadge status={runMutation.data.status} />
                {runMutation.data.background_job_id
                  ? " — large window, running in the background; refresh to see progress."
                  : runMutation.data.metrics
                    ? ` — scored ${runMutation.data.metrics.points_scored}, flagged ${runMutation.data.metrics.points_flagged}, created ${runMutation.data.metrics.anomalies_created} anomal${runMutation.data.metrics.anomalies_created === 1 ? "y" : "ies"}${runMutation.data.metrics.retrospective ? " (retrospective run)" : ""}.`
                    : ""}
              </p>
            )}
            {evaluateMutation.isError && <p className="mt-2 text-sm text-red-700">{evaluateMutation.error instanceof ApiError ? evaluateMutation.error.message : "Evaluation failed."}</p>}
            {evaluateMutation.data && (
              <div className="mt-3 rounded-md border border-slate-200 bg-white p-3 text-xs text-slate-600">
                <p className="font-medium text-slate-800">Synthetic scenario evaluation (pipeline validation, not a real-world accuracy claim)</p>
                <ul className="mt-1 space-y-0.5">
                  {(evaluateMutation.data.data.scenarios as Array<Record<string, unknown>>).map((s, i) => {
                    const metrics = s.metrics as Record<string, number | null> | null;
                    return (
                      <li key={i}>
                        <span className="font-medium">{String(s.scenario)}</span>:{" "}
                        {metrics ? `precision ${fmt(metrics.precision, 2)}, recall ${fmt(metrics.recall, 2)}` : `skipped — ${String(s.skipped_reason)}`}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>

      {/* ---------------------------------------------------------------- Anomalies panel */}
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h3 className="font-semibold text-slate-900">Recent unusual behavior</h3>
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-4">
          <div>
            <label className="block text-xs font-medium text-slate-500">Asset</label>
            <select
              value={filterAssetId}
              onChange={(e) => {
                setFilterAssetId(e.target.value);
                setFilterSensorId("");
              }}
              className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            >
              <option value="">All assets</option>
              {(assetsQuery.data?.items ?? []).map((a) => (
                <option key={a.id} value={a.id}>{a.name}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-500">Sensor</label>
            <select value={filterSensorId} onChange={(e) => setFilterSensorId(e.target.value)} disabled={!filterAssetId} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm disabled:bg-slate-50">
              <option value="">All sensors</option>
              {(filterSensorsQuery.data?.items ?? []).map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-500">Status</label>
            <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value as AnomalyStatus | "")} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">All statuses</option>
              {(["detected", "under_review", "acknowledged", "resolved", "false_positive"] as AnomalyStatus[]).map((s) => (
                <option key={s} value={s}>{s.replace(/_/g, " ")}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-500">Severity</label>
            <select value={filterSeverity} onChange={(e) => setFilterSeverity(e.target.value as AnomalySeverityTier | "")} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">All tiers</option>
              <option value="observed_unusual_behavior">Observed unusual</option>
              <option value="candidate_anomaly">Candidate anomaly</option>
              <option value="high_priority_anomaly">High priority</option>
            </select>
          </div>
        </div>

        {anomaliesQuery.isLoading && <LoadingState label="Loading anomalies..." />}
        {anomaliesQuery.isError && <ErrorState message={anomaliesQuery.error instanceof ApiError ? anomaliesQuery.error.message : "Failed to load anomalies."} />}
        {!anomaliesQuery.isLoading && anomalies.length === 0 && (
          <EmptyState title="No anomalies match these filters" description="Train a detector and run detection above, or broaden the filters." />
        )}

        {anomalies.length > 0 && (
          <ul className="mt-4 space-y-2">
            {anomalies.map((a) => (
              <li
                key={a.id}
                onClick={() => setSelectedAnomalyId(selectedAnomalyId === a.id ? null : a.id)}
                className={`cursor-pointer rounded-lg border px-3 py-2 text-sm hover:border-brand-300 ${selectedAnomalyId === a.id ? "border-brand-400 bg-brand-50/40" : "border-slate-200"}`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <SeverityTierBadge tier={a.severity_tier} />
                    <StatusBadge status={a.status} />
                    {a.is_synthetic_scenario && <span className="rounded-full bg-fuchsia-100 px-2 py-0.5 text-xs font-medium text-fuchsia-800">synthetic</span>}
                  </div>
                  <span className="text-xs text-slate-500">{new Date(a.period_start).toLocaleString()}</span>
                </div>
                <p className="mt-1 text-slate-800">{a.summary}</p>
                <p className="mt-0.5 text-xs text-slate-500">
                  {detectorTypeLabel(a.detector_type)} · raw score {fmt(a.raw_score)}
                  {a.normalized_score !== null ? ` · percentile ${fmt(a.normalized_score, 1)}` : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* ---------------------------------------------------------------- Anomaly detail */}
      {selectedAnomaly && (
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-slate-900">Investigation</h3>
            <div className="flex items-center gap-2">
              <SeverityTierBadge tier={selectedAnomaly.severity_tier} />
              <StatusBadge status={selectedAnomaly.status} />
            </div>
          </div>
          <p className="mt-2 text-sm text-slate-700">{selectedAnomaly.summary}</p>

          <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* Explanation */}
            <div className="rounded-lg border border-slate-200 p-4">
              <p className="text-sm font-medium text-slate-900">Detection method &amp; explanation</p>
              <p className="mt-1 text-xs text-slate-500">
                {detectorTypeLabel(selectedAnomaly.detector_type)} · threshold {fmt(selectedAnomaly.threshold_used)}
              </p>
              {selectedAnomaly.contributing_signals?.description ? (
                <p className="mt-2 text-sm text-slate-700">{String(selectedAnomaly.contributing_signals.description)}</p>
              ) : null}
              {Array.isArray(selectedAnomaly.contributing_signals?.contributing_features) && (
                <ul className="mt-2 space-y-1 text-xs text-slate-600">
                  {(selectedAnomaly.contributing_signals!.contributing_features as Array<Record<string, number | string>>).map((f, i) => (
                    <li key={i}>
                      <span className="font-medium">{String(f.feature_name)}</span>: observed {fmt(f.observed_value as number)} vs reference mean {fmt(f.reference_mean as number)} (z = {fmt(f.z_score as number, 2)})
                    </li>
                  ))}
                </ul>
              )}
              <p className="mt-3 text-xs text-slate-400">
                Scores from different detector types are not on a comparable scale — severity tiers are derived independently per detector.
              </p>
            </div>

            {/* Data quality */}
            <div className="rounded-lg border border-slate-200 p-4">
              <p className="text-sm font-medium text-slate-900">Data quality context</p>
              {selectedAnomaly.data_quality_context ? (
                <div className="mt-2 text-sm text-slate-700">
                  <p>{selectedAnomaly.data_quality_context.observation_count} observations in this window, {selectedAnomaly.data_quality_context.gap_count} gap(s).</p>
                  {selectedAnomaly.data_quality_context.missing_fraction !== null && (
                    <p className="mt-1">Missing fraction: {(selectedAnomaly.data_quality_context.missing_fraction * 100).toFixed(0)}%.</p>
                  )}
                  {selectedAnomaly.data_quality_context.warnings.length > 0 && (
                    <ul className="mt-2 list-inside list-disc space-y-0.5 text-xs text-amber-700">
                      {selectedAnomaly.data_quality_context.warnings.map((w, i) => (
                        <li key={i}>{w}</li>
                      ))}
                    </ul>
                  )}
                </div>
              ) : (
                <p className="mt-2 text-sm text-slate-500">No data quality summary available for this window.</p>
              )}
            </div>
          </div>

          {/* Temporal context */}
          <div className="mt-4 rounded-lg border border-slate-200 p-4">
            <p className="text-sm font-medium text-slate-900">Temporal context — what changed</p>
            {contextQuery.isLoading && <LoadingState label="Loading context..." />}
            {context?.note && <p className="mt-2 text-sm text-slate-500">{context.note}</p>}
            {context && !context.note && (
              <>
                <div className="mt-2 grid grid-cols-3 gap-3 text-center text-xs">
                  <ContextStat label="Before" stat={context.before} />
                  <ContextStat label="During" stat={context.during} highlight />
                  <ContextStat label="After" stat={context.after} />
                </div>
                {contextChartData.length > 0 && (
                  <ResponsiveContainer width="100%" height={200} className="mt-3">
                    <LineChart data={contextChartData}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                      <XAxis dataKey="timestamp" type="number" domain={["dataMin", "dataMax"]} tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} stroke="#94a3b8" fontSize={11} />
                      <YAxis stroke="#94a3b8" fontSize={11} domain={["auto", "auto"]} />
                      <Tooltip labelFormatter={(t) => new Date(t as number).toLocaleString()} />
                      <ReferenceArea x1={new Date(selectedAnomaly.period_start).getTime()} x2={new Date(selectedAnomaly.period_end).getTime()} fill="#ef4444" fillOpacity={0.12} />
                      <Line type="monotone" dataKey="value" stroke="#2563eb" dot={false} isAnimationActive={false} />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </>
            )}
          </div>

          {/* Review */}
          <div className="mt-4 rounded-lg border border-slate-200 p-4">
            <p className="text-sm font-medium text-slate-900">Human review</p>
            <textarea
              value={reviewNotes}
              onChange={(e) => setReviewNotes(e.target.value)}
              placeholder="Optional notes..."
              rows={2}
              className="mt-2 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            />
            <div className="mt-2 flex flex-wrap gap-2">
              {ALLOWED_NEXT_STATUS[selectedAnomaly.status].map((next) => (
                <button
                  key={next}
                  onClick={() => reviewMutation.mutate(next)}
                  disabled={reviewMutation.isPending}
                  className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                >
                  Mark {next.replace(/_/g, " ")}
                </button>
              ))}
            </div>
            {reviewMutation.isError && <p className="mt-2 text-sm text-red-700">{reviewMutation.error instanceof ApiError ? reviewMutation.error.message : "Review failed."}</p>}

            {investigationsQuery.data && investigationsQuery.data.length > 0 && (
              <div className="mt-4">
                <p className="text-xs font-medium text-slate-500">Audit history</p>
                <ul className="mt-1 space-y-1 text-xs text-slate-600">
                  {investigationsQuery.data.map((inv) => (
                    <li key={inv.id}>
                      {new Date(inv.created_at).toLocaleString()}: {inv.from_status.replace(/_/g, " ")} → {inv.to_status.replace(/_/g, " ")}
                      {inv.notes ? ` — "${inv.notes}"` : ""}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function StatCard({ title, value, sub }: { title: string; value: string; sub?: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <p className="text-xs font-medium text-slate-500">{title}</p>
      <p className="mt-1 text-lg font-semibold text-slate-900">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-slate-400">{sub}</p>}
    </div>
  );
}

function ContextStat({ label, stat, highlight }: { label: string; stat: { observation_count: number; mean: number; min: number; max: number } | null; highlight?: boolean }) {
  return (
    <div className={`rounded-md border px-2 py-2 ${highlight ? "border-red-200 bg-red-50" : "border-slate-200"}`}>
      <p className="font-medium text-slate-700">{label}</p>
      {stat ? (
        <>
          <p className="mt-1 text-slate-900">mean {fmt(stat.mean, 2)}</p>
          <p className="text-slate-500">{stat.observation_count} obs · [{fmt(stat.min, 2)}, {fmt(stat.max, 2)}]</p>
        </>
      ) : (
        <p className="mt-1 text-slate-400">no data</p>
      )}
    </div>
  );
}
