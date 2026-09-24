import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, CheckCircle2, FileCheck2, Loader2, Send, X } from "lucide-react";
import { useState, type KeyboardEvent } from "react";
import { ApiError } from "@/api/client";
import { api, queryKeys } from "@/api/endpoints";
import { ModeBadge } from "@/components/layout/ModeBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Label, Textarea } from "@/components/ui/input";
import { ErrorState, Notice } from "@/components/ui/states";
import { RequestContextView } from "@/features/requests/WorkflowRequestCard";
import type { PermissionPreview, WorkflowRequest } from "@/types/api";

const SUGGESTIONS = [
  "I need permission to attend the coding contest.",
  "I was absent yesterday because I was sick. I need attendance permission.",
  "I need leave tomorrow afternoon.",
];

const errorText = (error: unknown) => (error instanceof ApiError ? error.message : "CampusNexus couldn't reach the Permission Agent.");

/**
 * Ask the Permission Agent -> structured preview -> Confirm & Send.
 * The agent only prepares a draft; nothing is sent until the student confirms it.
 */
export function PermissionAgentPanel({ onSent }: { onSent?: (request: WorkflowRequest) => void }) {
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

  return (
    <div className="space-y-4">
      <Card className="flex items-start gap-4 px-5 py-4">
        <div className="flex size-11 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent-hover">
          <FileCheck2 className="size-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-lg font-semibold">Permission Agent</h1>
            <Badge tone="success">Available</Badge>
          </div>
          <p className="mt-0.5 text-sm text-muted">
            Describe what you need. The agent collects your event, classes and attendance, finds the right faculty member, and shows you the request before anything is sent.
          </p>
        </div>
        <ModeBadge />
      </Card>

      <Card className="px-5 py-4">
        <div className="mb-3 flex flex-wrap gap-2">
          {SUGGESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              disabled={prepare.isPending}
              onClick={() => ask(suggestion)}
              className="rounded-full border border-border bg-surface px-3 py-1 text-xs text-muted transition-colors hover:border-accent/40 hover:text-ink disabled:opacity-50"
            >
              {suggestion}
            </button>
          ))}
        </div>
        <div className="flex items-end gap-2">
          <Textarea
            rows={2}
            value={message}
            maxLength={1000}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="e.g. I need permission to attend the coding contest."
            aria-label="Message Permission Agent"
            className="resize-none"
          />
          <Button variant="accent" size="icon" aria-label="Prepare request" onClick={() => ask(message)} disabled={message.trim().length < 3 || prepare.isPending}>
            {prepare.isPending ? <Loader2 className="animate-spin" /> : <ArrowUp />}
          </Button>
        </div>
        {prepare.isPending && <p className="mt-2 text-sm text-muted" role="status">Collecting your classes, attendance and the right reviewer…</p>}
        {prepare.isError && <ErrorState className="mt-3" message={errorText(prepare.error)} />}
      </Card>

      {sent && (
        <Notice tone="success">
          <div className="flex items-start gap-2">
            <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />
            <span>
              Sent. {sent.title} is <strong>{sent.status === "pending" ? "pending" : "waiting for review"}</strong>
              {sent.reviewer_name ? ` with ${sent.reviewer_name}` : ""}.
            </span>
          </div>
        </Notice>
      )}

      {preview && !draft && (
        <Notice tone="warning">
          <p>{preview.message}</p>
          {preview.options.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-2">
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
        <Card className="space-y-4 px-5 py-4" aria-label="Request preview">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-subtle">Request preview · not sent yet</p>
              <h2 className="mt-1 text-base font-semibold">{draft.title}</h2>
              <p className="mt-1 text-sm text-muted">{preview?.message}</p>
            </div>
            <Badge tone="primary">{draft.type_label}</Badge>
          </div>
          <div className="rounded-lg border border-border bg-surface-muted px-4 py-3 text-sm">
            <span className="text-muted">Will be sent to: </span>
            <span className="font-medium">{draft.reviewer_name ?? "No reviewer found (it will wait for review)"}</span>
            <p className="mt-0.5 text-xs text-muted">{draft.routing_note}</p>
          </div>
          <RequestContextView request={draft} />
          <div className="space-y-1.5">
            <Label htmlFor="permission-reason">Reason</Label>
            <Textarea id="permission-reason" rows={2} value={reason} maxLength={1000} onChange={(event) => setReason(event.target.value)} />
          </div>
          {submit.isError && <ErrorState message={errorText(submit.error)} />}
          <div className="flex flex-wrap justify-end gap-2">
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
