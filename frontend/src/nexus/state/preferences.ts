/**
 * Per-browser conveniences for the Nexus interface (spoken replies, motion).
 * Stored in localStorage when available; every access tolerates it failing.
 */
import { useSyncExternalStore } from "react";

export interface NexusPreferences {
  /** Play Nexus' spoken reply during a voice session. */
  speakReplies: boolean;
  /** Show the right-hand activity rail on wide screens. */
  showActivityRail: boolean;
}

const KEY = "campusai.preferences";
const DEFAULTS: NexusPreferences = { speakReplies: true, showActivityRail: true };
const listeners = new Set<() => void>();
let cache: NexusPreferences | null = null;

function read(): NexusPreferences {
  if (cache) return cache;
  try {
    const stored = window.localStorage.getItem(KEY);
    cache = { ...DEFAULTS, ...(stored ? (JSON.parse(stored) as Partial<NexusPreferences>) : {}) };
  } catch {
    cache = { ...DEFAULTS };
  }
  return cache;
}

export function setPreference<K extends keyof NexusPreferences>(key: K, value: NexusPreferences[K]): void {
  cache = { ...read(), [key]: value };
  try {
    window.localStorage.setItem(KEY, JSON.stringify(cache));
  } catch {
    // Private mode / blocked storage: the preference lasts for this page only.
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function usePreferences(): NexusPreferences {
  return useSyncExternalStore(subscribe, read, () => DEFAULTS);
}

/** Test hook: forget the cached value (localStorage is cleared between tests). */
export function resetPreferenceCache(): void {
  cache = null;
}
