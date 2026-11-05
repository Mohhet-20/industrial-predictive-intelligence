import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const exploreDemo = vi.hoisted(() => vi.fn(() => Promise.resolve()));
const login = vi.hoisted(() => vi.fn(() => Promise.resolve()));
vi.mock("@/auth/useAuth", () => ({
  useAuth: () => ({ token: null, user: null, isLoading: false, error: null, login, exploreDemo, logout: vi.fn() }),
}));

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
  exploreDemo.mockClear();
  login.mockClear();
});

async function renderLogin() {
  const { LoginPage } = await import("./LoginPage");
  return render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>
  );
}

describe("LoginPage", () => {
  it("shows no demo credentials and does not prefill an account", async () => {
    const { container } = await renderLogin();
    expect(container.textContent).not.toMatch(/ChangeMe|riverbend-demo/i);
    expect(screen.getByLabelText("Email")).toHaveValue("");
    expect(screen.queryByRole("button", { name: "Explore the live demo" })).toBeNull();
  });

  it("offers a guest session when the build enables it", async () => {
    vi.stubEnv("VITE_GUEST_ACCESS", "true");
    await renderLogin();
    await userEvent.click(screen.getByRole("button", { name: "Explore the live demo" }));
    expect(exploreDemo).toHaveBeenCalledTimes(1);
    expect(login).not.toHaveBeenCalled();
  });
});
