import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./ErrorBoundary";

function Bomb(): never {
  throw new Error("Boom from a page render");
}

describe("ErrorBoundary", () => {
  it("renders children normally when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>All good</p>
      </ErrorBoundary>
    );
    expect(screen.getByText("All good")).toBeInTheDocument();
  });

  it("catches a render-time crash and shows a real failure state instead of a blank page", () => {
    // React logs the caught error to the console by default during tests;
    // silence it so the test output isn't noise, without hiding a real
    // assertion failure if the boundary doesn't actually catch it.
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>
    );

    expect(screen.getByText(/something went wrong displaying this page/i)).toBeInTheDocument();
    expect(screen.getByText(/Boom from a page render/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();

    consoleErrorSpy.mockRestore();
  });
});
