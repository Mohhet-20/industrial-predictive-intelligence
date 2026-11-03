import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "@/api/client";
import type { AssetResponse, IngestionBatchResponse, Page } from "@/api/types";
import { ErrorState, LoadingState } from "@/components/StateViews";

function StatCard({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5">
      <p className="text-sm text-slate-500">{label}</p>
      <p className="mt-2 text-2xl font-semibold text-slate-900">{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
    </div>
  );
}

export function OverviewPage() {
  const assetsQuery = useQuery({
    queryKey: ["assets", "overview"],
    queryFn: () => api.get<Page<AssetResponse>>("/assets", { limit: 200 }),
  });
  const batchesQuery = useQuery({
    queryKey: ["ingestion-batches", "recent"],
    queryFn: () => api.get<Page<IngestionBatchResponse>>("/ingestion/batches", { limit: 5 }),
  });

  if (assetsQuery.isLoading || batchesQuery.isLoading) return <LoadingState label="Loading platform overview..." />;
  if (assetsQuery.isError) return <ErrorState message={(assetsQuery.error as Error).message} />;

  const assets = assetsQuery.data?.items ?? [];
  const totalSensors = assets.reduce((sum, a) => sum + a.sensor_count, 0);
  const batches = batchesQuery.data?.items ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Platform Overview</h1>
        <p className="mt-1 text-sm text-slate-500">
          A snapshot of what's actually in the system — no projected or estimated figures.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatCard label="Assets monitored" value={assets.length} />
        <StatCard label="Sensors configured" value={totalSensors} />
        <StatCard label="Ingestion batches" value={batchesQuery.data?.total ?? 0} />
      </div>

      <div className="rounded-xl border border-slate-200 bg-white">
        <div className="border-b border-slate-200 px-5 py-3">
          <h2 className="text-sm font-semibold text-slate-900">Recent ingestion activity</h2>
        </div>
        {batches.length === 0 ? (
          <p className="px-5 py-6 text-sm text-slate-500">
            No data has been ingested yet. Run the synthetic data generator (see SETUP.md) or POST to
            /api/v1/ingestion/measurements to get started.
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-slate-500">
              <tr className="border-b border-slate-100">
                <th className="px-5 py-2 font-medium">Status</th>
                <th className="px-5 py-2 font-medium">Accepted</th>
                <th className="px-5 py-2 font-medium">Rejected</th>
                <th className="px-5 py-2 font-medium">Duplicate</th>
                <th className="px-5 py-2 font-medium">Created</th>
              </tr>
            </thead>
            <tbody>
              {batches.map((b) => (
                <tr key={b.id} className="border-b border-slate-50 last:border-0">
                  <td className="px-5 py-2">{b.status}</td>
                  <td className="px-5 py-2">{b.records_accepted}</td>
                  <td className="px-5 py-2">{b.records_rejected}</td>
                  <td className="px-5 py-2">{b.records_duplicate}</td>
                  <td className="px-5 py-2 text-slate-500">{new Date(b.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold text-slate-900">Assets</h2>
        <ul className="mt-3 divide-y divide-slate-100">
          {assets.map((asset) => (
            <li key={asset.id} className="flex items-center justify-between py-2">
              <Link to={`/assets/${asset.id}`} className="text-brand-700 hover:underline">
                {asset.name} <span className="text-slate-400">({asset.code})</span>
              </Link>
              <span className="text-xs text-slate-500">{asset.sensor_count} sensors</span>
            </li>
          ))}
          {assets.length === 0 && <p className="py-2 text-sm text-slate-500">No assets registered yet.</p>}
        </ul>
      </div>
    </div>
  );
}
