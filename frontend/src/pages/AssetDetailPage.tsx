import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { api } from "@/api/client";
import type { AssetResponse, DataQualitySummary, Page, SensorResponse } from "@/api/types";
import { CopilotPanel } from "@/components/CopilotPanel";
import { EmptyState, ErrorState, LoadingState } from "@/components/StateViews";

export function AssetDetailPage() {
  const { assetId } = useParams<{ assetId: string }>();

  const assetQuery = useQuery({
    queryKey: ["asset", assetId],
    queryFn: () => api.get<AssetResponse>(`/assets/${assetId}`),
    enabled: !!assetId,
  });
  const sensorsQuery = useQuery({
    queryKey: ["asset", assetId, "sensors"],
    queryFn: () => api.get<Page<SensorResponse>>(`/assets/${assetId}/sensors`, { limit: 100 }),
    enabled: !!assetId,
  });
  const qualityQuery = useQuery({
    queryKey: ["asset", assetId, "quality-summary"],
    queryFn: () => api.get<DataQualitySummary>(`/data-quality/summary/${assetId}`),
    enabled: !!assetId,
  });

  if (assetQuery.isLoading || sensorsQuery.isLoading) return <LoadingState label="Loading asset..." />;
  if (assetQuery.isError) return <ErrorState message={(assetQuery.error as Error).message} />;

  const asset = assetQuery.data!;
  const sensors = sensorsQuery.data?.items ?? [];
  const quality = qualityQuery.data;

  return (
    <div className="space-y-6">
      <div>
        <p className="text-xs uppercase tracking-wide text-slate-400">{asset.asset_type}</p>
        <h1 className="text-xl font-semibold text-slate-900">{asset.name}</h1>
        <p className="text-sm text-slate-500">
          {asset.code} {asset.location && `· ${asset.location}`}
        </p>
        {asset.description && <p className="mt-2 max-w-2xl text-sm text-slate-600">{asset.description}</p>}
      </div>

      {quality && (
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <h2 className="text-sm font-semibold text-slate-900">Data quality</h2>
          <div className="mt-3 flex gap-6 text-sm">
            <span className="text-slate-600">Total issues: <strong>{quality.total_issues}</strong></span>
            <span className="text-red-700">Critical: <strong>{quality.critical_count}</strong></span>
            <span className="text-amber-700">Warning: <strong>{quality.warning_count}</strong></span>
            <span className="text-slate-500">Info: <strong>{quality.info_count}</strong></span>
          </div>
          <Link to="/data-quality" className="mt-3 inline-block text-sm text-brand-700 hover:underline">
            View all data-quality issues →
          </Link>
        </div>
      )}

      <div>
        <h2 className="mb-2 text-sm font-semibold text-slate-900">Sensors</h2>
        {sensors.length === 0 ? (
          <EmptyState title="No sensors configured for this asset" />
        ) : (
          <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-slate-500">
                <tr>
                  <th className="px-4 py-2 font-medium">Sensor</th>
                  <th className="px-4 py-2 font-medium">Type</th>
                  <th className="px-4 py-2 font-medium">Unit</th>
                  <th className="px-4 py-2 font-medium">Expected range</th>
                  <th className="px-4 py-2 font-medium">Sampling interval</th>
                </tr>
              </thead>
              <tbody>
                {sensors.map((s) => (
                  <tr key={s.id} className="border-t border-slate-100">
                    <td className="px-4 py-2">
                      <Link to={`/explorer?sensorId=${s.id}`} className="text-brand-700 hover:underline">
                        {s.name}
                      </Link>
                      <span className="ml-1 text-xs text-slate-400">({s.code})</span>
                    </td>
                    <td className="px-4 py-2 text-slate-600">{s.measurement_type}</td>
                    <td className="px-4 py-2 text-slate-600">{s.unit}</td>
                    <td className="px-4 py-2 text-slate-600">
                      {s.expected_min !== null && s.expected_max !== null ? `${s.expected_min} – ${s.expected_max}` : "not set"}
                    </td>
                    <td className="px-4 py-2 text-slate-600">
                      {s.expected_interval_seconds ? `${s.expected_interval_seconds}s` : "not set"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <CopilotPanel contextType="asset" contextId={asset.id} contextLabel={`${asset.code} (${asset.name})`} />
    </div>
  );
}
