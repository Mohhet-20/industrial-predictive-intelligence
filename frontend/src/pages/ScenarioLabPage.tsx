import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useMemo, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { api, ApiError } from "@/api/client";
import type {
  AssetResponse,
  Page,
  ScenarioCompareResponse,
  ScenarioResponse,
  ScenarioRunKind,
  ScenarioRunResponse,
  ScenarioRunStatus,
  ScenarioStatus,
  ScenarioType,
  SensorResponse,
} from "@/api/types";
import { EmptyState, ErrorState, LoadingState } from "@/components/StateViews";

// ---------------------------------------------------------------------------
// THIS PAGE IS SCENARIO ANALYSIS, NOT A PHYSICAL DIGITAL TWIN.
//
// Every scenario built here is a hypothetical, non-destructive "what-if"
// applied to a COPY of real historical sensor data; the platform's real
// measurement history is never touched (see the "non-destructive" tests in
// backend/tests/test_api_scenario.py). Every result rendered on this page
// carries a visible SIMULATED label and is styled distinctly (purple/violet
// accents throughout) from every other page's real observed-data styling
// (slate/blue/emerald), so a simulated number can never be mistaken for a
// real observation at a glance.
// ---------------------------------------------------------------------------

function isoLocal(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fmt(n: unknown, digits = 3): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toFixed(digits) : "—";
}

function get(obj: unknown, path: string[]): unknown {
  let cur: unknown = obj;
  for (const key of path) {
    if (cur === null || cur === undefined || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[key];
  }
  return cur;
}

const SCENARIO_TYPE_LABELS: Record<ScenarioType, string> = {
  level_shift: "Level shift — mean shifts by a fixed amount",
  trend_change: "Trend change — rate of change shifts (units/hour)",
  variability_change: "Variability change — noise scaled up or down",
  temporary_spike: "Temporary spike — bounded bump, then reverts",
  drift: "Drift — total amount accumulated by window end",
  missing_data_gap: "Missing-data gap — observations dropped, not changed",
  multi_sensor: "Multi-sensor — independent scenarios on several sensors",
};

const SCENARIO_STATUS_STYLES: Record<ScenarioStatus, string> = {
  draft: "bg-slate-100 text-slate-700",
  validated: "bg-emerald-100 text-emerald-800",
  archived: "bg-slate-200 text-slate-500",
};
function ScenarioStatusBadge({ status }: { status: ScenarioStatus }) {
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${SCENARIO_STATUS_STYLES[status]}`}>{status}</span>;
}

const RUN_STATUS_STYLES: Record<ScenarioRunStatus, string> = {
  queued: "bg-slate-100 text-slate-600",
  running: "bg-blue-100 text-blue-800",
  succeeded: "bg-violet-100 text-violet-800",
  failed: "bg-red-100 text-red-800",
};
function RunStatusBadge({ status }: { status: ScenarioRunStatus }) {
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${RUN_STATUS_STYLES[status]}`}>{status}</span>;
}

function RunKindBadge({ kind }: { kind: ScenarioRunKind }) {
  return (
    <span className="inline-block rounded-full bg-indigo-100 px-2 py-0.5 text-xs font-medium text-indigo-800">
      {kind === "sensitivity_sweep" ? "sensitivity sweep" : "single run"}
    </span>
  );
}

/** Every rendered scenario result is wrapped in this — a loud, unmissable
 * violet banner so it can never read like a real observation (§13). */
function SimulatedBanner({ children }: { children?: ReactNode }) {
  return (
    <div className="rounded-lg border-2 border-dashed border-violet-300 bg-violet-50 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-violet-800">
      Simulated scenario result — hypothetical, not a real observation
      {children}
    </div>
  );
}

const PER_SENSOR_EXAMPLE = `[
  {
    "sensor_id": "<sensor id>",
    "scenario_type": "level_shift",
    "modification_start": "2026-01-05T00:00:00Z",
    "modification_duration_seconds": 86400,
    "magnitude": 5.0,
    "unit": "C"
  }
]`;

