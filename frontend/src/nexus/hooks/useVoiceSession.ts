import { useCallback, useEffect, useRef, useState } from "react";
import { BargeInDetector, VoiceActivityDetector } from "@/nexus/audio/vad";
import { base64ToArrayBuffer, rms, toUploadWav } from "@/nexus/audio/wav";
import type { VoiceTurnResult } from "@/nexus/state/AssistantContext";

export type VoiceState = "idle" | "connecting" | "listening" | "thinking" | "speaking" | "error";
/** What the UI shows: the session state, or "muted" while listening with the microphone muted. */
export type VoiceDisplayState = VoiceState | "muted";

/** Audio kept from just before speech is detected, so the first syllable is not clipped. */
const PREROLL_CHUNKS = 4;
const CHUNK_SIZE = 4096;

interface Engine {
  ctx: AudioContext;
  stream: MediaStream;
  source: MediaStreamAudioSourceNode;
  processor: ScriptProcessorNode;
  sink: GainNode;
  micAnalyser: AnalyserNode;
  outAnalyser: AnalyserNode;
  playback: AudioBufferSourceNode | null;
}

function microphoneError(error: unknown): string {
  const name = (error as DOMException | null)?.name;
  if (name === "NotAllowedError" || name === "SecurityError") return "Microphone access was blocked. Allow it in the browser to talk to Nexus.";
  if (name === "NotFoundError") return "No microphone was found on this device.";
  return "The microphone could not be started.";
}

/**
 * One hands-free voice conversation with Nexus.
 *
 * idle → connecting (microphone permission) → listening → [pause detected] →
 * thinking (one bounded WAV upload) → speaking (the reply audio) → listening …
 *
 * Capture is ignored while thinking, so Nexus never hears itself. While Nexus is speaking, the microphone is
 * only watched for a barge-in: if the user talks over the reply (sustained loud input, after echo cancellation),
 * playback stops and the user's words become the next turn. ``analyser`` always points at the audio the orb
 * should visualise. Muting disables the microphone track itself; ``end`` stops every track, the playback and the
 * audio context, so no microphone stays open after the session.
 */
