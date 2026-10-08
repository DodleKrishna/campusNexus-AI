import { useContext } from "react";
import { AssistantContext, type AssistantContextValue } from "@/nexus/state/AssistantContext";

export function useAssistant(): AssistantContextValue {
  const value = useContext(AssistantContext);
  if (!value) throw new Error("useAssistant must be used inside <AssistantProvider>");
  return value;
}
