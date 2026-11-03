import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api, ApiError } from "@/api/client";
import type {
  AnalysisEnvelope,
  AssetResponse,
  ChangeCandidatesResult,
  CorrelationResult,
  FeatureKind,
  FeaturePreviewResponse,
  Page,
  SensorProfile,
  SensorResponse,
} from "@/api/types";
import { WINDOWED_FEATURE_KINDS } from "@/api/types";
import { EmptyState, ErrorState, LoadingState } from "@/components/StateViews";

function isoLocal(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fmt(n: number | null | undefined, digits = 2): string {
  return n === null || n === undefined ? "—" : n.toFixed(digits);
}

const FEATURE_KINDS: FeatureKind[] = [
  "mean",
  "median",
  "std",
  "variance",
  "min",
  "max",
  "quantile",
  "iqr",
  "lag",
  "rolling_mean",
  "rolling_std",
  "rate_of_change",
  "diff",
  "ema",
  "skewness",
  "kurtosis",
];

export function TemporalAnalysisPage() {
  const now = useMemo(() => new Date(), []);
  const [assetId, setAssetId] = useState("");
  const [sensorId, setSensorId] = useState("");
  const [compareSensorId, setCompareSensorId] = useState("");
  const [startTime, setStartTime] = useState(isoLocal(new Date(now.getTime() - 7 * 24 * 3600 * 1000)));
  const [endTime, setEndTime] = useState(isoLocal(now));

  const [featureKind, setFeatureKind] = useState<FeatureKind>("mean");
  const [featureWindowMinutes, setFeatureWindowMinutes] = useState(60);
  const [featureAsOf, setFeatureAsOf] = useState(isoLocal(now));

  const startIso = new Date(startTime).toISOString();
  const endIso = new Date(endTime).toISOString();

  const assetsQuery = useQuery({
    queryKey: ["assets", "temporal"],
    queryFn: () => api.get<Page<AssetResponse>>("/assets", { limit: 200 }),
  });

  const sensorsQuery = useQuery({
    queryKey: ["assets", assetId, "sensors", "temporal"],
    queryFn: () => api.get<Page<SensorResponse>>(`/assets/${assetId}/sensors`, { limit: 100 }),
    enabled: !!assetId,
  });
  const sensors = sensorsQuery.data?.items ?? [];
  const sensor = sensors.find((s) => s.id === sensorId);

  const profileQuery = useQuery({
    queryKey: ["timeseries", "profile", sensorId, startIso, endIso],
    queryFn: () =>
      api.get<AnalysisEnvelope<SensorProfile>>(`/timeseries/sensors/${sensorId}/profile`, { start: startIso, end: endIso }),
    enabled: !!sensorId,
  });
  const profile = profileQuery.data?.data;

  const changeCandidatesQuery = useQuery({
    queryKey: ["timeseries", "change-candidates", sensorId, startIso, endIso],
    queryFn: () =>
      api.get<AnalysisEnvelope<ChangeCandidatesResult>>(`/timeseries/sensors/${sensorId}/change-candidates`, {
        start: startIso,
        end: endIso,
      }),
    enabled: !!sensorId,
  });
  const changeCandidates = changeCandidatesQuery.data?.data;

  const correlationQuery = useQuery({
    queryKey: ["timeseries", "correlation", sensorId, compareSensorId, startIso, endIso],
    queryFn: () =>
      api.get<AnalysisEnvelope<CorrelationResult>>("/timeseries/correlation", {
        sensor_ids: [sensorId, compareSensorId],
        start: startIso,
        end: endIso,
        freq_seconds: 300,
      }),
    enabled: !!sensorId && !!compareSensorId && sensorId !== compareSensorId,
  });
  const correlation = correlationQuery.data?.data;

  const previewMutation = useMutation({
    mutationFn: () => {
      const isWindowed = WINDOWED_FEATURE_KINDS.includes(featureKind);
      return api.post<FeaturePreviewResponse>("/timeseries/feature-preview", {
        as_of: new Date(featureAsOf).toISOString(),
        specs: [
          {
            feature_name: featureKind,
            sensor_id: sensorId,
            kind: featureKind,
            window_seconds: isWindowed ? featureWindowMinutes * 60 : null,
            params: featureKind === "quantile" ? { q: 0.5 } : featureKind === "lag" || featureKind === "diff" ? { lag_steps: 1 } : {},
          },
        ],
      });
    },
  });

  const rollingChartData = (profile?.rolling_behavior?.points ?? []).map((p) => ({
    timestamp: new Date(p.timestamp).getTime(),
    rolling_mean: p.rolling_mean,
    rolling_std: p.rolling_std,
  }));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Temporal Analysis</h1>
        <p className="mt-1 text-sm text-slate-500">
          Explore a sensor's behavior over time: trend, rolling statistics, change candidates, and feature previews —
          every number here says what it computed and how much data backed it, not just a value.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 rounded-xl border border-slate-200 bg-white p-5 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label htmlFor="temporal-analysis-asset" className="block text-xs font-medium text-slate-500">Asset</label>
          <select id="temporal-analysis-asset"
            value={assetId}
            onChange={(e) => {
              setAssetId(e.target.value);
              setSensorId("");
              setCompareSensorId("");
            }}
            className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          >
            <option value="">Select an asset…</option>
            {(assetsQuery.data?.items ?? []).map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="temporal-analysis-sensor" className="block text-xs font-medium text-slate-500">Sensor</label>
          <select id="temporal-analysis-sensor"
            value={sensorId}
            onChange={(e) => setSensorId(e.target.value)}
            disabled={!assetId}
            className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm disabled:bg-slate-50"
          >
            <option value="">Select a sensor…</option>
            {sensors.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({s.unit})
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="temporal-analysis-start" className="block text-xs font-medium text-slate-500">Start</label>
          <input id="temporal-analysis-start"
            type="datetime-local"
            value={startTime}
            onChange={(e) => setStartTime(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          <label htmlFor="temporal-analysis-end" className="block text-xs font-medium text-slate-500">End</label>
          <input id="temporal-analysis-end"
            type="datetime-local"
            value={endTime}
            onChange={(e) => setEndTime(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </div>
      </div>

      {!sensorId && <EmptyState title="No sensor selected" description="Pick an asset and a sensor to see its temporal profile." />}

      {sensorId && profileQuery.isLoading && <LoadingState label="Computing profile..." />}
      {sensorId && profileQuery.isError && (
        <ErrorState message={profileQuery.error instanceof ApiError ? profileQuery.error.message : "Failed to load profile."} />
      )}

      {profile && (
        <>
          {profile.insufficient_data ? (
            <EmptyState title="Not enough data for a profile" description={profile.insufficient_data_reason ?? undefined} />
          ) : (
            <>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <StatCard title="Observations" value={String(profile.observation_count)} sub={sensor ? `${sensor.unit}` : undefined} />
                <StatCard
                  title="Sampling interval"
                  value={profile.sampling_interval?.median_observed_seconds != null ? `${profile.sampling_interval.median_observed_seconds}s` : "—"}
                  sub="median observed"
                />
                <StatCard
                  title="Data gaps"
                  value={String(profile.gap_count)}
                  sub={profile.gaps[0] ? `longest ${(profile.gaps[0].duration_seconds / 3600).toFixed(1)}h` : undefined}
                />
                <StatCard
                  title="Missing"
                  value={profile.missingness?.missing_fraction != null ? `${(profile.missingness.missing_fraction * 100).toFixed(0)}%` : "n/a"}
                  sub={profile.missingness ? `${profile.missingness.missing_count} of ~${profile.missingness.expected_count}` : "no declared cadence"}
                />
              </div>

              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                <div className="rounded-xl border border-slate-200 bg-white p-5">
                  <h3 className="font-semibold text-slate-900">Descriptive statistics</h3>
                  {profile.descriptive_stats ? (
                    <dl className="mt-3 grid grid-cols-3 gap-y-2 text-sm">
                      <DL label="Min" value={fmt(profile.descriptive_stats.min)} />
                      <DL label="Max" value={fmt(profile.descriptive_stats.max)} />
                      <DL label="Mean" value={fmt(profile.descriptive_stats.mean)} />
                      <DL label="Median" value={fmt(profile.descriptive_stats.median)} />
                      <DL label="Std dev" value={fmt(profile.descriptive_stats.std)} />
                      <DL label="p90" value={fmt(profile.descriptive_stats.quantiles?.p90)} />
                    </dl>
                  ) : (
                    <p className="mt-2 text-sm text-slate-500">Not enough data.</p>
                  )}
                  {profile.distribution && (
                    <p className="mt-3 text-xs text-slate-500">
                      {profile.distribution.skewness != null
                        ? `Skewness ${fmt(profile.distribution.skewness, 2)}, kurtosis ${fmt(profile.distribution.kurtosis, 2)}.`
                        : profile.distribution.note}
                    </p>
                  )}
                </div>

                <div className="rounded-xl border border-slate-200 bg-white p-5">
                  <h3 className="font-semibold text-slate-900">Trend</h3>
                  {profile.trend?.direction ? (
                    <div className="mt-2 text-sm text-slate-700">
                      <p>
                        <span className="font-medium capitalize">{profile.trend.direction.replace(/_/g, " ")}</span>{" "}
                        ({profile.trend.strength} strength) — {fmt(profile.trend.slope_per_hour, 4)} {sensor?.unit ?? ""}/hour, R² ={" "}
                        {fmt(profile.trend.r_squared, 2)}.
                      </p>
                      <p className="mt-2 text-xs text-slate-500">{profile.trend.note}</p>
                    </div>
                  ) : (
                    <p className="mt-2 text-sm text-slate-500">{profile.trend?.note ?? "Not enough data for a trend."}</p>
                  )}
                  {profile.change_rate && (
                    <p className="mt-3 text-xs text-slate-500">
                      Typical step change: {fmt(profile.change_rate.mean_absolute_step_change, 3)} {sensor?.unit ?? ""} between
                      consecutive readings.
                    </p>
                  )}
                </div>
              </div>

              {profile.rolling_behavior && rollingChartData.length > 0 && (
                <div className="rounded-xl border border-slate-200 bg-white p-5">
                  <h3 className="font-semibold text-slate-900">Rolling mean &amp; std dev</h3>
                  <p className="mt-1 text-xs text-slate-500">
                    Window of {profile.rolling_behavior.window_observation_count} observations, computed over the requested range.
                  </p>
                  <ResponsiveContainer width="100%" height={220}>
                    <LineChart data={rollingChartData}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                      <XAxis
                        dataKey="timestamp"
                        type="number"
                        domain={["dataMin", "dataMax"]}
                        tickFormatter={(t) => new Date(t).toLocaleDateString([], { month: "short", day: "numeric" })}
                        stroke="#94a3b8"
                        fontSize={11}
                      />
                      <YAxis stroke="#94a3b8" fontSize={11} />
                      <Tooltip labelFormatter={(t) => new Date(t as number).toLocaleString()} />
                      <Legend />
                      <Line type="monotone" dataKey="rolling_mean" name="rolling mean" stroke="#2563eb" dot={false} isAnimationActive={false} />
                      <Line type="monotone" dataKey="rolling_std" name="rolling std dev" stroke="#d97706" dot={false} isAnimationActive={false} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}

              {profile.data_quality_warnings.length > 0 && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
                  <p className="font-medium">Data quality notes</p>
                  <ul className="mt-1 list-inside list-disc space-y-0.5">
                    {profile.data_quality_warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}

          <div className="rounded-xl border border-slate-200 bg-white p-5">
            <h3 className="font-semibold text-slate-900">Change candidates</h3>
            <p className="mt-1 text-xs text-slate-500">
              Statistical candidates for human review — not confirmed anomalies or fault diagnoses. See{" "}
              {changeCandidates?.methodology ?? "windowed mean/variance comparison"}.
            </p>
            {changeCandidatesQuery.isLoading && <LoadingState label="Scanning for change candidates..." />}
            {changeCandidates && changeCandidates.candidates.length === 0 && (
              <p className="mt-3 text-sm text-slate-500">{changeCandidates.note}</p>
            )}
            {changeCandidates && changeCandidates.candidates.length > 0 && (
              <ul className="mt-3 space-y-2">
                {changeCandidates.candidates.map((c, i) => (
                  <li key={i} className="rounded-lg border border-slate-200 px-3 py-2 text-sm">
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-slate-900">
                        {c.type === "level_shift" ? "Level shift" : "Variance shift"} candidate
                      </span>
                      <StrengthBadge strength={c.strength} />
                    </div>
                    <p className="mt-1 text-xs text-slate-500">{new Date(c.timestamp).toLocaleString()}</p>
                    <p className="mt-1 text-slate-600">{c.description}</p>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="rounded-xl border border-slate-200 bg-white p-5">
            <h3 className="font-semibold text-slate-900">Feature preview</h3>
            <p className="mt-1 text-xs text-slate-500">
              Compute one feature value right now, causally filtered to observations at or before "as of" — the same
              computation a training dataset row would use.
            </p>
            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-4">
              <div>
                <label htmlFor="temporal-analysis-kind" className="block text-xs font-medium text-slate-500">Kind</label>
                <select id="temporal-analysis-kind"
                  value={featureKind}
                  onChange={(e) => setFeatureKind(e.target.value as FeatureKind)}
                  className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                >
                  {FEATURE_KINDS.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
              </div>
              {WINDOWED_FEATURE_KINDS.includes(featureKind) && (
                <div>
                  <label htmlFor="temporal-analysis-window-minutes" className="block text-xs font-medium text-slate-500">Window (minutes)</label>
                  <input id="temporal-analysis-window-minutes"
                    type="number"
                    min={1}
                    value={featureWindowMinutes}
                    onChange={(e) => setFeatureWindowMinutes(Number(e.target.value))}
                    className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </div>
              )}
              <div>
                <label htmlFor="temporal-analysis-as-of" className="block text-xs font-medium text-slate-500">As of</label>
                <input id="temporal-analysis-as-of"
                  type="datetime-local"
                  value={featureAsOf}
                  onChange={(e) => setFeatureAsOf(e.target.value)}
                  className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </div>
              <div className="flex items-end">
                <button
                  onClick={() => previewMutation.mutate()}
                  disabled={previewMutation.isPending}
                  className="w-full rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50"
                >
                  {previewMutation.isPending ? "Computing…" : "Preview"}
                </button>
              </div>
            </div>
            {previewMutation.isError && (
              <p className="mt-3 text-sm text-red-700">
                {previewMutation.error instanceof ApiError ? previewMutation.error.message : "Failed to compute feature."}
              </p>
            )}
            {previewMutation.data && (
              <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm">
                {previewMutation.data.results.map((r) => (
                  <div key={r.feature_name}>
                    {r.result.computed ? (
                      <p>
                        <span className="font-medium">{r.result.kind}</span> = {fmt(r.result.value, 4)} (using{" "}
                        {r.result.observation_count_used} observation{r.result.observation_count_used === 1 ? "" : "s"}
                        {r.result.window_start ? ` from ${new Date(r.result.window_start).toLocaleString()}` : ""} through{" "}
                        {r.result.window_end ? new Date(r.result.window_end).toLocaleString() : "as of"})
                      </p>
                    ) : (
                      <p className="text-slate-500">Not computed: {r.result.reason}</p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="rounded-xl border border-slate-200 bg-white p-5">
            <h3 className="font-semibold text-slate-900">Multi-sensor correlation</h3>
            <p className="mt-1 text-xs text-slate-500">
              Compares this sensor to another on the same asset over the selected range. Association only — never causation.
            </p>
            <div className="mt-3 max-w-xs">
              <label htmlFor="temporal-analysis-compare-against" className="block text-xs font-medium text-slate-500">Compare against</label>
              <select id="temporal-analysis-compare-against"
                value={compareSensorId}
                onChange={(e) => setCompareSensorId(e.target.value)}
                className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              >
                <option value="">Select a second sensor…</option>
                {sensors
                  .filter((s) => s.id !== sensorId)
                  .map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} ({s.unit})
                    </option>
                  ))}
              </select>
            </div>
            {correlationQuery.isLoading && <LoadingState label="Computing correlation..." />}
            {correlation && correlation.pairs[0] && (
              <div className="mt-3 text-sm">
                {correlation.pairs[0].correlation !== null ? (
                  <p>
                    Pearson correlation: <span className="font-medium">{fmt(correlation.pairs[0].correlation, 3)}</span> over{" "}
                    {correlation.pairs[0].overlapping_observations} overlapping buckets.
                  </p>
                ) : (
                  <p className="text-slate-500">{correlation.pairs[0].note}</p>
                )}
                <p className="mt-2 text-xs text-slate-500">{correlation.caveat}</p>
              </div>
            )}
          </div>
        </>
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

function DL({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-slate-400">{label}</dt>
      <dd className="font-medium text-slate-800">{value}</dd>
    </div>
  );
}

function StrengthBadge({ strength }: { strength: "weak" | "moderate" | "strong" }) {
  const styles: Record<string, string> = {
    weak: "bg-slate-100 text-slate-700",
    moderate: "bg-amber-100 text-amber-800",
    strong: "bg-red-100 text-red-800",
  };
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${styles[strength]}`}>{strength}</span>;
}
