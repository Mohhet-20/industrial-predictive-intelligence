import React from "react";

interface Props {
  children: React.ReactNode;
}

interface State {
  error: Error | null;
}

/** Route-level error boundary (Phase 7, §9). Catches a render-time crash
 * in whichever page is currently mounted inside Layout's <Outlet/> and
 * shows a real, honest failure state instead of a blank white screen — the
 * nav/header chrome around it (which lives outside this boundary, in
 * Layout itself) stays usable so the user can navigate to a different page
 * rather than being stuck. This does NOT catch data-fetching errors (those
 * are handled per-page via TanStack Query's isError + ErrorState/ApiError,
 * see StateViews.tsx) — only genuine unexpected exceptions thrown while
 * rendering, which is what React error boundaries are actually for. */
export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("Unhandled error rendering a page:", error, info.componentStack);
  }

  private reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return (
        <div className="rounded-lg border border-red-200 bg-red-50 px-6 py-8 text-center">
          <p className="font-medium text-red-900">Something went wrong displaying this page.</p>
          <p className="mx-auto mt-2 max-w-xl text-sm text-red-800">
            This is a system error, not missing data — the page hit an unexpected problem while rendering.
            {this.state.error.message ? ` (${this.state.error.message})` : ""}
          </p>
          <button
            onClick={this.reset}
            className="mt-4 rounded border border-red-300 bg-white px-3 py-1.5 text-sm text-red-800 hover:bg-red-100"
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