export function useVoiceSession({ onUtterance, speakReplies }: {
  onUtterance: (wav: Blob, durationSec: number, signal: AbortSignal) => Promise<VoiceTurnResult>;
  speakReplies: boolean;
}) {
  const [state, setStateValue] = useState<VoiceState>("idle");
  const [muted, setMutedValue] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);

  const analyser = useRef<AnalyserNode | null>(null);
  const engine = useRef<Engine | null>(null);
  const stateRef = useRef<VoiceState>("idle");
  const mutedRef = useRef(false);
  const chunks = useRef<Float32Array[]>([]);
  const preroll = useRef<Float32Array[]>([]);
  const vad = useRef(new VoiceActivityDetector());
  const bargeIn = useRef(new BargeInDetector());
  const interruptRef = useRef<() => void>(() => undefined);
  const inflight = useRef<AbortController | null>(null);
  const session = useRef(0);
  const onUtteranceRef = useRef(onUtterance);
  const speakRef = useRef(speakReplies);

  useEffect(() => {
    onUtteranceRef.current = onUtterance;
    speakRef.current = speakReplies;
  }, [onUtterance, speakReplies]);

  const setState = useCallback((next: VoiceState) => {
    stateRef.current = next;
    setStateValue(next);
    const e = engine.current;
    analyser.current = e ? (next === "speaking" ? e.outAnalyser : e.micAnalyser) : null;
  }, []);

  const listen = useCallback(() => {
    chunks.current = [];
    preroll.current = [];
    vad.current.reset();
    setCapturing(false);
    setError(null);
    setState("listening");
  }, [setState]);

  const teardown = useCallback(() => {
    session.current += 1;
    inflight.current?.abort();
    inflight.current = null;
    const e = engine.current;
    engine.current = null;
    analyser.current = null;
    if (e) {
      try {
        e.playback?.stop();
      } catch {
        // already stopped
      }
      e.processor.onaudioprocess = null;
      e.stream.getTracks().forEach((track) => track.stop());
      void e.ctx.close().catch(() => undefined);
    }
    chunks.current = [];
    preroll.current = [];
    vad.current.reset();
    mutedRef.current = false;
    setMutedValue(false);
    setCapturing(false);
  }, []);

  const playReply = useCallback(
    async (base64: string, id: number) => {
      const e = engine.current;
      if (!e) return listen();
      try {
        const buffer = await e.ctx.decodeAudioData(base64ToArrayBuffer(base64));
        if (session.current !== id || !engine.current) return;
        const source = e.ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(e.outAnalyser);
        e.playback = source;
        source.onended = () => {
          if (session.current !== id || e.playback !== source) return;
          e.playback = null;
          listen();
        };
        bargeIn.current.reset();
        setState("speaking");
        source.start();
      } catch {
        listen();
      }
    },
    [listen, setState],
  );

  const submit = useCallback(async () => {
    const e = engine.current;
    const captured = chunks.current;
    chunks.current = [];
    preroll.current = [];
    vad.current.reset();
    setCapturing(false);
    if (!e || captured.length === 0) return;
    const samples = captured.reduce((n, c) => n + c.length, 0);
    const wav = toUploadWav(captured, e.ctx.sampleRate);
    const id = session.current;
    const controller = new AbortController();
    inflight.current = controller;
    setState("thinking");
    const result = await onUtteranceRef.current(wav, samples / e.ctx.sampleRate, controller.signal);
    if (session.current !== id) return;
    inflight.current = null;
    if (!result.ok) {
      setError(result.error);
      setState("error");
      return;
    }
    if (speakRef.current && result.reply.audio_wav_base64) await playReply(result.reply.audio_wav_base64, id);
    else listen();
  }, [listen, playReply, setState]);

  const submitRef = useRef(submit);
  useEffect(() => {
    submitRef.current = submit;
  }, [submit]);

  const start = useCallback(async () => {
    if (stateRef.current !== "idle" && stateRef.current !== "error") return;
    if (engine.current) return listen();
    setError(null);
    setState("connecting");
    const id = ++session.current;
    if (!navigator.mediaDevices?.getUserMedia) {
      setError("Voice needs a microphone and a secure page (https or localhost).");
      setState("error");
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      if (session.current === id) {
        setError(microphoneError(err));
        setState("error");
      }
      return;
    }
    if (session.current !== id) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    const ctx = new AudioContext();
    await ctx.resume().catch(() => undefined);
    const source = ctx.createMediaStreamSource(stream);
    const micAnalyser = ctx.createAnalyser();
    micAnalyser.fftSize = 1024;
    micAnalyser.smoothingTimeConstant = 0.78;
    const outAnalyser = ctx.createAnalyser();
    outAnalyser.fftSize = 1024;
    outAnalyser.smoothingTimeConstant = 0.72;
    outAnalyser.connect(ctx.destination);
    // ScriptProcessorNode is deprecated but universally available and needs no worklet module.
    const processor = ctx.createScriptProcessor(CHUNK_SIZE, 1, 1);
    const sink = ctx.createGain();
    sink.gain.value = 0;
    source.connect(micAnalyser);
    source.connect(processor);
    processor.connect(sink);
    sink.connect(ctx.destination);
    processor.onaudioprocess = (event) => {
      if (mutedRef.current) return;
      if (stateRef.current === "speaking") {
        const level = rms(event.inputBuffer.getChannelData(0));
        if (bargeIn.current.push(level, (event.inputBuffer.length / ctx.sampleRate) * 1000)) interruptRef.current();
        return;
      }
      if (stateRef.current !== "listening") return;
      const data = new Float32Array(event.inputBuffer.getChannelData(0));
      const verdict = vad.current.push(rms(data), (data.length / ctx.sampleRate) * 1000);
      if (verdict === "speech_start") {
        chunks.current = [...preroll.current, data];
        preroll.current = [];
        setCapturing(true);
      } else if (vad.current.inUtterance || verdict === "utterance_end" || verdict === "max_length") {
        chunks.current.push(data);
      } else {
        preroll.current = [...preroll.current.slice(-(PREROLL_CHUNKS - 1)), data];
      }
      if (verdict === "utterance_end" || verdict === "max_length") void submitRef.current();
      else if (verdict === "discard") {
        chunks.current = [];
        setCapturing(false);
      }
    };
    engine.current = { ctx, stream, source, processor, sink, micAnalyser, outAnalyser, playback: null };
    listen();
  }, [listen, setState]);

  const end = useCallback(() => {
    teardown();
    setError(null);
    setState("idle");
  }, [setState, teardown]);

  const toggleMute = useCallback(() => {
    const next = !mutedRef.current;
    mutedRef.current = next;
    setMutedValue(next);
    engine.current?.stream.getAudioTracks().forEach((track) => (track.enabled = !next));
    if (next) {
      chunks.current = [];
      vad.current.reset();
      setCapturing(false);
    }
  }, []);

  /** Send what has been said so far without waiting for the pause. */
  const sendNow = useCallback(() => {
    if (stateRef.current === "listening" && chunks.current.length > 0) void submit();
  }, [submit]);

  /** Stop Nexus mid-reply and listen again. */
  const interrupt = useCallback(() => {
    const e = engine.current;
    if (stateRef.current !== "speaking" || !e?.playback) return;
    const playing = e.playback;
    e.playback = null;
    bargeIn.current.reset();
    try {
      playing.stop();
    } catch {
      // already stopped
    }
    listen();
  }, [listen]);
  useEffect(() => {
    interruptRef.current = interrupt;
  }, [interrupt]);

  useEffect(() => teardown, [teardown]);

  const displayState: VoiceDisplayState = muted && state === "listening" ? "muted" : state;
  return { state, displayState, muted, error, capturing, analyser, start, end, toggleMute, sendNow, interrupt, retry: start };
}

export type VoiceSession = ReturnType<typeof useVoiceSession>;
