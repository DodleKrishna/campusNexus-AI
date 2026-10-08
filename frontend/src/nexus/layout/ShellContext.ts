import { createContext, useContext } from "react";

export interface ShellContextValue {
  openVoice: () => void;
  openProfile: () => void;
  openActivity: () => void;
}

export const ShellContext = createContext<ShellContextValue | null>(null);

export function useShell(): ShellContextValue {
  const value = useContext(ShellContext);
  if (!value) throw new Error("useShell must be used inside <NexusShell>");
  return value;
}
