import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { JobResponse, Page, ReadinessResponse, SystemMetricsResponse } from "@/api/types";
import { EmptyState, ErrorState, LoadingState } from "@/components/StateViews";

export function SystemHealthPage() {
  const readyQuery = useQuery({
    queryKey: ["health", "ready"],
    queryFn: () => api.get<ReadinessResponse>("/health/ready"),
    refetchInterval: 15_000,
  });
  const jobsQuery = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.get<Page<JobResponse>>("/jobs", { limit: 20 }),
  });
  const metricsQuery = useQuery({
    queryKey: ["system", "metrics"],
    queryFn: () => api.get<SystemMetricsResponse>("/system/metrics"),
    refetchInterval: 30_000,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">System Health</h1>
        <p className="mt-1 text-sm text-slate-500">Real, measured state — not a decorative dashboard.</p>
      </div>

      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold text-slate-900">API readiness</h2>
        {readyQuery.isLoading && <LoadingState label="Checking..." />}
        {readyQuery.isError && <ErrorState message="Could not reach the API." />}
        {readyQuery.data && (
          <div className="mt-3 space-y-1 text-sm">
            <p>
              Overall status:{" "}
              <span className={readyQuery.data.status === "ok" ? "text-emerald-700" : "text-amber-700"}>
                {readyQuery.data.status}
              </span>{" "}
              <span className="text-slate-400">({readyQuery.data.environment})</span>
            </p>
            {readyQuery.data.schema_version && (
              <p className="text-slate-500">
                Schema version: <span className="font-mono text-xs">{readyQuery.data.schema_version}</span>
              </p>
            )}
            {Object.entries(readyQuery.data.checks).map(([name, status]) => (
              <p key={name} className="text-slate-600">
                {name}: <span className={status === "ok" ? "text-emerald-700" : "text-red-700"}>{status}</span>
              </p>
            ))}
          </div>
        )}
      </div>

      <div className="rounded-xl border border-slate-200 bg-white">
        <div className="border-b border-slate-200 px-5 py-3">
          <h2 className="text-sm font-semibold text-slate-900">Background jobs</h2>
        </div>
        {jobsQuery.isLoading && <LoadingState label="Loading jobs..." />}
        {jobsQuery.data && jobsQuery.data.items.length === 0 && (
          <div className="p-5">
            <EmptyState title="No background jobs yet" description="Large ingestion batches submitted via POST /ingestion/batches will appear here." />
          </div>
        )}
        {jobsQuery.data && jobsQuery.data.items.length > 0 && (
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-slate-500">
              <tr>
                <th className="px-4 py-2 font-medium">Type</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Progress</th>
                <th className="px-4 py-2 font-medium">Created</th>
              </tr>
            </thead>
            <tbody>
              {jobsQuery.data.items.map((job) => (
                <tr key={job.id} className="border-t border-slate-100">
                  <td className="px-4 py-2">{job.job_type}</td>
                  <td className="px-4 py-2">{job.status}</td>
                  <td className="px-4 py-2">{Math.round(job.progress * 100)}%</td>
                  <td className="px-4 py-2 text-slate-500">{new Date(job.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold text-slate-900">Operational metrics</h2>
        <p className="mt-1 text-xs text-slate-500">
          Real, measured figures over the trailing 7 days plus this process's own in-memory request counters —
          never fabricated. See the note field in the raw response for exact scope.
        </p>
        {metricsQuery.isLoading && <LoadingState label="Loading metrics..." />}
        {metricsQuery.isError && <ErrorState message="Could not load operational metrics." />}
        {metricsQuery.data && (
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-3">
            <div className="rounded-lg border border-slate-100 bg-slate-50 p-3">
              <p className="text-xs font-medium uppercase text-slate-500">Requests</p>
              <p className="mt-1 text-lg font-semibold text-slate-900">
                {metricsQuery.data.request_metrics.total_requests_recorded}
              </p>
              <p className="text-xs text-slate-500">
                p95 latency: {metricsQuery.data.request_metrics.latency_ms.p95 ?? "n/a"} ms
              </p>
              <p className="text-xs text-slate-500">
                {Object.entries(metricsQuery.data.request_metrics.requests_by_status_class)
                  .map(([cls, count]) => `${cls}: ${count}`)
                  .join(" · ")}
              </p>
            </div>
            <div className="rounded-lg border border-slate-100 bg-slate-50 p-3">
              <p className="text-xs font-medium uppercase text-slate-500">Ingestion (7d)</p>
              {Object.entries(metricsQuery.data.ingestion.batches_by_status).length === 0 && (
                <p className="mt-1 text-sm text-slate-400">No batches in this window.</p>
              )}
              {Object.entries(metricsQuery.data.ingestion.batches_by_status).map(([status, count]) => (
                <p key={status} className="text-sm text-slate-700">
                  {status}: {count}
                </p>
              ))}
            </div>
            <div className="rounded-lg border border-slate-100 bg-slate-50 p-3">
              <p className="text-xs font-medium uppercase text-slate-500">Anomaly detection (7d)</p>
              <p className="text-sm text-slate-700">
                Failed runs: <span className={metricsQuery.data.anomaly_detection.failed_runs > 0 ? "text-red-700" : ""}>{metricsQuery.data.anomaly_detection.failed_runs}</span>
              </p>
              {Object.entries(metricsQuery.data.anomaly_detection.anomalies_by_severity).map(([tier, count]) => (
                <p key={tier} className="text-sm text-slate-700">
                  {tier}: {count}
                </p>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
