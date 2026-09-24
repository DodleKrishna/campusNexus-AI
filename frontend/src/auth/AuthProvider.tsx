import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiError, setUnauthorizedHandler } from "@/api/client";
import { api } from "@/api/endpoints";
import { AuthContext, type AuthContextValue, type AuthStatus } from "@/auth/AuthContext";
import { tokenStore } from "@/auth/tokenStore";
import type { AuthUser } from "@/types/api";

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [status, setStatus] = useState<AuthStatus>(() => (tokenStore.get() ? "loading" : "anonymous"));
  const [notice, setNotice] = useState<string | null>(null);

  const signOutLocally = useCallback(
    (message: string | null) => {
      tokenStore.clear();
      queryClient.clear();
      setUser(null);
      setStatus("anonymous");
      setNotice(message);
    },
    [queryClient],
  );

  // Any 401 from the API ends the session: the server is the authority.
  useEffect(() => {
    setUnauthorizedHandler((message) => signOutLocally(message));
    return () => setUnauthorizedHandler(null);
  }, [signOutLocally]);

  // A stored token survives a page refresh; confirm it with the server once.
  useEffect(() => {
    if (!tokenStore.get()) return;
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (cancelled) return;
        setUser(me);
        setStatus("authenticated");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (error instanceof ApiError && error.isUnauthorized) return; // the 401 handler already signed out
        // Backend unreachable: keep the stored session for the next attempt.
        setStatus("anonymous");
        setNotice(error instanceof ApiError ? error.message : "CampusNexus is not reachable right now.");
      });
    return () => {
      cancelled = true;
    };
  }, [signOutLocally]);

  const login = useCallback(async (email: string, password: string) => {
    const response = await api.login(email, password);
    tokenStore.set({ token: response.access_token, expiresAt: response.expires_at });
    setUser(response.user);
    setStatus("authenticated");
    setNotice(null);
    return response.user;
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* the local sign-out below is what matters */
    }
    signOutLocally(null);
  }, [signOutLocally]);

  const value = useMemo<AuthContextValue>(() => ({ status, user, notice, login, logout }), [status, user, notice, login, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
