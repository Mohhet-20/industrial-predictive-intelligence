import { useQueries, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useSearchParams } from "react-router-dom";

import { api } from "@/api/client";
import type { AssetResponse, MeasurementResponse, Page, SensorResponse, SensorSummaryStats } from "@/api/types";
import { EmptyState, ErrorState, LoadingState } from "@/components/StateViews";

const COLORS = ["#2563eb", "#059669", "#d97706", "#dc2626", "#7c3aed"];

function isoLocal(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function DataExplorerPage() {
  const [searchParams] = useSearchParams();
  const preselectedSensorId = searchParams.get("sensorId");

  const [assetId, setAssetId] = useState<string>("");
  const [selectedSensorIds, setSelectedSensorIds] = useState<string[]>([]);
  const now = useMemo(() => new Date(), []);
  const [startTime, setStartTime] = useState(isoLocal(new Date(now.getTime() - 24 * 3600 * 1000)));
  const [endTime, setEndTime] = useState(isoLocal(now));

  const assetsQuery = useQuery({
    queryKey: ["assets", "explorer"],
    queryFn: () => api.get<Page<AssetResponse>>("/assets", { limit: 200 }),
  });

  const sensorsQuery = useQuery({
    queryKey: ["assets", assetId, "sensors", "explorer"],
    queryFn: () => api.get<Page<SensorResponse>>(`/assets/${assetId}/sensors`, { limit: 100 }),
    enabled: !!assetId,
  });

  // Resolve a sensorId passed via the URL (e.g. from the Asset Detail page) into its parent asset once assets load.
  useEffect(() => {
    if (!preselectedSensorId || !assetsQuery.data) return;
    (async () => {
      for (const asset of assetsQuery.data!.items) {
        const sensors = await api.get<Page<SensorResponse>>(`/assets/${asset.id}/sensors`, { limit: 100 });
        if (sensors.items.some((s) => s.id === preselectedSensorId)) {
          setAssetId(asset.id);
          setSelectedSensorIds([preselectedSensorId]);
          break;
        }
      }
    })();
  }, [preselectedSensorId, assetsQuery.data]);

  const sensors = sensorsQuery.data?.items ?? [];

  const measurementsQuery = useQuery({
    queryKey: ["measurements", selectedSensorIds, startTime, endTime],
    queryFn: () =>
      api.get<Page<MeasurementResponse>>("/measurements", {
        sensor_ids: selectedSensorIds,
        start_time: new Date(startTime).toISOString(),
        end_time: new Date(endTime).toISOString(),
        limit: 20000,
      }),
    enabled: selectedSensorIds.length > 0,
  });

  // useQueries (not a loop of useQuery) so the number of hooks stays stable
  // across renders even as the sensor selection changes — calling useQuery
  // inside Array.map would violate the rules of hooks.
  const statsQueries = useQueries({
    queries: selectedSensorIds.map((sensorId) => ({
      queryKey: ["stats", sensorId, startTime, endTime],
      queryFn: () =>
        api.get<SensorSummaryStats>(`/stats/sensors/${sensorId}`, {
          start_time: new Date(startTime).toISOString(),
          end_time: new Date(endTime).toISOString(),
        }),
    })),
  });

  function toggleSensor(sensorId: string) {
    setSelectedSensorIds((prev) => (prev.includes(sensorId) ? prev.filter((id) => id !== sensorId) : [...prev, sensorId]));
  }

  const chartData = useMemo(() => {
    const items = measurementsQuery.data?.items ?? [];
    const bySensor = new Map<string, MeasurementResponse[]>();
    for (const m of items) {
      if (!bySensor.has(m.sensor_id)) bySensor.set(m.sensor_id, []);
      bySensor.get(m.sensor_id)!.push(m);
    }
    return bySensor;
  }, [measurementsQuery.data]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Data Explorer</h1>
        <p className="mt-1 text-sm text-slate-500">
          Select an asset, one or more sensors, and a time range to inspect raw measurements.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 rounded-xl border border-slate-200 bg-white p-5 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label className="block text-xs font-medium text-slate-500">Asset</label>
          <select
            value={assetId}
            onChange={(e) => {
              setAssetId(e.target.value);
              setSelectedSensorIds([]);
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
          <label className="block text-xs font-medium text-slate-500">Start</label>
          <input
            type="datetime-local"
            value={startTime}
            onChange={(e) => setStartTime(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-500">End</label>
          <input
            type="datetime-local"
            value={endTime}
            onChange={(e) => setEndTime(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-500">Sensors</label>
          <div className="mt-1 max-h-24 space-y-1 overflow-y-auto rounded-md border border-slate-200 p-2">
            {sensors.length === 0 && <p className="text-xs text-slate-400">Select an asset first</p>}
            {sensors.map((s) => (
              <label key={s.id} className="flex items-center gap-2 text-xs text-slate-700">
                <input type="checkbox" checked={selectedSensorIds.includes(s.id)} onChange={() => toggleSensor(s.id)} />
                {s.name} ({s.unit})
              </label>
            ))}
          </div>
        </div>
      </div>

      {selectedSensorIds.length === 0 && (
        <EmptyState title="No sensors selected" description="Pick an asset and at least one sensor to explore its data." />
      )}

      {measurementsQuery.isLoading && selectedSensorIds.length > 0 && <LoadingState label="Loading measurements..." />}
      {measurementsQuery.isError && <ErrorState message={(measurementsQuery.error as Error).message} />}

      {selectedSensorIds.map((sensorId, idx) => {
        const sensor = sensors.find((s) => s.id === sensorId);
        const points = chartData.get(sensorId) ?? [];
        const stats = statsQueries[idx]?.data;
        const expectedInterval = sensor?.expected_interval_seconds;
        const rangeSeconds = (new Date(endTime).getTime() - new Date(startTime).getTime()) / 1000;
        const expectedCount = expectedInterval ? Math.floor(rangeSeconds / expectedInterval) : null;

        return (
          <div key={sensorId} className="rounded-xl border border-slate-200 bg-white p-5">
            <div className="mb-3 flex items-center justify-between">
              <div>
                <h3 className="font-semibold text-slate-900">{sensor?.name ?? sensorId}</h3>
                <p className="text-xs text-slate-500">
                  {points.length} readings shown
                  {expectedCount !== null && (
                    <> · ~{expectedCount} expected at {expectedInterval}s cadence — {Math.max(expectedCount - points.length, 0)} apparently missing</>
                  )}
                </p>
              </div>
              {stats && (
                <div className="text-right text-xs text-slate-500">
                  <p>min {stats.min_value?.toFixed(2)} · max {stats.max_value?.toFixed(2)}</p>
                  <p>mean {stats.mean_value?.toFixed(2)} · σ {stats.std_dev?.toFixed(2)}</p>
                </div>
              )}
            </div>
            {points.length === 0 ? (
              <EmptyState title="No measurements in this range" description="Try widening the time range or picking a different sensor." />
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={points.map((p) => ({ timestamp: new Date(p.timestamp).getTime(), value: p.value }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis
                    dataKey="timestamp"
                    type="number"
                    domain={["dataMin", "dataMax"]}
                    tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                    stroke="#94a3b8"
                    fontSize={11}
                  />
                  <YAxis stroke="#94a3b8" fontSize={11} unit={sensor ? ` ${sensor.unit}` : ""} />
                  <Tooltip labelFormatter={(t) => new Date(t as number).toLocaleString()} />
                  <Legend />
                  <Line
                    type="monotone"
                    dataKey="value"
                    name={sensor?.name ?? "value"}
                    stroke={COLORS[idx % COLORS.length]}
                    dot={false}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            )}
          </div>
        );
      })}
    </div>
  );
}