export function ScenarioLabPage() {
  const queryClient = useQueryClient();
  const now = useMemo(() => new Date(), []);
  const daysAgo = (d: number) => isoLocal(new Date(now.getTime() - d * 24 * 3600 * 1000));

  // -- Asset / sensor scope --------------------------------------------------
  const [assetId, setAssetId] = useState("");
  const [sensorId, setSensorId] = useState("");

  // -- Create-scenario form ---------------------------------------------------
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scenarioType, setScenarioType] = useState<ScenarioType>("level_shift");
  const [baselineStart, setBaselineStart] = useState(daysAgo(14));
  const [baselineEnd, setBaselineEnd] = useState(daysAgo(0));
  const [useAutoReference, setUseAutoReference] = useState(true);
  const [referenceStart, setReferenceStart] = useState(daysAgo(28));
  const [referenceEnd, setReferenceEnd] = useState(daysAgo(14));
  const [modificationStart, setModificationStart] = useState(daysAgo(7));
  const [durationHours, setDurationHours] = useState<string>("");
  const [magnitude, setMagnitude] = useState<string>("1.0");
  const [unit, setUnit] = useState("");
  const [rollingWindowObs, setRollingWindowObs] = useState<string>("15");
  const [spikeShape, setSpikeShape] = useState<"rectangular" | "triangular">("rectangular");
  const [extraSensorIds, setExtraSensorIds] = useState<string[]>([]);
  const [perSensorJson, setPerSensorJson] = useState(PER_SENSOR_EXAMPLE);
  const [createError, setCreateError] = useState<string | null>(null);

  // -- Selection / run state ---------------------------------------------------
  const [selectedScenarioId, setSelectedScenarioId] = useState<string | null>(null);
  const [includeForecast, setIncludeForecast] = useState(false);
  const [sweepMagnitudes, setSweepMagnitudes] = useState("1,2,4");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [compareRunIds, setCompareRunIds] = useState<string[]>([]);
  const [compareResult, setCompareResult] = useState<ScenarioCompareResponse["data"] | null>(null);

  // -- Queries -------------------------------------------------------------
  const assetsQuery = useQuery({ queryKey: ["assets", "scenario-lab"], queryFn: () => api.get<Page<AssetResponse>>("/assets", { limit: 200 }) });
  const sensorsQuery = useQuery({
    queryKey: ["assets", assetId, "sensors", "scenario-lab"],
    queryFn: () => api.get<Page<SensorResponse>>(`/assets/${assetId}/sensors`, { limit: 100 }),
    enabled: !!assetId,
  });
  const sensors = sensorsQuery.data?.items ?? [];

  const scenariosQuery = useQuery({
    queryKey: ["scenarios", assetId],
    queryFn: () => api.get<Page<ScenarioResponse>>("/scenarios", { asset_id: assetId, limit: 100 }),
    enabled: !!assetId,
  });
  const scenarios = scenariosQuery.data?.items ?? [];
  const selectedScenario = scenarios.find((s) => s.id === selectedScenarioId) ?? null;

  const runsQuery = useQuery({
    queryKey: ["scenarios", selectedScenarioId, "runs"],
    queryFn: () => api.get<Page<ScenarioRunResponse>>(`/scenarios/${selectedScenarioId}/runs`, { limit: 50 }),
    enabled: !!selectedScenarioId,
    refetchInterval: (query) => (query.state.data?.items.some((r) => r.status === "queued" || r.status === "running") ? 2500 : false),
  });
  const runs = runsQuery.data?.items ?? [];
  const selectedRun = runs.find((r) => r.id === selectedRunId) ?? null;

  // -- Mutations -------------------------------------------------------------
  const createScenarioMutation = useMutation({
    mutationFn: () => {
      const parameters: Record<string, unknown> = {};
      if (scenarioType === "variability_change" && rollingWindowObs) parameters.rolling_window_observations = Number(rollingWindowObs);
      if (scenarioType === "temporary_spike") parameters.shape = spikeShape;
      if (scenarioType === "multi_sensor") {
        try {
          parameters.per_sensor = JSON.parse(perSensorJson);
        } catch {
          throw new Error("Per-sensor configuration is not valid JSON.");
        }
      }
      return api.post<ScenarioResponse>("/scenarios", {
        asset_id: assetId,
        sensor_id: sensorId,
        sensor_scope: scenarioType === "multi_sensor" ? extraSensorIds : undefined,
        name,
        description: description || undefined,
        scenario_type: scenarioType,
        baseline_window_start: new Date(baselineStart).toISOString(),
        baseline_window_end: new Date(baselineEnd).toISOString(),
        reference_window_start: useAutoReference ? undefined : new Date(referenceStart).toISOString(),
        reference_window_end: useAutoReference ? undefined : new Date(referenceEnd).toISOString(),
        modification_start: scenarioType === "multi_sensor" ? undefined : new Date(modificationStart).toISOString(),
        modification_duration_seconds: scenarioType === "multi_sensor" || !durationHours ? undefined : Number(durationHours) * 3600,
        magnitude: scenarioType === "multi_sensor" ? undefined : Number(magnitude),
        unit: unit || undefined,
        parameters,
      });
    },
    onSuccess: (scenario) => {
      setCreateError(null);
      queryClient.invalidateQueries({ queryKey: ["scenarios", assetId] });
      setSelectedScenarioId(scenario.id);
      setName("");
      setDescription("");
    },
    onError: (err) => setCreateError(err instanceof ApiError ? err.message : err instanceof Error ? err.message : "Failed to create scenario."),
  });

  const validateMutation = useMutation({
    mutationFn: () => api.post<ScenarioResponse>(`/scenarios/${selectedScenarioId}/validate`, {}),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["scenarios", assetId] }),
  });

  const archiveMutation = useMutation({
    mutationFn: (id: string) => api.del<ScenarioResponse>(`/scenarios/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["scenarios", assetId] });
      setSelectedScenarioId(null);
    },
  });

  const runMutation = useMutation({
    mutationFn: () => api.post<ScenarioRunResponse>(`/scenarios/${selectedScenarioId}/runs`, { include_forecast_comparison: includeForecast }),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ["scenarios", selectedScenarioId, "runs"] });
      setSelectedRunId(run.id);
    },
  });

  const sweepMutation = useMutation({
    mutationFn: () =>
      api.post<ScenarioRunResponse>(`/scenarios/${selectedScenarioId}/sensitivity`, {
        magnitudes: sweepMagnitudes.split(",").map((s) => Number(s.trim())).filter((n) => Number.isFinite(n)),
      }),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ["scenarios", selectedScenarioId, "runs"] });
      setSelectedRunId(run.id);
    },
  });

  const compareMutation = useMutation({
    mutationFn: () => api.post<ScenarioCompareResponse>("/scenarios/compare", { scenario_run_ids: compareRunIds }),
    onSuccess: (res) => setCompareResult(res.data),
  });

  const canRun = selectedScenario?.status === "validated";
  const canSweep = canRun && selectedScenario?.scenario_type !== "multi_sensor";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Scenario Lab</h1>
        <p className="mt-1 text-sm text-slate-500">
          Build bounded, reproducible &ldquo;what-if&rdquo; scenarios against a copy of real historical data, then run
          this platform&rsquo;s real analytical pipeline against it.
        </p>
      </div>

      <div className="rounded-xl border-2 border-violet-200 bg-violet-50/60 p-4 text-sm text-violet-900">
        <p className="font-semibold">This is scenario analysis, not a physical digital twin.</p>
        <p className="mt-1 text-violet-800">
          A scenario applies a declared, arithmetic modification (a level shift, a ramp, a spike, added drift,
          scaled variability, or a dropped-observations gap) to a copy of real sensor history — it never touches
          the original measurements. The
          platform runs the SAME robust z-score, condition-evidence, degradation, and change-detection logic it runs
          on real data, so results reflect how those real detectors would respond, not a validated physical model of
          this equipment. No result on this page is a guaranteed prediction of what will actually happen.
        </p>
      </div>

      {/* ------------------------------------------------------------ Asset/sensor scope */}
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h3 className="font-semibold text-slate-900">Scope</h3>
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label className="block text-xs font-medium text-slate-500">Asset</label>
            <select
              value={assetId}
              onChange={(e) => { setAssetId(e.target.value); setSensorId(""); setSelectedScenarioId(null); setExtraSensorIds([]); }}
              className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            >
              <option value="">Select an asset…</option>
              {(assetsQuery.data?.items ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-500">Primary sensor</label>
            <select value={sensorId} onChange={(e) => setSensorId(e.target.value)} disabled={!assetId} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm disabled:bg-slate-50">
              <option value="">Select a sensor…</option>
              {sensors.map((s) => <option key={s.id} value={s.id}>{s.name} ({s.unit})</option>)}
            </select>
          </div>
        </div>
      </div>

      {/* ------------------------------------------------------------ Create scenario */}
      {assetId && sensorId && (
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <h3 className="font-semibold text-slate-900">Create a scenario</h3>
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className="block text-xs font-medium text-slate-500">Name</label>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. what if bearing temp shifts +8C" className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-500">Scenario type</label>
              <select value={scenarioType} onChange={(e) => setScenarioType(e.target.value as ScenarioType)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                {(Object.keys(SCENARIO_TYPE_LABELS) as ScenarioType[]).map((t) => <option key={t} value={t}>{SCENARIO_TYPE_LABELS[t]}</option>)}
              </select>
            </div>
            <div className="sm:col-span-2">
              <label className="block text-xs font-medium text-slate-500">Description (optional)</label>
              <input value={description} onChange={(e) => setDescription(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </div>
          </div>

          <p className="mt-4 text-xs font-medium text-slate-500">Baseline window (the real historical data this scenario is built from)</p>
          <div className="mt-1 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <input type="datetime-local" value={baselineStart} onChange={(e) => setBaselineStart(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            <input type="datetime-local" value={baselineEnd} onChange={(e) => setBaselineEnd(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          </div>

          <div className="mt-4 flex items-center gap-2">
            <input id="auto-ref" type="checkbox" checked={useAutoReference} onChange={(e) => setUseAutoReference(e.target.checked)} />
            <label htmlFor="auto-ref" className="text-xs font-medium text-slate-500">
              Auto-select reference window (an equal-length period immediately before the baseline window)
            </label>
          </div>
          {!useAutoReference && (
            <div className="mt-1 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <input type="datetime-local" value={referenceStart} onChange={(e) => setReferenceStart(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              <input type="datetime-local" value={referenceEnd} onChange={(e) => setReferenceEnd(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </div>
          )}

          {scenarioType !== "multi_sensor" ? (
            <>
              <p className="mt-4 text-xs font-medium text-slate-500">Modification</p>
              <div className="mt-1 grid grid-cols-1 gap-3 sm:grid-cols-4">
                <div>
                  <label className="block text-xs text-slate-400">Starts at</label>
                  <input type="datetime-local" value={modificationStart} onChange={(e) => setModificationStart(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
                </div>
                <div>
                  <label className="block text-xs text-slate-400">
                    Duration (hours{scenarioType === "temporary_spike" || scenarioType === "missing_data_gap" ? ", required" : ", optional — blank = to end of window"})
                  </label>
                  <input type="number" min={0} value={durationHours} onChange={(e) => setDurationHours(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
                </div>
                <div>
                  <label className="block text-xs text-slate-400">
                    Magnitude {scenarioType === "trend_change" ? "(units/hour)" : scenarioType === "variability_change" ? "(multiplier, e.g. 2 = twice as noisy)" : scenarioType === "missing_data_gap" ? "(fraction 0–1 of in-window points to drop; 1 = full gap)" : "(sensor units)"}
                  </label>
                  <input type="number" step="any" value={magnitude} onChange={(e) => setMagnitude(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
                </div>
                <div>
                  <label className="block text-xs text-slate-400">Unit (optional)</label>
                  <input value={unit} onChange={(e) => setUnit(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
                </div>
              </div>

              {scenarioType === "variability_change" && (
                <div className="mt-3">
                  <label className="block text-xs text-slate-400">Rolling window (observations, for the median used to rescale deviation)</label>
                  <input type="number" min={1} value={rollingWindowObs} onChange={(e) => setRollingWindowObs(e.target.value)} className="mt-1 w-40 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
                </div>
              )}
              {scenarioType === "temporary_spike" && (
                <div className="mt-3">
                  <label className="block text-xs text-slate-400">Shape</label>
                  <select value={spikeShape} onChange={(e) => setSpikeShape(e.target.value as "rectangular" | "triangular")} className="mt-1 w-40 rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                    <option value="rectangular">Rectangular (full magnitude across window)</option>
                    <option value="triangular">Triangular (ramps up, peaks at midpoint, ramps down)</option>
                  </select>
                </div>
              )}
            </>
          ) : (
            <>
              <p className="mt-4 text-xs font-medium text-slate-500">
                Additional sensors on this asset (each runs its own, completely independent scenario — this platform
                never infers a relationship between them)
              </p>
              <div className="mt-1 flex flex-wrap gap-3">
                {sensors.filter((s) => s.id !== sensorId).map((s) => (
                  <label key={s.id} className="flex items-center gap-1.5 text-xs text-slate-700">
                    <input
                      type="checkbox"
                      checked={extraSensorIds.includes(s.id)}
                      onChange={(e) => setExtraSensorIds((cur) => (e.target.checked ? [...cur, s.id] : cur.filter((id) => id !== s.id)))}
                    />
                    {s.name}
                  </label>
                ))}
                {sensors.length <= 1 && <span className="text-xs text-slate-400">No other sensors on this asset.</span>}
              </div>
              <p className="mt-3 text-xs font-medium text-slate-500">
                Per-sensor configuration (JSON array — one entry per sensor in scope, each with its own scenario_type,
                modification_start, magnitude, etc.)
              </p>
              <textarea value={perSensorJson} onChange={(e) => setPerSensorJson(e.target.value)} rows={8} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 font-mono text-xs" />
            </>
          )}

          <div className="mt-4">
            <button
              onClick={() => createScenarioMutation.mutate()}
              disabled={createScenarioMutation.isPending || !name.trim()}
              className="rounded-md bg-violet-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-violet-700 disabled:opacity-50"
            >
              {createScenarioMutation.isPending ? "Creating…" : "Create scenario"}
            </button>
            {createError && <p className="mt-2 text-sm text-red-700">{createError}</p>}
          </div>
        </div>
      )}

      {/* ------------------------------------------------------------ Scenario list */}
      {assetId && (
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <h3 className="font-semibold text-slate-900">Scenarios for this asset</h3>
          {scenariosQuery.isLoading && <LoadingState label="Loading scenarios..." />}
          {scenariosQuery.isError && <ErrorState message={scenariosQuery.error instanceof ApiError ? scenariosQuery.error.message : "Failed to load scenarios."} />}
          {!scenariosQuery.isLoading && scenarios.length === 0 && <EmptyState title="No scenarios yet" description="Create one above." />}
          {scenarios.length > 0 && (
            <ul className="mt-3 space-y-1.5">
              {scenarios.map((s) => (
                <li
                  key={s.id}
                  className={`flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm ${selectedScenarioId === s.id ? "border-violet-400 bg-violet-50/40" : "border-slate-200"}`}
                >
                  <div className="cursor-pointer" onClick={() => { setSelectedScenarioId(selectedScenarioId === s.id ? null : s.id); setSelectedRunId(null); }}>
                    <div className="flex items-center gap-2">
                      <ScenarioStatusBadge status={s.status} />
                      <span className="font-medium text-slate-900">{s.name}</span>
                      <span className="text-xs text-slate-400">{SCENARIO_TYPE_LABELS[s.scenario_type].split(" — ")[0]} · v{s.version}</span>
                    </div>
                    <p className="mt-0.5 text-xs text-slate-500">{new Date(s.baseline_window_start).toLocaleDateString()} – {new Date(s.baseline_window_end).toLocaleDateString()}</p>
                  </div>
                  {s.status !== "archived" && (
                    <button
                      onClick={() => archiveMutation.mutate(s.id)}
                      disabled={archiveMutation.isPending}
                      className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                    >
                      Archive
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* ------------------------------------------------------------ Selected scenario: validate / run / sweep */}
      {selectedScenario && (
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="font-semibold text-slate-900">{selectedScenario.name}</h3>
            <ScenarioStatusBadge status={selectedScenario.status} />
          </div>
          {selectedScenario.description && <p className="mt-1 text-sm text-slate-600">{selectedScenario.description}</p>}

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              onClick={() => validateMutation.mutate()}
              disabled={validateMutation.isPending || selectedScenario.status === "archived"}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              {validateMutation.isPending ? "Validating…" : "Validate scenario"}
            </button>

            <label className="flex items-center gap-1.5 text-xs text-slate-600">
              <input type="checkbox" checked={includeForecast} onChange={(e) => setIncludeForecast(e.target.checked)} />
              include forecast backtest comparison
            </label>
            <button
              onClick={() => runMutation.mutate()}
              disabled={!canRun || runMutation.isPending}
              className="rounded-md bg-violet-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-violet-700 disabled:opacity-50"
            >
              {runMutation.isPending ? "Running…" : "Run scenario"}
            </button>
            {!canRun && selectedScenario.status !== "archived" && <span className="text-xs text-slate-400">Validate first — a scenario with any error cannot be run.</span>}
          </div>
          {(validateMutation.isError || runMutation.isError) && (
            <p className="mt-2 text-sm text-red-700">
              {(validateMutation.error instanceof ApiError && validateMutation.error.message) || (runMutation.error instanceof ApiError && runMutation.error.message) || "Request failed."}
            </p>
          )}

          {/* Validation feedback — never silently clamps; shows every error and warning */}
          {selectedScenario.validation_result && (
            <div className="mt-3 rounded-lg border border-slate-200 p-3 text-sm">
              <p className="font-medium text-slate-900">{selectedScenario.validation_result.ok ? "Validation passed" : "Validation failed — this scenario cannot be run until every error below is resolved"}</p>
              {selectedScenario.validation_result.errors.length > 0 && (
                <ul className="mt-1 list-inside list-disc text-red-700">
                  {selectedScenario.validation_result.errors.map((e, i) => <li key={i}>{e}</li>)}
                </ul>
              )}
              {selectedScenario.validation_result.warnings.length > 0 && (
                <ul className="mt-1 list-inside list-disc text-amber-700">
                  {selectedScenario.validation_result.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              )}
            </div>
          )}

          {/* Sensitivity sweep */}
          {canSweep && (
            <div className="mt-4 rounded-lg border border-slate-200 p-3">
              <p className="text-sm font-medium text-slate-900">Sensitivity sweep</p>
              <p className="mt-1 text-xs text-slate-500">Re-runs the real pipeline at each magnitude below against the same real baseline data — up to 8 values, comma-separated. No trend is fit across the results.</p>
              <div className="mt-2 flex items-center gap-2">
                <input value={sweepMagnitudes} onChange={(e) => setSweepMagnitudes(e.target.value)} className="w-64 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
                <button onClick={() => sweepMutation.mutate()} disabled={sweepMutation.isPending} className="rounded-md border border-violet-300 bg-violet-50 px-3 py-1.5 text-sm font-medium text-violet-800 hover:bg-violet-100 disabled:opacity-50">
                  {sweepMutation.isPending ? "Sweeping…" : "Run sweep"}
                </button>
              </div>
              {sweepMutation.isError && <p className="mt-2 text-sm text-red-700">{sweepMutation.error instanceof ApiError ? sweepMutation.error.message : "Sweep failed."}</p>}
            </div>
          )}

          {/* Runs list */}
          <div className="mt-4">
            <p className="text-sm font-medium text-slate-900">Runs</p>
            {runsQuery.isLoading && <LoadingState label="Loading runs..." />}
            {!runsQuery.isLoading && runs.length === 0 && <p className="mt-1 text-xs text-slate-400">No runs yet.</p>}
            {runs.length > 0 && (
              <ul className="mt-2 space-y-1.5">
                {runs.map((r) => (
                  <li key={r.id} className={`flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-1.5 text-sm ${selectedRunId === r.id ? "border-violet-400 bg-violet-50/40" : "border-slate-200"}`}>
                    <div className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={compareRunIds.includes(r.id)}
                        disabled={r.status !== "succeeded"}
                        onChange={(e) => setCompareRunIds((cur) => (e.target.checked ? [...cur, r.id] : cur.filter((id) => id !== r.id)))}
                        title="select for multi-run comparison"
                      />
                      <RunStatusBadge status={r.status} />
                      <RunKindBadge kind={r.run_kind} />
                      <span className="text-xs text-slate-500">{new Date(r.created_at).toLocaleString()}</span>
                    </div>
                    <button onClick={() => setSelectedRunId(selectedRunId === r.id ? null : r.id)} disabled={r.status !== "succeeded"} className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40">
                      {selectedRunId === r.id ? "Close" : "Inspect"}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {compareRunIds.length >= 2 && (
              <div className="mt-2">
                <button onClick={() => compareMutation.mutate()} disabled={compareMutation.isPending} className="rounded-md border border-indigo-300 bg-indigo-50 px-3 py-1.5 text-xs font-medium text-indigo-800 hover:bg-indigo-100 disabled:opacity-50">
                  {compareMutation.isPending ? "Comparing…" : `Compare ${compareRunIds.length} selected runs`}
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ------------------------------------------------------------ Run detail */}
      {selectedRun && selectedRun.results && <RunResultPanel run={selectedRun} />}

      {/* ------------------------------------------------------------ Multi-run comparison */}
      {compareResult && (
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <h3 className="font-semibold text-slate-900">Multi-scenario comparison</h3>
          <p className="mt-1 text-xs text-slate-500">{compareResult.note}</p>
          <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
            {compareResult.runs.map((r) => (
              <div key={r.scenario_run_id} className="rounded-lg border border-violet-200 p-3">
                <p className="text-xs font-medium text-slate-500">run {r.scenario_run_id.slice(0, 8)} · {r.run_kind} · {new Date(r.created_at).toLocaleString()}</p>
                <ResultSummaryMini results={r.results} />
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Result rendering
// ---------------------------------------------------------------------------

function ValueSummaryTable({ diff }: { diff: unknown }) {
  const rows = ["mean", "median", "std", "min", "max"] as const;
  return (
    <table className="mt-2 w-full text-left text-xs">
      <thead className="text-slate-500"><tr><th className="pb-1 pr-2">Statistic</th><th className="pb-1 pr-2">Baseline</th><th className="pb-1 pr-2">Simulated</th><th className="pb-1 pr-2">Absolute diff</th><th className="pb-1">Relative diff</th></tr></thead>
      <tbody>
        {rows.map((key) => {
          const cell = get(diff, [key]) as Record<string, unknown> | undefined;
          return (
            <tr key={key} className="border-t border-slate-100">
              <td className="py-1 pr-2 capitalize">{key}</td>
              <td className="py-1 pr-2">{fmt(cell?.baseline)}</td>
              <td className="py-1 pr-2 font-medium text-violet-700">{fmt(cell?.simulated)}</td>
              <td className="py-1 pr-2">{fmt(cell?.absolute_diff)}</td>
              <td className="py-1">{fmt(cell?.relative_diff_pct, 1)}%</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function ComparisonBlock({ comparison }: { comparison: unknown }) {
  const valueDiff = get(comparison, ["value_summary_diff"]);
  const anomalyDiff = get(comparison, ["anomaly_detection_diff"]) as Record<string, unknown> | undefined;
  const conditionDiff = get(comparison, ["condition_intelligence_diff"]) as Record<string, unknown> | undefined;
  const changeDiff = get(comparison, ["change_candidates_diff"]) as Record<string, unknown> | undefined;
  const forecastDiff = get(comparison, ["forecast_backtest_diff"]) as Record<string, unknown> | undefined;

  return (
    <div className="space-y-3">
      <div>
        <p className="text-xs font-semibold text-slate-700">Value summary</p>
        <ValueSummaryTable diff={valueDiff} />
      </div>

      <div className="rounded-md border border-slate-200 p-2 text-xs">
        <p className="font-semibold text-slate-700">Anomaly detection</p>
        {anomalyDiff?.computed ? (
          <p className="mt-1 text-slate-700">{String(anomalyDiff.note ?? "")}</p>
        ) : (
          <p className="mt-1 text-slate-400">{String(anomalyDiff?.reason ?? "Not computed.")}</p>
        )}
      </div>

      <div className="rounded-md border border-slate-200 p-2 text-xs">
        <p className="font-semibold text-slate-700">Condition intelligence</p>
        {conditionDiff?.computed ? (
          <p className="mt-1 text-slate-700">
            Degradation stage: <span className="text-slate-500">{String(conditionDiff.baseline_stage ?? "none")}</span> → <span className="font-medium text-violet-700">{String(conditionDiff.simulated_stage ?? "none")}</span>
            {conditionDiff.stage_changed ? " (changed)" : " (unchanged)"}
          </p>
        ) : (
          <p className="mt-1 text-slate-400">{String(conditionDiff?.reason ?? "Not computed.")}</p>
        )}
      </div>

      <div className="rounded-md border border-slate-200 p-2 text-xs">
        <p className="font-semibold text-slate-700">Change candidates</p>
        <p className="mt-1 text-slate-700">
          {String(changeDiff?.baseline_candidate_count ?? 0)} baseline → <span className="font-medium text-violet-700">{String(changeDiff?.simulated_candidate_count ?? 0)}</span> simulated
          {" "}(delta {String(changeDiff?.candidate_count_delta ?? 0)})
        </p>
      </div>

      <div className="rounded-md border border-slate-200 p-2 text-xs">
        <p className="font-semibold text-slate-700">Forecast backtest</p>
        {forecastDiff?.computed ? (
          <p className="mt-1 text-slate-700">{String(forecastDiff.note ?? "")}</p>
        ) : (
          <p className="mt-1 text-slate-400">{String(forecastDiff?.reason ?? "Not requested/computed for this run.")}</p>
        )}
      </div>
    </div>
  );
}

function LimitationsPanel({ limitations }: { limitations: unknown }) {
  const items = Array.isArray(limitations) ? (limitations as string[]) : [];
  if (items.length === 0) return null;
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50/50 p-3">
      <p className="text-xs font-semibold text-amber-900">Limitations &amp; uncertainty (always shown)</p>
      <ul className="mt-1 list-inside list-disc text-xs text-amber-800">
        {items.map((l, i) => <li key={i}>{l}</li>)}
      </ul>
    </div>
  );
}

function ProvenancePanel({ provenance }: { provenance: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-slate-200 p-3">
      <button onClick={() => setOpen((v) => !v)} className="text-xs font-semibold text-slate-700">
        {open ? "Hide" : "Show"} provenance (exact windows, methods, and configuration used)
      </button>
      {open && <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-slate-50 p-2 text-[11px] text-slate-700">{JSON.stringify(provenance, null, 2)}</pre>}
    </div>
  );
}

function RunResultPanel({ run }: { run: ScenarioRunResponse }) {
  const results = run.results as Record<string, unknown>;
  const runType = results.run_type as string | undefined;

  return (
    <div className="rounded-xl border-2 border-violet-300 bg-white p-5">
      <SimulatedBanner />
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold text-slate-900">Result — run {run.id.slice(0, 8)}</h3>
        <span className="text-xs text-slate-500">{run.finished_at ? new Date(run.finished_at).toLocaleString() : "—"}</span>
      </div>

      {runType === "single" && (
        <div className="mt-4 space-y-4">
          <ComparisonBlock comparison={results.comparison} />
          <LimitationsPanel limitations={results.limitations} />
          <ProvenancePanel provenance={results.provenance} />
        </div>
      )}

      {runType === "multi_sensor" && (
        <div className="mt-4 space-y-4">
          {Object.entries((results.per_sensor as Record<string, Record<string, unknown>>) ?? {}).map(([sensorId, sub]) => (
            <div key={sensorId} className="rounded-lg border border-slate-200 p-3">
              <p className="text-xs font-semibold text-slate-700">Sensor {sensorId}</p>
              <div className="mt-2">
                <ComparisonBlock comparison={sub.comparison} />
              </div>
            </div>
          ))}
          <LimitationsPanel limitations={results.limitations} />
          <ProvenancePanel provenance={results.provenance} />
        </div>
      )}

      {runType === "sensitivity_sweep" && <SensitivitySweepPanel sensitivity={results.sensitivity} limitations={results.limitations} provenance={results.provenance} />}
    </div>
  );
}

function SensitivitySweepPanel({ sensitivity, limitations, provenance }: { sensitivity: unknown; limitations: unknown; provenance: unknown }) {
  const points = (get(sensitivity, ["points"]) as Array<Record<string, unknown>> | undefined) ?? [];
  const note = get(sensitivity, ["note"]) as string | undefined;

  const chartData = points.map((p) => {
    const magnitude = p.magnitude as number;
    const anomalyDiff = get(p.comparison, ["anomaly_detection_diff"]) as Record<string, unknown> | undefined;
    const valueDiff = get(p.comparison, ["value_summary_diff", "mean"]) as Record<string, unknown> | undefined;
    return {
      magnitude,
      simulated_candidate_count: typeof anomalyDiff?.simulated_candidate_count === "number" ? anomalyDiff.simulated_candidate_count : 0,
      mean_absolute_diff: typeof valueDiff?.absolute_diff === "number" ? valueDiff.absolute_diff : null,
    };
  });

  return (
    <div className="mt-4 space-y-4">
      <p className="text-xs text-slate-600">{note}</p>
      {chartData.length > 0 && (
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
            <XAxis dataKey="magnitude" stroke="#94a3b8" fontSize={11} label={{ value: "magnitude", position: "insideBottom", offset: -2, fontSize: 11 }} />
            <YAxis stroke="#94a3b8" fontSize={11} />
            <Tooltip />
            <Line type="monotone" dataKey="simulated_candidate_count" name="simulated anomaly candidates" stroke="#7c3aed" dot />
          </LineChart>
        </ResponsiveContainer>
      )}
      <table className="w-full text-left text-xs">
        <thead className="text-slate-500"><tr><th className="pb-1 pr-2">Magnitude</th><th className="pb-1 pr-2">Simulated anomaly candidates</th><th className="pb-1">Mean absolute diff</th></tr></thead>
        <tbody>
          {chartData.map((row, i) => (
            <tr key={i} className="border-t border-slate-100">
              <td className="py-1 pr-2">{row.magnitude}</td>
              <td className="py-1 pr-2 font-medium text-violet-700">{row.simulated_candidate_count}</td>
              <td className="py-1">{fmt(row.mean_absolute_diff)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <LimitationsPanel limitations={limitations} />
      <ProvenancePanel provenance={provenance} />
    </div>
  );
}

function ResultSummaryMini({ results }: { results: unknown }) {
  const runType = get(results, ["run_type"]) as string | undefined;
  if (runType === "single") {
    const anomalyDiff = get(results, ["comparison", "anomaly_detection_diff"]) as Record<string, unknown> | undefined;
    const conditionDiff = get(results, ["comparison", "condition_intelligence_diff"]) as Record<string, unknown> | undefined;
    return (
      <div className="mt-2 text-xs text-slate-700">
        <p>Simulated anomaly candidates: <span className="font-medium text-violet-700">{String(anomalyDiff?.simulated_candidate_count ?? "n/a")}</span></p>
        <p>Simulated degradation stage: <span className="font-medium text-violet-700">{String(conditionDiff?.simulated_stage ?? "n/a")}</span></p>
      </div>
    );
  }
  return <p className="mt-2 text-xs text-slate-400">{runType ?? "unknown run type"}</p>;
}
