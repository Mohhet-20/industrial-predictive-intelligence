
export function LoadingState({ label = "Loading..." }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-8 text-slate-500">
      <span className="h-3 w-3 animate-pulse rounded-full bg-brand-500" />
      <span>{label}</span>
    </div>
  );
}

export function ErrorState({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
      <span className="font-medium">Something went wrong: </span>
      {message}
    </div>
  );
}

export function EmptyState({ title, description }: { title: string; description?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
      <p className="font-medium text-slate-700">{title}</p>
      {description && <p className="mt-1 text-sm text-slate-500">{description}</p>}
    </div>
  );
}

export function NotYetAvailable({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 px-6 py-10 text-center">
      <p className="font-medium text-amber-900">Not yet available</p>
      <p className="mx-auto mt-2 max-w-xl text-sm text-amber-800">{message}</p>
    </div>
  );
}

export function SeverityBadge({ severity }: { severity: "info" | "warning" | "critical" }) {
  const styles: Record<string, string> = {
    info: "bg-slate-100 text-slate-700",
    warning: "bg-amber-100 text-amber-800",
    critical: "bg-red-100 text-red-800",
  };
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${styles[severity]}`}>
      {severity}
    </span>
  );
}
