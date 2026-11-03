import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { api, ApiError } from "@/api/client";
import type {
  AssetResponse,
  AttentionListResult,
  ConditionMonitoringSummary,
  ConditionRunResponse,
  ConditionSignalStatus,
  DegradationReviewResponse,
  DegradationSignalResponse,
  DegradationStage,
  ForecastModelResponse,
  ForecastRunResponse,
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

const STAGE_STYLES: Record<DegradationStage, string> = {
  observed_trend: "bg-slate-100 text-slate-700",
  degradation_candidate: "bg-amber-100 text-amber-800",
  validated_degradation: "bg-red-100 text-red-800",
};
function StageBadge({ stage }: { stage: DegradationStage }) {
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STAGE_STYLES[stage]}`}>{stage.replace(/_/g, " ")}</span>;
}

const STATUS_STYLES: Record<ConditionSignalStatus, string> = {
  detected: "bg-blue-100 text-blue-800",
  under_review: "bg-amber-100 text-amber-800",
  acknowledged: "bg-purple-100 text-purple-800",
  resolved: "bg-emerald-100 text-emerald-800",
  false_positive: "bg-slate-200 text-slate-600",
};
function StatusBadge({ status }: { status: ConditionSignalStatus }) {
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[status]}`}>{status.replace(/_/g, " ")}</span>;
}

function ForecastStatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    justified: "bg-emerald-100 text-emerald-800",
    not_justified: "bg-slate-200 text-slate-600",
    failed: "bg-red-100 text-red-800",
  };
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${styles[status] ?? "bg-slate-100"}`}>{status.replace(/_/g, " ")}</span>;
}

const ALLOWED_NEXT_STATUS: Record<ConditionSignalStatus, ConditionSignalStatus[]> = {
  detected: ["under_review", "acknowledged", "false_positive", "resolved"],
  under_review: ["acknowledged", "false_positive", "resolved", "detected"],
  acknowledged: ["resolved", "false_positive", "under_review"],
  resolved: ["under_review"],
  false_positive: ["under_review"],
};

export function ConditionPage() {
  const queryClient = useQueryClient();
  const now = useMemo(() => new Date(), []);
  const daysAgo = (d: number) => isoLocal(new Date(now.getTime() - d * 24 * 3600 * 1000));

  // -- Shared asset/sensor pickers -----------------------------------------------------
  const [runAssetId, setRunAssetId] = useState("");
  const [runSensorId, setRunSensorId] = useState("");
  const [analysisStart, setAnalysisStart] = useState(daysAgo(18));
  const [analysisEnd, setAnalysisEnd] = useState(daysAgo(0));
  const [referenceStart, setReferenceStart] = useState(daysAgo(21));
  const [referenceEnd, setReferenceEnd] = useState(daysAgo(18));

  // -- Signal filters / selection ------------------------------------------------------
  const [filterStage, setFilterStage] = useState<DegradationStage | "">("");
  const [filterStatus, setFilterStatus] = useState<ConditionSignalStatus | "">("");
  const [selectedSignalId, setSelectedSignalId] = useState<string | null>(null);
  const [reviewNotes, setReviewNotes] = useState("");
  const [validateNotes, setValidateNotes] = useState("");

  // -- Forecasting ----------------------------------------------------------------------
  const [showForecastForm, setShowForecastForm] = useState(false);
  const [forecastName, setForecastName] = useState("");
  const [fitStart, setFitStart] = useState(daysAgo(21));
  const [fitEnd, setFitEnd] = useState(daysAgo(7));
  const [testEnd, setTestEnd] = useState(daysAgo(0));
  const [selectedForecastModelId, setSelectedForecastModelId] = useState<string | null>(null);
  const [horizonHours, setHorizonHours] = useState(24);
  const [stepMinutes, setStepMinutes] = useState(60);

  const assetsQuery = useQuery({ queryKey: ["assets", "condition"], queryFn: () => api.get<Page<AssetResponse>>("/assets", { limit: 200 }) });
  const sensorsQuery = useQuery({
    queryKey: ["assets", runAssetId, "sensors", "condition"],
    queryFn: () => api.get<Page<SensorResponse>>(`/assets/${runAssetId}/sensors`, { limit: 100 }),
    enabled: !!runAssetId,
  });

  const monitoringQuery = useQuery({
    queryKey: ["condition", "monitoring"],
    queryFn: () => api.get<{ data: ConditionMonitoringSummary }>("/condition/monitoring"),
    refetchInterval: 20000,
  });
  const monitoring = monitoringQuery.data?.data;

  const attentionQuery = useQuery({
    queryKey: ["condition", "attention-list"],
    queryFn: () => api.get<{ data: AttentionListResult }>("/condition/attention-list", { limit: 20 }),
    refetchInterval: 20000,
  });
  const attention = attentionQuery.data?.data;

  const signalsQuery = useQuery({
    queryKey: ["condition", "signals", filterStage, filterStatus],
    queryFn: () => api.get<Page<DegradationSignalResponse>>("/condition/signals", { stage: filterStage || undefined, status: filterStatus || undefined, limit: 50 }),
  });
  const signals = signalsQuery.data?.items ?? [];
  const selectedSignal = signals.find((s) => s.id === selectedSignalId) ?? null;

  const reviewsQuery = useQuery({
    queryKey: ["condition", "reviews", selectedSignalId],
    queryFn: () => api.get<DegradationReviewResponse[]>(`/condition/signals/${selectedSignalId}/reviews`),
    enabled: !!selectedSignalId,
  });

  const forecastModelsQuery = useQuery({
    queryKey: ["condition", "forecast-models"],
    queryFn: () => api.get<Page<ForecastModelResponse>>("/condition/forecast-models", { limit: 50 }),
  });
  const forecastModels = forecastModelsQuery.data?.items ?? [];
  const selectedForecastModel = forecastModels.find((m) => m.id === selectedForecastModelId) ?? null;

  const runConditionMutation = useMutation({
    mutationFn: () =>
      api.post<ConditionRunResponse>("/condition/runs", {
        sensor_id: runSensorId,
        analysis_start: new Date(analysisStart).toISOString(),
        analysis_end: new Date(analysisEnd).toISOString(),
        reference_start: new Date(referenceStart).toISOString(),
        reference_end: new Date(referenceEnd).toISOString(),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["condition", "signals"] });
      queryClient.invalidateQueries({ queryKey: ["condition", "monitoring"] });
      queryClient.invalidateQueries({ queryKey: ["condition", "attention-list"] });
    },
  });

  const reviewMutation = useMutation({
    mutationFn: (toStatus: ConditionSignalStatus) =>
      api.post<DegradationSignalResponse>(`/condition/signals/${selectedSignalId}/review`, { to_status: toStatus, notes: reviewNotes || undefined }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["condition", "signals"] });
      queryClient.invalidateQueries({ queryKey: ["condition", "reviews", selectedSignalId] });
      queryClient.invalidateQueries({ queryKey: ["condition", "monitoring"] });
      queryClient.invalidateQueries({ queryKey: ["condition", "attention-list"] });
      setReviewNotes("");
    },
  });

  const validateMutation = useMutation({
    mutationFn: () => api.post<DegradationSignalResponse>(`/condition/signals/${selectedSignalId}/validate`, { notes: validateNotes }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["condition", "signals"] });
      queryClient.invalidateQueries({ queryKey: ["condition", "reviews", selectedSignalId] });
      queryClient.invalidateQueries({ queryKey: ["condition", "attention-list"] });
      setValidateNotes("");
    },
  });

  const fitForecastMutation = useMutation({
    mutationFn: () =>
      api.post<ForecastModelResponse>("/condition/forecast-models", {
        sensor_id: runSensorId,
        name: forecastName,
        fit_start: new Date(fitStart).toISOString(),
        fit_end: new Date(fitEnd).toISOString(),
        test_end: new Date(testEnd).toISOString(),
      }),
    onSuccess: (model) => {
      queryClient.invalidateQueries({ queryKey: ["condition", "forecast-models"] });
      setSelectedForecastModelId(model.id);
      setShowForecastForm(false);
    },
  });

  const forecastRunMutation = useMutation({
    mutationFn: () =>
      api.post<ForecastRunResponse>(`/condition/forecast-models/${selectedForecastModelId}/runs`, {
        as_of: selectedForecastModel ? selectedForecastModel.fit_period_end : new Date().toISOString(),
        horizon_seconds: horizonHours * 3600,
        step_seconds: stepMinutes * 60,
      }),
  });

  const forecastChartData = (forecastRunMutation.data?.points ?? []).map((p) => ({
    timestamp: new Date(p.target_timestamp).getTime(),
    predicted: p.predicted_value,
    band: [p.lower_bound, p.upper_bound] as [number, number],
  }));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Condition Intelligence</h1>
        <p className="mt-1 text-sm text-slate-500">
          Trend, persistent-anomaly, deviation, variance, and forecast evidence about equipment condition — computed
          from real stored data and detector output. There is no single blended &ldquo;health score&rdquo; anywhere
          on this page: every number here traces to a stated methodology, and a degradation signal only reaches
          &ldquo;validated&rdquo; through an explicit human decision, never automatically.
        </p>
      </div>

      {/* ---------------------------------------------------------------- Monitoring strip */}
      {monitoring && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <StatCard title="Degradation candidates" value={String(monitoring.degradation_signal_count_by_stage.degradation_candidate ?? 0)} sub={`${monitoring.degradation_signal_count_by_stage.validated_degradation ?? 0} validated`} />
          <StatCard title="Observed trends" value={String(monitoring.degradation_signal_count_by_stage.observed_trend ?? 0)} />
          <StatCard title="Forecast models justified" value={String(monitoring.forecast_model_count_by_status.justified ?? 0)} sub={`${monitoring.forecast_model_count_by_status.not_justified ?? 0} not justified`} />
          <StatCard title="Open items awaiting review" value={String((monitoring.degradation_signal_count_by_status.detected ?? 0) + (monitoring.degradation_signal_count_by_status.under_review ?? 0))} />
        </div>
      )}

      {/* ---------------------------------------------------------------- Attention list */}
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h3 className="font-semibold text-slate-900">Attention list</h3>
        {attention && <p className="mt-1 text-xs text-slate-500">{attention.sort_methodology}</p>}
        {attentionQuery.isLoading && <LoadingState label="Loading attention list..." />}
        {attention && attention.items.length === 0 && <EmptyState title="Nothing open" description="No open degradation signals or anomalies right now." />}
        {attention && attention.items.length > 0 && (
          <ul className="mt-3 space-y-1.5">
            {attention.items.map((item) => (
              <li key={`${item.item_type}-${item.id}`} className="flex items-center justify-between rounded-md border border-slate-100 px-3 py-1.5 text-sm">
                <div className="flex items-center gap-2">
                  <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${item.item_type === "degradation_signal" ? "bg-amber-100 text-amber-800" : "bg-blue-100 text-blue-800"}`}>
                    {item.item_type === "degradation_signal" ? "condition" : "anomaly"}
                  </span>
                  <span className="text-slate-800">{item.summary}</span>
                </div>
                <span className="text-xs text-slate-400">rank {item.rank.stage_rank}/{item.rank.severity_rank}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* ---------------------------------------------------------------- Analysis scope + run */}
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h3 className="font-semibold text-slate-900">Analyze a sensor</h3>
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div>
            <label htmlFor="condition-asset" className="block text-xs font-medium text-slate-500">Asset</label>
            <select id="condition-asset" value={runAssetId} onChange={(e) => { setRunAssetId(e.target.value); setRunSensorId(""); }} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">Select an asset…</option>
              {(assetsQuery.data?.items ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </div>
          <div>
            <label htmlFor="condition-sensor" className="block text-xs font-medium text-slate-500">Sensor</label>
            <select id="condition-sensor" value={runSensorId} onChange={(e) => setRunSensorId(e.target.value)} disabled={!runAssetId} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm disabled:bg-slate-50">
              <option value="">Select a sensor…</option>
              {(sensorsQuery.data?.items ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </div>
        </div>

        <p className="mt-4 text-xs font-medium text-slate-500">Reference period (independent baseline — must end at or before the analysis window starts)</p>
        <div className="mt-1 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <input type="datetime-local" value={referenceStart} onChange={(e) => setReferenceStart(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          <input type="datetime-local" value={referenceEnd} onChange={(e) => setReferenceEnd(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
        </div>
        <p className="mt-3 text-xs font-medium text-slate-500">Analysis window (degradation classification runs against this period)</p>
        <div className="mt-1 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <input type="datetime-local" value={analysisStart} onChange={(e) => setAnalysisStart(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          <input type="datetime-local" value={analysisEnd} onChange={(e) => setAnalysisEnd(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
        </div>

        <div className="mt-3 flex items-center gap-3">
          <button
            onClick={() => runConditionMutation.mutate()}
            disabled={runConditionMutation.isPending || !runSensorId}
            className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50"
          >
            {runConditionMutation.isPending ? "Analyzing…" : "Run degradation analysis"}
          </button>
          {runConditionMutation.data && (
            <span className="text-sm text-slate-600">
              {runConditionMutation.data.metrics?.signal_produced
                ? `Signal produced: ${runConditionMutation.data.metrics.stage}`
                : `No signal — ${runConditionMutation.data.metrics?.reason ?? "insufficient evidence"}`}
            </span>
          )}
        </div>
        {runConditionMutation.isError && <p className="mt-2 text-sm text-red-700">{runConditionMutation.error instanceof ApiError ? runConditionMutation.error.message : "Analysis failed."}</p>}
      </div>

      {/* ---------------------------------------------------------------- Degradation signals */}
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h3 className="font-semibold text-slate-900">Degradation signals</h3>
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor="condition-stage" className="block text-xs font-medium text-slate-500">Stage</label>
            <select id="condition-stage" value={filterStage} onChange={(e) => setFilterStage(e.target.value as DegradationStage | "")} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">All stages</option>
              <option value="observed_trend">Observed trend</option>
              <option value="degradation_candidate">Degradation candidate</option>
              <option value="validated_degradation">Validated degradation</option>
            </select>
          </div>
          <div>
            <label htmlFor="condition-status" className="block text-xs font-medium text-slate-500">Status</label>
            <select id="condition-status" value={filterStatus} onChange={(e) => setFilterStatus(e.target.value as ConditionSignalStatus | "")} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">All statuses</option>
              {(["detected", "under_review", "acknowledged", "resolved", "false_positive"] as ConditionSignalStatus[]).map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
            </select>
          </div>
        </div>

        {signalsQuery.isLoading && <LoadingState label="Loading signals..." />}
        {signalsQuery.isError && <ErrorState message={signalsQuery.error instanceof ApiError ? signalsQuery.error.message : "Failed to load signals."} />}
        {!signalsQuery.isLoading && signals.length === 0 && <EmptyState title="No degradation signals" description="Run an analysis above against a sensor with a real trend." />}

        {signals.length > 0 && (
          <ul className="mt-4 space-y-2">
            {signals.map((s) => (
              <li
                key={s.id}
                onClick={() => setSelectedSignalId(selectedSignalId === s.id ? null : s.id)}
                className={`cursor-pointer rounded-lg border px-3 py-2 text-sm hover:border-brand-300 ${selectedSignalId === s.id ? "border-brand-400 bg-brand-50/40" : "border-slate-200"}`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <StageBadge stage={s.stage} />
                    <StatusBadge status={s.status} />
                  </div>
                  <span className="text-xs text-slate-500">{new Date(s.period_start).toLocaleDateString()} – {new Date(s.period_end).toLocaleDateString()}</span>
                </div>
                <p className="mt-1 text-slate-800">{s.summary}</p>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* ---------------------------------------------------------------- Signal detail / review */}
      {selectedSignal && (
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-slate-900">Signal evidence</h3>
            <div className="flex items-center gap-2">
              <StageBadge stage={selectedSignal.stage} />
              <StatusBadge status={selectedSignal.status} />
            </div>
          </div>

          <div className="mt-3 rounded-lg border border-slate-200 p-4">
            <p className="text-sm font-medium text-slate-900">Classification rationale</p>
            <p className="mt-1 text-sm text-slate-700">{selectedSignal.evidence.classification.rationale}</p>
            <p className="mt-2 text-xs text-slate-500">
              {selectedSignal.evidence.classification.sub_windows_agreeing}/{selectedSignal.evidence.classification.sub_window_count} sub-windows agree on direction &ldquo;{selectedSignal.evidence.classification.dominant_direction ?? "none"}&rdquo;
              {" · "}corroborated by deviation: {String(selectedSignal.evidence.classification.corroborated_by_deviation)}
              {" · "}corroborated by rising anomaly frequency: {String(selectedSignal.evidence.classification.corroborated_by_anomalies)}
            </p>
            <table className="mt-3 w-full text-left text-xs">
              <thead className="text-slate-500"><tr><th className="pb-1 pr-2">Sub-window</th><th className="pb-1 pr-2">Direction</th><th className="pb-1 pr-2">Slope/hr</th><th className="pb-1 pr-2">R²</th><th className="pb-1">p-value</th></tr></thead>
              <tbody>
                {selectedSignal.evidence.classification.sub_window_trends.map((t, i) => (
                  <tr key={i} className="border-t border-slate-100">
                    <td className="py-1 pr-2">{new Date(t.start).toLocaleDateString()}</td>
                    <td className="py-1 pr-2">{t.direction ?? "—"} {t.significant ? "(significant)" : ""}</td>
                    <td className="py-1 pr-2">{fmt(t.slope_per_hour)}</td>
                    <td className="py-1 pr-2">{fmt(t.r_squared, 2)}</td>
                    <td className="py-1">{fmt(t.p_value, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-4 rounded-lg border border-slate-200 p-4">
            <p className="text-sm font-medium text-slate-900">Human review</p>
            <textarea value={reviewNotes} onChange={(e) => setReviewNotes(e.target.value)} placeholder="Optional notes..." rows={2} className="mt-2 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            <div className="mt-2 flex flex-wrap gap-2">
              {ALLOWED_NEXT_STATUS[selectedSignal.status].map((next) => (
                <button key={next} onClick={() => reviewMutation.mutate(next)} disabled={reviewMutation.isPending} className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
                  Mark {next.replace(/_/g, " ")}
                </button>
              ))}
            </div>
            {reviewMutation.isError && <p className="mt-2 text-sm text-red-700">{reviewMutation.error instanceof ApiError ? reviewMutation.error.message : "Review failed."}</p>}

            {selectedSignal.stage === "degradation_candidate" && (
              <div className="mt-4 rounded-md border border-amber-200 bg-amber-50/50 p-3">
                <p className="text-xs font-medium text-amber-900">Validate as confirmed degradation</p>
                <p className="mt-1 text-xs text-amber-800">
                  This platform has no maintenance-outcome data to confirm a candidate automatically — validating requires your own engineering justification, recorded permanently.
                </p>
                <textarea value={validateNotes} onChange={(e) => setValidateNotes(e.target.value)} placeholder="Required: justification (e.g. confirmed via inspection)..." rows={2} className="mt-2 w-full rounded-md border border-amber-300 px-2 py-1.5 text-sm" />
                <button onClick={() => validateMutation.mutate()} disabled={validateMutation.isPending || !validateNotes.trim()} className="mt-2 rounded-md bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-50">
                  {validateMutation.isPending ? "Validating…" : "Validate degradation"}
                </button>
                {validateMutation.isError && <p className="mt-2 text-xs text-red-700">{validateMutation.error instanceof ApiError ? validateMutation.error.message : "Validation failed."}</p>}
              </div>
            )}

            {reviewsQuery.data && reviewsQuery.data.length > 0 && (
              <div className="mt-4">
                <p className="text-xs font-medium text-slate-500">Audit history</p>
                <ul className="mt-1 space-y-1 text-xs text-slate-600">
                  {reviewsQuery.data.map((r) => (
                    <li key={r.id}>
                      {new Date(r.created_at).toLocaleString()}: {r.from_status.replace(/_/g, " ")} → {r.to_status.replace(/_/g, " ")}
                      {r.from_stage !== r.to_stage ? ` · stage ${r.from_stage.replace(/_/g, " ")} → ${r.to_stage.replace(/_/g, " ")}` : ""}
                      {r.notes ? ` — "${r.notes}"` : ""}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ---------------------------------------------------------------- Forecasting */}
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="font-semibold text-slate-900">Forecasting</h3>
            <p className="mt-1 text-xs text-slate-500">
              Fitting always backtests linear_trend against naive-last-value and seasonal-naive baselines on real
              held-out data. A model is only &ldquo;justified&rdquo; — and only then can generate forecast points —
              if it beats both by at least 10%. &ldquo;Not justified&rdquo; is a valid, honest outcome for most
              sensors, not an error.
            </p>
          </div>
          <button onClick={() => setShowForecastForm((v) => !v)} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
            {showForecastForm ? "Cancel" : "Fit forecast model"}
          </button>
        </div>

        {showForecastForm && (
          <div className="mt-4 grid grid-cols-1 gap-3 rounded-lg border border-slate-200 bg-slate-50 p-4 sm:grid-cols-3">
            <div className="sm:col-span-3">
              <p className="text-xs text-slate-500">Uses the asset/sensor selected in &ldquo;Analyze a sensor&rdquo; above ({runSensorId ? "sensor selected" : "select a sensor above first"}).</p>
            </div>
            <div>
              <label htmlFor="condition-name" className="block text-xs font-medium text-slate-500">Name</label>
              <input id="condition-name" value={forecastName} onChange={(e) => setForecastName(e.target.value)} placeholder="e.g. bearing-temp-forecast" className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </div>
            <div>
              <label htmlFor="condition-fit-start" className="block text-xs font-medium text-slate-500">Fit start</label>
              <input id="condition-fit-start" type="datetime-local" value={fitStart} onChange={(e) => setFitStart(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </div>
            <div>
              <label htmlFor="condition-fit-end-backtest-cutoff" className="block text-xs font-medium text-slate-500">Fit end (backtest cutoff)</label>
              <input id="condition-fit-end-backtest-cutoff" type="datetime-local" value={fitEnd} onChange={(e) => setFitEnd(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </div>
            <div>
              <label htmlFor="condition-test-end-held-out-through" className="block text-xs font-medium text-slate-500">Test end (held-out through)</label>
              <input id="condition-test-end-held-out-through" type="datetime-local" value={testEnd} onChange={(e) => setTestEnd(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </div>
            <div className="flex items-end">
              <button onClick={() => fitForecastMutation.mutate()} disabled={fitForecastMutation.isPending || !runSensorId || !forecastName} className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50">
                {fitForecastMutation.isPending ? "Fitting…" : "Fit & backtest"}
              </button>
            </div>
            {fitForecastMutation.isError && <p className="text-sm text-red-700 sm:col-span-3">{fitForecastMutation.error instanceof ApiError ? fitForecastMutation.error.message : "Fit failed."}</p>}
          </div>
        )}

        {forecastModelsQuery.isLoading && <LoadingState label="Loading forecast models..." />}
        {!forecastModelsQuery.isLoading && forecastModels.length === 0 && <EmptyState title="No forecast models yet" description="Fit one above against a sensor with enough history." />}

        {forecastModels.length > 0 && (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr><th className="pb-2">Name / version</th><th className="pb-2">Status</th><th className="pb-2">Backtest (candidate vs best baseline)</th><th className="pb-2" /></tr>
              </thead>
              <tbody>
                {forecastModels.map((m) => {
                  const ev = m.evaluation_results?.evaluations;
                  return (
                    <tr key={m.id} className={`border-t border-slate-100 ${selectedForecastModelId === m.id ? "bg-brand-50/50" : ""}`}>
                      <td className="py-2 font-medium text-slate-900">{m.name} v{m.version}</td>
                      <td className="py-2"><ForecastStatusBadge status={m.status} /></td>
                      <td className="py-2 text-xs text-slate-500">
                        {ev ? `linear_trend MAE ${fmt(ev.linear_trend.mae, 3)} vs naive ${fmt(ev.naive_last_value.mae, 3)} / seasonal ${fmt(ev.seasonal_naive.mae, 3)}` : m.training_error ?? "—"}
                      </td>
                      <td className="py-2 text-right">
                        <button onClick={() => setSelectedForecastModelId(selectedForecastModelId === m.id ? null : m.id)} disabled={m.status !== "justified"} className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40">
                          {selectedForecastModelId === m.id ? "Close" : m.status === "justified" ? "Select" : "Not runnable"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {selectedForecastModel && (
          <div className="mt-4 rounded-lg border border-brand-200 bg-brand-50/40 p-4">
            <p className="text-sm font-medium text-slate-900">Generate forecast — {selectedForecastModel.name} v{selectedForecastModel.version}</p>
            {selectedForecastModel.evaluation_results?.justification_note && (
              <p className="mt-1 text-xs text-slate-600">{selectedForecastModel.evaluation_results.justification_note}</p>
            )}
            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
              <div>
                <label htmlFor="condition-horizon-hours" className="block text-xs font-medium text-slate-500">Horizon (hours)</label>
                <input id="condition-horizon-hours" type="number" min={1} value={horizonHours} onChange={(e) => setHorizonHours(Number(e.target.value))} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              </div>
              <div>
                <label htmlFor="condition-step-minutes" className="block text-xs font-medium text-slate-500">Step (minutes)</label>
                <input id="condition-step-minutes" type="number" min={1} value={stepMinutes} onChange={(e) => setStepMinutes(Number(e.target.value))} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              </div>
              <div className="flex items-end">
                <button onClick={() => forecastRunMutation.mutate()} disabled={forecastRunMutation.isPending} className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50">
                  {forecastRunMutation.isPending ? "Generating…" : "Generate forecast"}
                </button>
              </div>
            </div>
            {forecastRunMutation.isError && <p className="mt-2 text-sm text-red-700">{forecastRunMutation.error instanceof ApiError ? forecastRunMutation.error.message : "Forecast run failed."}</p>}

            {forecastChartData.length > 0 && (
              <>
                <p className="mt-3 text-xs text-slate-500">Shaded band is a rough uncertainty range from in-sample residual spread — not a calibrated probability.</p>
                <ResponsiveContainer width="100%" height={220} className="mt-2">
                  <ComposedChart data={forecastChartData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                    <XAxis dataKey="timestamp" type="number" domain={["dataMin", "dataMax"]} tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} stroke="#94a3b8" fontSize={11} />
                    <YAxis stroke="#94a3b8" fontSize={11} domain={["auto", "auto"]} />
                    <Tooltip labelFormatter={(t) => new Date(t as number).toLocaleString()} />
                    <Area type="monotone" dataKey="band" stroke="none" fill="#2563eb" fillOpacity={0.12} />
                    <Line type="monotone" dataKey="predicted" stroke="#2563eb" dot={false} isAnimationActive={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              </>
            )}
          </div>
        )}
      </div>
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
