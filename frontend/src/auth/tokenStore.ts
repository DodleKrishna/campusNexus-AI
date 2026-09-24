/**
 * Where the session token lives. localStorage keeps the user signed in across
 * a page refresh; the token is short-lived (server TTL) and cleared on sign-out
 * or on any 401.
 */
const KEY = "campusnexus.session";

export interface StoredSession {
  token: string;
  expiresAt: string;
}

export const tokenStore = {
  get(): StoredSession | null {
    try {
      const raw = window.localStorage.getItem(KEY);
      if (!raw) return null;
      const session = JSON.parse(raw) as StoredSession;
      if (!session.token || new Date(session.expiresAt).getTime() <= Date.now()) {
        window.localStorage.removeItem(KEY);
        return null;
      }
      return session;
    } catch {
      return null;
    }
  },
  set(session: StoredSession): void {
    try {
      window.localStorage.setItem(KEY, JSON.stringify(session));
    } catch {
      /* private mode: the session still works for this tab */
    }
  },
  clear(): void {
    try {
      window.localStorage.removeItem(KEY);
    } catch {
      /* ignore */
    }
  },
};
