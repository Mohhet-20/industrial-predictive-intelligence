import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/api/client";
import type { AssetResponse, DataQualityIssueResponse, DataQualityIssueSeverity, Page } from "@/api/types";
import { EmptyState, ErrorState, LoadingState, SeverityBadge } from "@/components/StateViews";

const ISSUE_TYPE_LABELS: Record<string, string> = {
  missing_value: "Missing value",
  out_of_range: "Out of range",
  duplicate_observation: "Duplicate observation",
  timestamp_out_of_order: "Timestamp out of order",
  timestamp_future: "Future timestamp",
  data_gap: "Data gap",
  unsupported_unit: "Unsupported unit",
  malformed_record: "Malformed record",
};

export function DataQualityPage() {
  const [assetId, setAssetId] = useState<string>("");
  const [severity, setSeverity] = useState<DataQualityIssueSeverity | "">("");

  const assetsQuery = useQuery({
    queryKey: ["assets", "quality-filter"],
    queryFn: () => api.get<Page<AssetResponse>>("/assets", { limit: 200 }),
  });

  const issuesQuery = useQuery({
    queryKey: ["data-quality-issues", assetId, severity],
    queryFn: () =>
      api.get<Page<DataQualityIssueResponse>>("/data-quality/issues", {
        asset_id: assetId || undefined,
        severity: severity || undefined,
        limit: 100,
      }),
  });

  const assetsById = new Map((assetsQuery.data?.items ?? []).map((a) => [a.id, a]));

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Data Quality</h1>
        <p className="mt-1 text-sm text-slate-500">
          Every issue here is derived evidence about the data — nothing here has changed a raw measurement.
        </p>
      </div>

      <div className="flex gap-4 rounded-xl border border-slate-200 bg-white p-4">
        <div>
          <label className="block text-xs font-medium text-slate-500">Asset</label>
          <select value={assetId} onChange={(e) => setAssetId(e.target.value)} className="mt-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">All assets</option>
            {(assetsQuery.data?.items ?? []).map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-500">Severity</label>
          <select
            value={severity}
            onChange={(e) => setSeverity(e.target.value as DataQualityIssueSeverity | "")}
            className="mt-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          >
            <option value="">All severities</option>
            <option value="critical">Critical</option>
            <option value="warning">Warning</option>
            <option value="info">Info</option>
          </select>
        </div>
      </div>

      {issuesQuery.isLoading && <LoadingState label="Loading data-quality issues..." />}
      {issuesQuery.isError && <ErrorState message={(issuesQuery.error as Error).message} />}
      {issuesQuery.data && issuesQuery.data.items.length === 0 && (
        <EmptyState title="No data-quality issues found" description="Either nothing has been ingested yet, or the data ingested so far is clean by these checks." />
      )}

      {issuesQuery.data && issuesQuery.data.items.length > 0 && (
        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-slate-500">
              <tr>
                <th className="px-4 py-2 font-medium">Severity</th>
                <th className="px-4 py-2 font-medium">Type</th>
                <th className="px-4 py-2 font-medium">Asset</th>
                <th className="px-4 py-2 font-medium">Description</th>
                <th className="px-4 py-2 font-medium">Detected</th>
              </tr>
            </thead>
            <tbody>
              {issuesQuery.data.items.map((issue) => (
                <tr key={issue.id} className="border-t border-slate-100 align-top">
                  <td className="px-4 py-2">
                    <SeverityBadge severity={issue.severity} />
                  </td>
                  <td className="px-4 py-2 text-slate-700">{ISSUE_TYPE_LABELS[issue.issue_type] ?? issue.issue_type}</td>
                  <td className="px-4 py-2 text-slate-600">{assetsById.get(issue.asset_id)?.name ?? issue.asset_id}</td>
                  <td className="px-4 py-2 text-slate-700">{issue.description}</td>
                  <td className="px-4 py-2 text-slate-500">{new Date(issue.detected_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
