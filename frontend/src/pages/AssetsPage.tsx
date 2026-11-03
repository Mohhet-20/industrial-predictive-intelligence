import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "@/api/client";
import type { AssetResponse, Page } from "@/api/types";
import { EmptyState, ErrorState, LoadingState } from "@/components/StateViews";

export function AssetsPage() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["assets"],
    queryFn: () => api.get<Page<AssetResponse>>("/assets", { limit: 200 }),
  });

  if (isLoading) return <LoadingState label="Loading assets..." />;
  if (isError) return <ErrorState message={(error as Error).message} />;

  const assets = data?.items ?? [];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Assets</h1>
        <p className="mt-1 text-sm text-slate-500">Equipment registered in this organization.</p>
      </div>

      {assets.length === 0 ? (
        <EmptyState
          title="No assets yet"
          description="Load the synthetic demo dataset or create an asset via POST /api/v1/assets."
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {assets.map((asset) => (
            <Link
              key={asset.id}
              to={`/assets/${asset.id}`}
              className="rounded-xl border border-slate-200 bg-white p-5 transition hover:border-brand-300 hover:shadow-sm"
            >
              <p className="text-xs uppercase tracking-wide text-slate-400">{asset.asset_type}</p>
              <h2 className="mt-1 font-semibold text-slate-900">{asset.name}</h2>
              <p className="text-sm text-slate-500">{asset.code}</p>
              {asset.location && <p className="mt-2 text-xs text-slate-400">{asset.location}</p>}
              <p className="mt-3 text-sm text-brand-700">{asset.sensor_count} sensor{asset.sensor_count === 1 ? "" : "s"}</p>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
