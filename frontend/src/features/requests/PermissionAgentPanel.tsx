import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, FileCheck2, Loader2, Send, X } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ApiError } from "@/api/client";
import { api, queryKeys } from "@/api/endpoints";
import { AgentHeader, ThinkingIndicator } from "@/components/agents/AgentHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DetailList, DetailRow } from "@/components/ui/detail-list";
import { Label, Textarea } from "@/components/ui/input";
import { Stepper } from "@/components/ui/stepper";
import { ErrorState, Notice } from "@/components/ui/states";
import { RequestContextView } from "@/features/requests/WorkflowRequestCard";
import type { PermissionPreview, WorkflowRequest } from "@/types/api";

const SUGGESTIONS = {
  student: [
    "I need permission to attend the coding contest.",
    "I was absent yesterday because I was sick. I need attendance permission.",
    "I need leave tomorrow afternoon.",
  ],
  faculty: ["I need leave tomorrow afternoon.", "I need a substitute for my classes tomorrow.", "I need on duty leave tomorrow for an external workshop."],
  hod: ["I need leave tomorrow.", "I need a new projector for the CSE lab because the old one broke.", "I need to escalate the lab staffing shortage to the admin."],
};
const INTRO = {
  student: "Describe what you need. The agent collects your event, classes and attendance, finds the right faculty member, and shows you the request before anything is sent.",
  faculty: "Describe what you need. The agent collects the classes you would miss, sends the request to your HOD, and shows it to you before anything is sent.",
  hod: "Describe what you need. The agent collects the classes you would miss, sends the request to the Administration, and shows it to you before anything is sent.",
};

const errorText = (error: unknown) => (error instanceof ApiError ? error.message : "CampusNexus couldn't reach the Permission Agent.");

/**
 * Ask the Permission Agent -> structured preview -> Confirm & Send.
 * The agent only prepares a draft; nothing is sent until the requester confirms it.
 */
