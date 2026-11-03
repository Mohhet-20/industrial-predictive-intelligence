import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api, ApiError } from "@/api/client";
import type {
  AssetResponse,
  ComponentResponse,
  ComponentType,
  ComponentVersionResponse,
  GovernanceEventResponse,
  LifecycleStage,
  MlopsMonitoringSummary,
  Page,
  RetrainingRequestResponse,
  SensorResponse,
  ShiftAnalysisRunResponse,
  VersionKind,
} from "@/api/types";
import { EmptyState, LoadingState } from "@/components/StateViews";

function isoLocal(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const COMPONENT_TYPE_LABELS: Record<ComponentType, string> = {
  trained_ml_model: "Trained ML model",
  statistical_detector: "Statistical detector",
  forecasting_model: "Forecasting model",
  heuristic_signal_method: "Heuristic signal method",
};

const STAGE_STYLES: Record<LifecycleStage, string> = {
  experimenting: "bg-slate-100 text-slate-600",
  trained: "bg-blue-100 text-blue-800",
  evaluated: "bg-indigo-100 text-indigo-800",
  validated: "bg-purple-100 text-purple-800",
  approved: "bg-amber-100 text-amber-800",
  active: "bg-emerald-100 text-emerald-800",
  retired: "bg-slate-200 text-slate-500",
  failed: "bg-red-100 text-red-800",
};
function StageBadge({ stage }: { stage: LifecycleStage }) {
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STAGE_STYLES[stage]}`}>{stage}</span>;
}

export function MlopsPage() {
  const queryClient = useQueryClient();
  const now = useMemo(() => new Date(), []);
  const daysAgo = (d: number) => isoLocal(new Date(now.getTime() - d * 24 * 3600 * 1000));

  // -- Component registration & selection ------------------------------------------------
  const [newComponentType, setNewComponentType] = useState<ComponentType>("trained_ml_model");
  const [newComponentName, setNewComponentName] = useState("");
  const [newComponentDescription, setNewComponentDescription] = useState("");
  const [selectedComponentId, setSelectedComponentId] = useState<string | null>(null);

  // -- Version registration ---------------------------------------------------------------
  const [showRegisterVersion, setShowRegisterVersion] = useState(false);
  const [versionKind, setVersionKind] = useState<VersionKind>("model_version");
  const [underlyingId, setUnderlyingId] = useState("");
  const [heuristicCodeVersion, setHeuristicCodeVersion] = useState("");

  // -- Selected version actions -------------------------------------------------------------
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [validateNotes, setValidateNotes] = useState("");
  const [reviewedReference, setReviewedReference] = useState("");
  const [approveNotes, setApproveNotes] = useState("");
  const [retireReason, setRetireReason] = useState("");

  // -- Retraining ---------------------------------------------------------------------------
  const [retrainReason, setRetrainReason] = useState("");
  const [retrainConfig, setRetrainConfig] = useState("{}");

  // -- Shift analysis -------------------------------------------------------------------------
  const [shiftAssetId, setShiftAssetId] = useState("");
  const [shiftSensorId, setShiftSensorId] = useState("");
  const [refStart, setRefStart] = useState(daysAgo(14));
  const [refEnd, setRefEnd] = useState(daysAgo(7));
  const [cmpStart, setCmpStart] = useState(daysAgo(7));
  const [cmpEnd, setCmpEnd] = useState(daysAgo(0));

  const monitoringQuery = useQuery({ queryKey: ["mlops", "monitoring"], queryFn: () => api.get<{ data: MlopsMonitoringSummary }>("/mlops/monitoring"), refetchInterval: 20000 });
  const monitoring = monitoringQuery.data?.data;

  const componentsQuery = useQuery({ queryKey: ["mlops", "components"], queryFn: () => api.get<Page<ComponentResponse>>("/mlops/components", { limit: 100 }) });
  const components = componentsQuery.data?.items ?? [];
  const selectedComponent = components.find((c) => c.id === selectedComponentId) ?? null;

  const versionsQuery = useQuery({
    queryKey: ["mlops", "versions", selectedComponentId],
    queryFn: () => api.get<Page<ComponentVersionResponse>>(`/mlops/components/${selectedComponentId}/versions`, { limit: 100 }),
    enabled: !!selectedComponentId,
  });
  const versions = versionsQuery.data?.items ?? [];
  const selectedVersion = versions.find((v) => v.id === selectedVersionId) ?? null;

  const governanceQuery = useQuery({
    queryKey: ["mlops", "governance-events", selectedComponentId],
    queryFn: () => api.get<Page<GovernanceEventResponse>>("/mlops/governance-events", { component_id: selectedComponentId!, limit: 100 }),
    enabled: !!selectedComponentId,
  });

  const retrainingQuery = useQuery({
    queryKey: ["mlops", "retraining-requests", selectedComponentId],
    queryFn: () => api.get<Page<RetrainingRequestResponse>>("/mlops/retraining-requests", { component_id: selectedComponentId!, limit: 50 }),
    enabled: !!selectedComponentId,
  });

  const assetsQuery = useQuery({ queryKey: ["assets", "mlops"], queryFn: () => api.get<Page<AssetResponse>>("/assets", { limit: 200 }) });
  const shiftSensorsQuery = useQuery({
    queryKey: ["assets", shiftAssetId, "sensors", "mlops"],
    queryFn: () => api.get<Page<SensorResponse>>(`/assets/${shiftAssetId}/sensors`, { limit: 100 }),
    enabled: !!shiftAssetId,
  });
  const shiftRunsQuery = useQuery({ queryKey: ["mlops", "shift-analysis"], queryFn: () => api.get<Page<ShiftAnalysisRunResponse>>("/mlops/shift-analysis", { limit: 50 }) });

  function invalidateComponent() {
    queryClient.invalidateQueries({ queryKey: ["mlops", "versions", selectedComponentId] });
    queryClient.invalidateQueries({ queryKey: ["mlops", "governance-events", selectedComponentId] });
    queryClient.invalidateQueries({ queryKey: ["mlops", "monitoring"] });
  }

  const registerComponentMutation = useMutation({
    mutationFn: () => api.post<ComponentResponse>("/mlops/components", { component_type: newComponentType, name: newComponentName, description: newComponentDescription || undefined }),
    onSuccess: (c) => {
      queryClient.invalidateQueries({ queryKey: ["mlops", "components"] });
      setSelectedComponentId(c.id);
      setNewComponentName("");
      setNewComponentDescription("");
    },
  });

  const registerVersionMutation = useMutation({
    mutationFn: () =>
      api.post<ComponentVersionResponse>(`/mlops/components/${selectedComponentId}/versions`, {
        version_kind: versionKind,
        model_version_id: versionKind === "model_version" ? underlyingId : undefined,
        forecast_model_id: versionKind === "forecast_model" ? underlyingId : undefined,
        heuristic_code_version: versionKind === "heuristic" ? heuristicCodeVersion : undefined,
      }),
    onSuccess: (v) => {
      invalidateComponent();
      setSelectedVersionId(v.id);
      setShowRegisterVersion(false);
      setUnderlyingId("");
      setHeuristicCodeVersion("");
    },
  });

  const evaluateMutation = useMutation({ mutationFn: () => api.post<ComponentVersionResponse>(`/mlops/versions/${selectedVersionId}/evaluate`, {}), onSuccess: invalidateComponent });
  const validateMutation = useMutation({
    mutationFn: () => api.post<ComponentVersionResponse>(`/mlops/versions/${selectedVersionId}/validate`, { notes: validateNotes, evidence: reviewedReference ? { reviewed_reference: reviewedReference } : undefined }),
    onSuccess: () => {
      invalidateComponent();
      setValidateNotes("");
      setReviewedReference("");
    },
  });
  const approveMutation = useMutation({
    mutationFn: () => api.post<ComponentVersionResponse>(`/mlops/versions/${selectedVersionId}/approve`, { notes: approveNotes || undefined }),
    onSuccess: () => {
      invalidateComponent();
      setApproveNotes("");
    },
  });
  const activateMutation = useMutation({
    mutationFn: () => api.post<ComponentVersionResponse>(`/mlops/components/${selectedComponentId}/activate`, { component_version_id: selectedVersionId }),
    onSuccess: invalidateComponent,
  });
  const rollbackMutation = useMutation({ mutationFn: () => api.post<ComponentVersionResponse>(`/mlops/components/${selectedComponentId}/rollback`, {}), onSuccess: invalidateComponent });
  const retireMutation = useMutation({
    mutationFn: () => api.post<ComponentVersionResponse>(`/mlops/versions/${selectedVersionId}/retire`, { reason: retireReason }),
    onSuccess: () => {
      invalidateComponent();
      setRetireReason("");
    },
  });

  const requestRetrainingMutation = useMutation({
    mutationFn: () => {
      let config: Record<string, unknown>;
      try {
        config = JSON.parse(retrainConfig);
      } catch {
        throw new Error("Config must be valid JSON.");
      }
      return api.post<RetrainingRequestResponse>(`/mlops/components/${selectedComponentId}/retraining-requests`, { reason: retrainReason, config });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mlops", "retraining-requests", selectedComponentId] });
      setRetrainReason("");
    },
  });
  const executeRetrainingMutation = useMutation({
    mutationFn: (requestId: string) => api.post<RetrainingRequestResponse>(`/mlops/retraining-requests/${requestId}/execute`, {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mlops", "retraining-requests", selectedComponentId] });
      invalidateComponent();
    },
  });

  const shiftAnalysisMutation = useMutation({
    mutationFn: () =>
      api.post<ShiftAnalysisRunResponse>("/mlops/shift-analysis", {
        sensor_id: shiftSensorId,
        reference_start: new Date(refStart).toISOString(),
        reference_end: new Date(refEnd).toISOString(),
        comparison_start: new Date(cmpStart).toISOString(),
        comparison_end: new Date(cmpEnd).toISOString(),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["mlops", "shift-analysis"] }),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Intelligence Lifecycle & Governance</h1>
        <p className="mt-1 text-sm text-slate-500">
          Every trained detector, forecaster, and heuristic method is registered here under one honest vocabulary
          (never calling a statistical detector or heuristic &ldquo;a machine learning model&rdquo;) and moves
          through an explicit, audited lifecycle — evaluate, validate, approve, activate — before anything is scored
          through it. Nothing here is activated automatically, and older versions are demoted, never deleted.
        </p>
      </div>

      {/* ---------------------------------------------------------------- Monitoring strip */}
      {monitoring && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <StatCard title="Registered components" value={String(Object.values(monitoring.operational.component_count_by_type).reduce((a, b) => a + b, 0))} />
          <StatCard title="Active versions" value={String(monitoring.operational.active_components.length)} />
          <StatCard title="Versions awaiting decision" value={String((monitoring.operational.component_version_count_by_lifecycle_stage.evaluated ?? 0) + (monitoring.operational.component_version_count_by_lifecycle_stage.validated ?? 0))} />
          <StatCard title="Shift analyses recorded" value={String(monitoring.data.shift_analysis_run_count)} />
        </div>
      )}

      {/* ---------------------------------------------------------------- Register a component */}
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h3 className="font-semibold text-slate-900">Register a component</h3>
        <p className="mt-1 text-xs text-slate-500">The logical identity of one capability (e.g. a specific detector on a specific sensor) — independent of any one trained version.</p>
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-4">
          <select value={newComponentType} onChange={(e) => setNewComponentType(e.target.value as ComponentType)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {(Object.keys(COMPONENT_TYPE_LABELS) as ComponentType[]).map((t) => <option key={t} value={t}>{COMPONENT_TYPE_LABELS[t]}</option>)}
          </select>
          <input value={newComponentName} onChange={(e) => setNewComponentName(e.target.value)} placeholder="Name — must match the underlying detector/forecast model name" className="rounded-md border border-slate-300 px-2 py-1.5 text-sm sm:col-span-2" />
          <input value={newComponentDescription} onChange={(e) => setNewComponentDescription(e.target.value)} placeholder="Optional description" className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
        </div>
        <button onClick={() => registerComponentMutation.mutate()} disabled={registerComponentMutation.isPending || !newComponentName} className="mt-3 rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50">
          {registerComponentMutation.isPending ? "Registering…" : "Register component"}
        </button>
        {registerComponentMutation.isError && <p className="mt-2 text-sm text-red-700">{registerComponentMutation.error instanceof ApiError ? registerComponentMutation.error.message : "Failed."}</p>}
      </div>

      {/* ---------------------------------------------------------------- Component list */}
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h3 className="font-semibold text-slate-900">Components</h3>
        {componentsQuery.isLoading && <LoadingState label="Loading components..." />}
        {!componentsQuery.isLoading && components.length === 0 && <EmptyState title="No components registered" description="Register one above, referencing an already-trained detector or forecast model." />}
        {components.length > 0 && (
          <ul className="mt-3 space-y-1.5">
            {components.map((c) => (
              <li
                key={c.id}
                onClick={() => { setSelectedComponentId(selectedComponentId === c.id ? null : c.id); setSelectedVersionId(null); }}
                className={`cursor-pointer rounded-lg border px-3 py-2 text-sm hover:border-brand-300 ${selectedComponentId === c.id ? "border-brand-400 bg-brand-50/40" : "border-slate-200"}`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium text-slate-900">{c.name}</span>
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">{COMPONENT_TYPE_LABELS[c.component_type]}</span>
                </div>
                {c.description && <p className="mt-0.5 text-xs text-slate-500">{c.description}</p>}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* ---------------------------------------------------------------- Selected component: versions */}
      {selectedComponent && (
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-slate-900">{selectedComponent.name} — versions</h3>
            <div className="flex gap-2">
              <button onClick={() => rollbackMutation.mutate()} disabled={rollbackMutation.isPending} className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
                {rollbackMutation.isPending ? "Rolling back…" : "Roll back to previous"}
              </button>
              <button onClick={() => setShowRegisterVersion((v) => !v)} className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50">
                {showRegisterVersion ? "Cancel" : "Register version"}
              </button>
            </div>
          </div>
          {rollbackMutation.isError && <p className="mt-2 text-xs text-red-700">{rollbackMutation.error instanceof ApiError ? rollbackMutation.error.message : "Rollback failed."}</p>}

          {showRegisterVersion && (
            <div className="mt-3 grid grid-cols-1 gap-3 rounded-lg border border-slate-200 bg-slate-50 p-4 sm:grid-cols-3">
              <select value={versionKind} onChange={(e) => setVersionKind(e.target.value as VersionKind)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                <option value="model_version">model_version (detector id from /anomaly/detectors)</option>
                <option value="forecast_model">forecast_model (id from /condition/forecast-models)</option>
                <option value="heuristic">heuristic (no id — a code version string)</option>
              </select>
              {versionKind === "heuristic" ? (
                <input value={heuristicCodeVersion} onChange={(e) => setHeuristicCodeVersion(e.target.value)} placeholder="e.g. degradation_classifier@1" className="rounded-md border border-slate-300 px-2 py-1.5 text-sm sm:col-span-2" />
              ) : (
                <input value={underlyingId} onChange={(e) => setUnderlyingId(e.target.value)} placeholder="Underlying id" className="rounded-md border border-slate-300 px-2 py-1.5 text-sm sm:col-span-2" />
              )}
              <button onClick={() => registerVersionMutation.mutate()} disabled={registerVersionMutation.isPending} className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50 sm:col-span-3">
                {registerVersionMutation.isPending ? "Registering…" : "Register version"}
              </button>
              {registerVersionMutation.isError && <p className="text-sm text-red-700 sm:col-span-3">{registerVersionMutation.error instanceof ApiError ? registerVersionMutation.error.message : "Failed."}</p>}
            </div>
          )}

          {versionsQuery.isLoading && <LoadingState label="Loading versions..." />}
          {!versionsQuery.isLoading && versions.length === 0 && <EmptyState title="No versions yet" description="Register one above." />}
          {versions.length > 0 && (
            <table className="mt-4 w-full text-left text-sm">
              <thead className="text-xs text-slate-500"><tr><th className="pb-2">Version</th><th className="pb-2">Kind</th><th className="pb-2">Stage</th><th className="pb-2" /></tr></thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.id} className={`border-t border-slate-100 ${selectedVersionId === v.id ? "bg-brand-50/50" : ""}`}>
                    <td className="py-2 font-medium text-slate-900">v{v.version_number}</td>
                    <td className="py-2 text-xs text-slate-500">{v.version_kind}</td>
                    <td className="py-2"><StageBadge stage={v.lifecycle_stage} /></td>
                    <td className="py-2 text-right">
                      <button onClick={() => setSelectedVersionId(selectedVersionId === v.id ? null : v.id)} className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50">
                        {selectedVersionId === v.id ? "Close" : "Manage"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* ---------------------------------------------------------------- Selected version: lifecycle actions */}
      {selectedVersion && (
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-slate-900">v{selectedVersion.version_number} lifecycle</h3>
            <StageBadge stage={selectedVersion.lifecycle_stage} />
          </div>

          {selectedVersion.lifecycle_stage === "trained" && selectedVersion.version_kind === "model_version" && (
            <div className="mt-3 rounded-lg border border-slate-200 p-4">
              <p className="text-sm text-slate-700">Run the real synthetic evaluation harness before this can be validated.</p>
              <button onClick={() => evaluateMutation.mutate()} disabled={evaluateMutation.isPending} className="mt-2 rounded-md bg-brand-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-700 disabled:opacity-50">
                {evaluateMutation.isPending ? "Evaluating…" : "Run synthetic evaluation"}
              </button>
              {evaluateMutation.isError && <p className="mt-2 text-xs text-red-700">{evaluateMutation.error instanceof ApiError ? evaluateMutation.error.message : "Failed."}</p>}
            </div>
          )}

          {selectedVersion.lifecycle_stage === "evaluated" && (
            <div className="mt-3 rounded-lg border border-slate-200 p-4">
              <p className="text-sm font-medium text-slate-900">Human validation</p>
              <p className="mt-1 text-xs text-slate-500">
                {selectedVersion.version_kind === "heuristic"
                  ? "No trained artifact exists to evaluate — cite what was actually reviewed (a doc section or test file)."
                  : "Requires a written attestation of what was reviewed."}
              </p>
              <textarea value={validateNotes} onChange={(e) => setValidateNotes(e.target.value)} placeholder="Required: notes on what was reviewed..." rows={2} className="mt-2 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              {selectedVersion.version_kind === "heuristic" && (
                <input value={reviewedReference} onChange={(e) => setReviewedReference(e.target.value)} placeholder="Required: reviewed_reference (e.g. module + test file)" className="mt-2 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              )}
              <button
                onClick={() => validateMutation.mutate()}
                disabled={validateMutation.isPending || !validateNotes.trim() || (selectedVersion.version_kind === "heuristic" && !reviewedReference.trim())}
                className="mt-2 rounded-md bg-brand-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-700 disabled:opacity-50"
              >
                {validateMutation.isPending ? "Validating…" : "Validate"}
              </button>
              {validateMutation.isError && <p className="mt-2 text-xs text-red-700">{validateMutation.error instanceof ApiError ? validateMutation.error.message : "Failed — see gate requirements above."}</p>}
            </div>
          )}

          {selectedVersion.lifecycle_stage === "validated" && (
            <div className="mt-3 rounded-lg border border-slate-200 p-4">
              <p className="text-sm font-medium text-slate-900">Approval</p>
              <textarea value={approveNotes} onChange={(e) => setApproveNotes(e.target.value)} placeholder="Optional approval notes..." rows={2} className="mt-2 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              <button onClick={() => approveMutation.mutate()} disabled={approveMutation.isPending} className="mt-2 rounded-md bg-brand-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-700 disabled:opacity-50">
                {approveMutation.isPending ? "Approving…" : "Approve"}
              </button>
            </div>
          )}

          {selectedVersion.lifecycle_stage === "approved" && (
            <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50/50 p-4">
              <p className="text-sm font-medium text-amber-900">Activation</p>
              <p className="mt-1 text-xs text-amber-800">Puts this version into use for the component — any currently active version is demoted (never deleted), not deactivated blindly.</p>
              <button onClick={() => activateMutation.mutate()} disabled={activateMutation.isPending} className="mt-2 rounded-md bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-50">
                {activateMutation.isPending ? "Activating…" : "Activate"}
              </button>
              {activateMutation.isError && <p className="mt-2 text-xs text-red-700">{activateMutation.error instanceof ApiError ? activateMutation.error.message : "Failed."}</p>}
            </div>
          )}

          {selectedVersion.lifecycle_stage !== "retired" && (
            <div className="mt-3 rounded-lg border border-slate-200 p-4">
              <p className="text-sm font-medium text-slate-900">Retire this version</p>
              <input value={retireReason} onChange={(e) => setRetireReason(e.target.value)} placeholder="Required: reason" className="mt-2 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              <button onClick={() => retireMutation.mutate()} disabled={retireMutation.isPending || !retireReason.trim()} className="mt-2 rounded-md border border-red-300 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50">
                {retireMutation.isPending ? "Retiring…" : "Retire"}
              </button>
            </div>
          )}

          {selectedVersion.validation_evidence && (
            <div className="mt-3 rounded-lg border border-slate-200 p-4">
              <p className="text-xs font-medium text-slate-500">Recorded validation evidence</p>
              <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-xs text-slate-600">{JSON.stringify(selectedVersion.validation_evidence, null, 2)}</pre>
            </div>
          )}
        </div>
      )}

      {/* ---------------------------------------------------------------- Retraining */}
      {selectedComponent && (
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <h3 className="font-semibold text-slate-900">Controlled retraining</h3>
          <p className="mt-1 text-xs text-slate-500">Calls the same real training code as the underlying detector/forecast API — never simulated. Never activates the result automatically.</p>
          <div className="mt-3 grid grid-cols-1 gap-3">
            <input value={retrainReason} onChange={(e) => setRetrainReason(e.target.value)} placeholder="Required: reason for retraining" className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            <textarea value={retrainConfig} onChange={(e) => setRetrainConfig(e.target.value)} rows={3} className="rounded-md border border-slate-300 px-2 py-1.5 font-mono text-xs" placeholder='{"sensor_ids": ["..."], "reference_start": "...", "reference_end": "..."}' />
            <button onClick={() => requestRetrainingMutation.mutate()} disabled={requestRetrainingMutation.isPending || !retrainReason.trim()} className="w-fit rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50">
              {requestRetrainingMutation.isPending ? "Requesting…" : "Request retraining"}
            </button>
            {requestRetrainingMutation.isError && <p className="text-sm text-red-700">{requestRetrainingMutation.error instanceof Error ? requestRetrainingMutation.error.message : "Failed."}</p>}
          </div>

          {(retrainingQuery.data?.items.length ?? 0) > 0 && (
            <table className="mt-4 w-full text-left text-sm">
              <thead className="text-xs text-slate-500"><tr><th className="pb-2">Requested</th><th className="pb-2">Reason</th><th className="pb-2">Status</th><th className="pb-2" /></tr></thead>
              <tbody>
                {retrainingQuery.data!.items.map((r) => (
                  <tr key={r.id} className="border-t border-slate-100">
                    <td className="py-2 text-xs text-slate-500">{new Date(r.created_at).toLocaleString()}</td>
                    <td className="py-2 text-slate-700">{r.reason}</td>
                    <td className="py-2 text-xs">{r.status}</td>
                    <td className="py-2 text-right">
                      {r.status === "requested" && (
                        <button onClick={() => executeRetrainingMutation.mutate(r.id)} disabled={executeRetrainingMutation.isPending} className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50">
                          Execute
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* ---------------------------------------------------------------- Governance audit log */}
      {selectedComponent && (
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <h3 className="font-semibold text-slate-900">Governance audit log</h3>
          {governanceQuery.isLoading && <LoadingState label="Loading events..." />}
          {governanceQuery.data && governanceQuery.data.items.length === 0 && <EmptyState title="No events yet" description="Actions on this component's versions will appear here." />}
          {governanceQuery.data && governanceQuery.data.items.length > 0 && (
            <ul className="mt-3 max-h-64 space-y-1 overflow-auto text-xs text-slate-600">
              {governanceQuery.data.items.map((e) => (
                <li key={e.id}>
                  {new Date(e.created_at).toLocaleString()} — <span className="font-medium text-slate-800">{e.event_type}</span>
                  {e.from_stage && e.to_stage ? ` (${e.from_stage} → ${e.to_stage})` : ""}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* ---------------------------------------------------------------- Shift analysis */}
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h3 className="font-semibold text-slate-900">Data / feature shift analysis</h3>
        <p className="mt-1 text-xs text-slate-500">
          A discrete, on-demand comparison between two stated periods — summary statistics plus a Welch t-test and a
          Kolmogorov–Smirnov test where both periods have enough observations. Never a continuous drift score, and
          never triggers retraining by itself.
        </p>
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <select value={shiftAssetId} onChange={(e) => { setShiftAssetId(e.target.value); setShiftSensorId(""); }} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">Select an asset…</option>
            {(assetsQuery.data?.items ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
          <select value={shiftSensorId} onChange={(e) => setShiftSensorId(e.target.value)} disabled={!shiftAssetId} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm disabled:bg-slate-50">
            <option value="">Select a sensor…</option>
            {(shiftSensorsQuery.data?.items ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </div>
        <p className="mt-3 text-xs font-medium text-slate-500">Reference period</p>
        <div className="mt-1 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <input type="datetime-local" value={refStart} onChange={(e) => setRefStart(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          <input type="datetime-local" value={refEnd} onChange={(e) => setRefEnd(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
        </div>
        <p className="mt-3 text-xs font-medium text-slate-500">Comparison period</p>
        <div className="mt-1 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <input type="datetime-local" value={cmpStart} onChange={(e) => setCmpStart(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          <input type="datetime-local" value={cmpEnd} onChange={(e) => setCmpEnd(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
        </div>
        <button onClick={() => shiftAnalysisMutation.mutate()} disabled={shiftAnalysisMutation.isPending || !shiftSensorId} className="mt-3 rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50">
          {shiftAnalysisMutation.isPending ? "Analyzing…" : "Run shift analysis"}
        </button>
        {shiftAnalysisMutation.isError && <p className="mt-2 text-sm text-red-700">{shiftAnalysisMutation.error instanceof ApiError ? shiftAnalysisMutation.error.message : "Failed."}</p>}

        {(shiftRunsQuery.data?.items.length ?? 0) > 0 && (
          <table className="mt-4 w-full text-left text-sm">
            <thead className="text-xs text-slate-500"><tr><th className="pb-2">Run</th><th className="pb-2">Method</th><th className="pb-2">Result</th></tr></thead>
            <tbody>
              {shiftRunsQuery.data!.items.map((r) => (
                <tr key={r.id} className="border-t border-slate-100">
                  <td className="py-2 text-xs text-slate-500">{new Date(r.created_at).toLocaleString()}</td>
                  <td className="py-2 text-xs text-slate-600">{r.method}</td>
                  <td className="py-2 text-xs text-slate-700">
                    {r.result.test_performed ? `mean Δ ${(r.result.mean_difference as number).toFixed(3)}, KS p=${(r.result.ks_2samp_test as { p_value: number }).p_value.toFixed(4)}` : (r.result.reason as string)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function StatCard({ title, value }: { title: string; value: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <p className="text-xs font-medium text-slate-500">{title}</p>
      <p className="mt-1 text-lg font-semibold text-slate-900">{value}</p>
    </div>
  );
}
