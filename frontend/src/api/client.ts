/**
 * The one place the app talks to the backend: base URL, bearer token, JSON,
 * normalized errors and session expiry. Components never call fetch directly.
 */
import { tokenStore } from "@/auth/tokenStore";

export const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api";

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
  get isUnauthorized(): boolean {
    return this.status === 401;
  }
  get isUnavailable(): boolean {
    return this.status === 503 || this.status === 0;
  }
}

type UnauthorizedHandler = (message: string) => void;
let onUnauthorized: UnauthorizedHandler | null = null;

/** The auth provider registers how to react when the server rejects the session. */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler;
}

const FALLBACK_MESSAGES: Record<number, string> = {
  0: "CampusNexus is not reachable right now. Check that the backend is running.",
  403: "You don't have access to this.",
  404: "That was not found.",
  500: "Something went wrong on our side. Please try again.",
  502: "The AI provider returned an unusable response. Please try again.",
  503: "Live AI is temporarily unavailable. Please try again shortly.",
};

function messageFrom(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) return "Some of the information sent was not valid.";
  // Structured refusals (AGENT_DISABLED, AI_BUDGET_EXCEEDED, ...) keep their machine-readable code visible.
  const coded = detail as { code?: unknown; message?: unknown } | null;
  if (coded && typeof coded.code === "string") return typeof coded.message === "string" ? `${coded.message} (${coded.code})` : coded.code;
  return FALLBACK_MESSAGES[status] ?? FALLBACK_MESSAGES[500];
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  auth?: boolean;
  signal?: AbortSignal;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, auth = true, signal } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const session = tokenStore.get();
    if (session) headers.Authorization = `Bearer ${session.token}`;
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method, headers, signal, body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if ((error as Error).name === "AbortError") throw error;
    throw new ApiError(0, FALLBACK_MESSAGES[0]);
  }

  if (response.status === 204) return undefined as T;
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const message = messageFrom(response.status, payload);
    if (response.status === 401 && auth) onUnauthorized?.(message);
    throw new ApiError(response.status, message);
  }
  return payload as T;
}
