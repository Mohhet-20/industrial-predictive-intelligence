import React, { useCallback, useEffect, useMemo, useState } from "react";

import { api, ApiError, setAuthToken } from "@/api/client";
import type { TokenResponse, UserResponse } from "@/api/types";
import { AuthContext } from "./useAuth";

const STORAGE_KEY = "pred_platform_token";


export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem(STORAGE_KEY));
  const [user, setUser] = useState<UserResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadUser = useCallback(async () => {
    try {
      const me = await api.get<UserResponse>("/auth/me");
      setUser(me);
    } catch (err) {
      // Only clear a stored session when the SERVER said the token is bad
      // (401 authentication_failed) — a transient network failure while
      // loading the app (Wi-Fi hiccup, backend mid-restart) must not log
      // the user out of an otherwise-valid session. This distinction only
      // exists because the client now throws a separate NetworkError for
      // "never reached the server" (see api/client.ts) instead of treating
      // every failure the same way.
      if (err instanceof ApiError) {
        setToken(null);
        setUser(null);
        localStorage.removeItem(STORAGE_KEY);
        setAuthToken(null);
      } else {
        setError("Unable to reach the API right now. Your session was kept — retrying will pick it back up.");
      }
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (token) {
      setAuthToken(token);
      loadUser();
    } else {
      setIsLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const login = useCallback(async (email: string, password: string) => {
    setError(null);
    try {
      const tokenResponse = await api.post<TokenResponse>("/auth/login", { email, password });
      localStorage.setItem(STORAGE_KEY, tokenResponse.access_token);
      setAuthToken(tokenResponse.access_token);
      setToken(tokenResponse.access_token);
      const me = await api.get<UserResponse>("/auth/me");
      setUser(me);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Unable to reach the API. Is the backend running?");
      }
      throw err;
    }
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setAuthToken(null);
    setToken(null);
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ user, token, isLoading, error, login, logout }),
    [user, token, isLoading, error, login, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
