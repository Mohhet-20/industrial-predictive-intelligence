import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "@/auth/useAuth";
import { ErrorBoundary } from "@/components/ErrorBoundary";

const NAV_ITEMS = [
  { to: "/", label: "Overview", end: true },
  { to: "/workspace", label: "Intelligence Workspace" },
  { to: "/assets", label: "Assets" },
  { to: "/explorer", label: "Data Explorer" },
  { to: "/data-quality", label: "Data Quality" },
  { to: "/temporal-analysis", label: "Temporal Analysis" },
  { to: "/intelligence", label: "Intelligence" },
  { to: "/condition", label: "Condition Intelligence" },
  { to: "/scenarios", label: "Scenario Lab" },
  { to: "/outcomes", label: "Outcome & Learning Center" },
  { to: "/mlops", label: "MLOps & Governance" },
  { to: "/system", label: "System Health" },
];

export function Layout() {
  const { user, logout } = useAuth();
  const location = useLocation();

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-3">
          <div>
            <p className="text-sm font-semibold text-slate-900">Industrial AI Predictive Intelligence Platform</p>
            <p className="text-xs text-slate-500">{user?.organization_name}</p>
          </div>
          <div className="flex items-center gap-4 text-sm">
            <span className="text-slate-600">
              {user?.full_name} <span className="text-slate-400">({user?.role})</span>
            </span>
            <button onClick={logout} className="rounded border border-slate-300 px-3 py-1 text-slate-700 hover:bg-slate-100">
              Log out
            </button>
          </div>
        </div>
        <nav className="mx-auto flex max-w-7xl gap-1 px-6">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `border-b-2 px-3 py-2 text-sm font-medium ${
                  isActive ? "border-brand-600 text-brand-700" : "border-transparent text-slate-600 hover:text-slate-900"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-6">
        {/* key={pathname} remounts the boundary (clearing any caught error)
            on navigation, so a crash on one page doesn't leave every other
            page stuck showing the same fallback. */}
        <ErrorBoundary key={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}