export function PermissionAgentPanel({
  onSent,
  role = "student",
  initialMessage,
}: {
  onSent?: (request: WorkflowRequest) => void;
  role?: "student" | "faculty" | "hod";
  initialMessage?: string;
}) {
  const client = useQueryClient();
  const [message, setMessage] = useState("");
  const [preview, setPreview] = useState<PermissionPreview | null>(null);
  const [reason, setReason] = useState("");
  const [sent, setSent] = useState<WorkflowRequest | null>(null);

  const prepare = useMutation({
    mutationFn: (text: string) => api.prepareRequest(text),
    onSuccess: (result) => {
      setPreview(result);
      setReason(result.request?.reason ?? "");
      setSent(null);
    },
  });
  const submit = useMutation({
    mutationFn: (request: WorkflowRequest) => api.submitRequest(request.request_id, reason.trim() !== request.reason ? reason.trim() : undefined),
    onSuccess: (request) => {
      setSent(request);
      setPreview(null);
      setMessage("");
      void client.invalidateQueries({ queryKey: queryKeys.workflowRequests });
      void client.invalidateQueries({ queryKey: ["student"] });
      onSent?.(request);
    },
  });
  const discard = useMutation({
    mutationFn: (request: WorkflowRequest) => api.cancelRequest(request.request_id),
    onSettled: () => setPreview(null),
  });

  const ask = (text: string) => {
    const value = text.trim();
    if (value.length < 3 || prepare.isPending) return;
    setMessage(value);
    prepare.mutate(value);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      ask(message);
    }
  };
  const draft = preview?.request ?? null;

  // A request handed over from agent chat ("I need leave tomorrow") is prepared once.
  const handedOver = useRef<string | null>(null);
  useEffect(() => {
    if (!initialMessage || handedOver.current === initialMessage) return;
    handedOver.current = initialMessage;
    setMessage(initialMessage);
    prepare.mutate(initialMessage);
  }, [initialMessage, prepare]);

  return (
    <div className="mx-auto w-full max-w-[800px] space-y-6">
      <AgentHeader icon={FileCheck2} title="Permission Agent" tagline="Prepares your request. Nothing is sent until you confirm it." capability="Prepares requests" />

      <Card className="p-4 sm:p-5">
        <p className="text-sm text-muted">{INTRO[role]}</p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {SUGGESTIONS[role].map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              disabled={prepare.isPending}
              onClick={() => ask(suggestion)}
              className="rounded-md border border-border bg-surface px-2.5 py-1 text-left text-[13px] text-ink transition-colors hover:border-primary/40 hover:bg-primary-soft/50 disabled:opacity-50"
            >
              {suggestion}
            </button>
          ))}
        </div>
        <div className="mt-4 flex items-end gap-2 rounded-card border border-border bg-surface p-2 transition-colors focus-within:border-primary focus-within:ring-3 focus-within:ring-primary/15">
          <Textarea
            rows={2}
            value={message}
            maxLength={1000}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="e.g. I need permission to attend the coding contest."
            aria-label="Message Permission Agent"
            className="field-sizing-content max-h-40 min-h-9 resize-none border-0 bg-transparent py-2 shadow-none hover:border-0 focus:ring-0"
          />
          <Button variant="accent" size="icon" aria-label="Prepare request" onClick={() => ask(message)} disabled={message.trim().length < 3 || prepare.isPending}>
            {prepare.isPending ? <Loader2 className="animate-spin" /> : <ArrowUp />}
          </Button>
        </div>
        {prepare.isPending && (
          <div className="mt-3">
            <ThinkingIndicator label="Collecting your classes, attendance and the right reviewer…" />
          </div>
        )}
        {prepare.isError && <ErrorState className="mt-3" message={errorText(prepare.error)} />}
      </Card>

      {sent && (
        <Notice tone="success">
          Sent. {sent.title} is <strong>{sent.status === "pending" ? "pending" : "waiting for review"}</strong>
          {sent.reviewer_name ? ` with ${sent.reviewer_name}` : ""}.
        </Notice>
      )}

      {preview && !draft && (
        <Notice tone="warning">
          <p>{preview.message}</p>
          {preview.options.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2">
              {preview.options.map((option) => (
                <Button key={option} variant="outline" size="sm" onClick={() => ask(`${message} (${option})`.replace(/\s+/g, " "))}>
                  {option}
                </Button>
              ))}
            </div>
          )}
        </Notice>
      )}

      {draft && (
        <Card className="overflow-hidden" aria-label="Request preview">
          <div className="flex flex-wrap items-start justify-between gap-3 px-5 pt-4 pb-4">
            <div className="min-w-0">
              <p className="text-xs font-medium text-warning">Preview · not sent yet</p>
              <h2 className="mt-1 text-base font-semibold text-ink">{draft.title}</h2>
              <p className="mt-1 text-sm text-muted">{preview?.message}</p>
            </div>
            <Badge tone="primary">{draft.type_label}</Badge>
          </div>
          <div className="grid grid-cols-1 gap-6 border-t border-border px-5 py-4 md:grid-cols-[minmax(0,1fr)_15rem]">
            <div className="min-w-0 space-y-5">
              <DetailList>
                <DetailRow label="Will be sent to">
                  {draft.reviewer_name ?? "No reviewer found (it will wait for review)"}
                  <p className="text-xs font-normal text-muted">{draft.routing_note}</p>
                </DetailRow>
              </DetailList>
              <RequestContextView request={draft} />
              <div className="space-y-1.5">
                <Label htmlFor="permission-reason">Reason</Label>
                <Textarea id="permission-reason" rows={2} value={reason} maxLength={1000} onChange={(event) => setReason(event.target.value)} />
              </div>
            </div>
            <div className="md:border-l md:border-border md:pl-6">
              <p className="mb-3 text-xs text-muted">What happens next</p>
              <Stepper
                steps={[
                  { label: "Request prepared", state: "done" },
                  { label: "You confirm and send", state: "current" },
                  { label: draft.reviewer_name ? `Sent to ${draft.reviewer_name}` : "Waits for a reviewer", state: "upcoming" },
                  { label: "Approved or rejected", state: "upcoming" },
                ]}
              />
            </div>
          </div>
          {submit.isError && <ErrorState className="mx-5 mb-4" message={errorText(submit.error)} />}
          <div className="flex flex-col-reverse gap-2 border-t border-border px-5 py-4 sm:flex-row sm:justify-end">
            <Button variant="ghost" onClick={() => discard.mutate(draft)} disabled={submit.isPending || discard.isPending}>
              <X /> Discard
            </Button>
            <Button variant="accent" onClick={() => submit.mutate(draft)} disabled={reason.trim().length < 3 || submit.isPending}>
              {submit.isPending ? <Loader2 className="animate-spin" /> : <Send />} Confirm &amp; Send
            </Button>
          </div>
        </Card>
      )}
    </div>
  );
}
