import { useEffect, useRef, type RefObject } from "react";
import { cn } from "@/utils/cn";

export type OrbState = "idle" | "connecting" | "listening" | "thinking" | "speaking" | "muted" | "error";

type RGB = [number, number, number];

/** Three light layers per state: (primary, secondary, accent). */
const PALETTES: Record<OrbState, [RGB, RGB, RGB]> = {
  idle: [[34, 211, 238], [59, 130, 246], [139, 92, 246]],
  connecting: [[59, 130, 246], [99, 102, 241], [34, 211, 238]],
  listening: [[103, 232, 249], [34, 211, 238], [59, 130, 246]],
  thinking: [[167, 139, 250], [139, 92, 246], [59, 130, 246]],
  speaking: [[56, 189, 248], [59, 130, 246], [139, 92, 246]],
  muted: [[100, 116, 139], [71, 85, 105], [51, 65, 85]],
  error: [[251, 113, 133], [244, 63, 94], [139, 92, 246]],
};

/** How much each state breathes, deforms and spins on its own (before audio). */
const MOTION: Record<OrbState, { breathe: number; wobble: number; spin: number; bars: number }> = {
  idle: { breathe: 0.025, wobble: 0.028, spin: 0.25, bars: 0.12 },
  connecting: { breathe: 0.05, wobble: 0.03, spin: 0.6, bars: 0.15 },
  listening: { breathe: 0.02, wobble: 0.035, spin: 0.4, bars: 1 },
  thinking: { breathe: 0.03, wobble: 0.055, spin: 1.4, bars: 0.25 },
  speaking: { breathe: 0.02, wobble: 0.04, spin: 0.5, bars: 1 },
  muted: { breathe: 0.012, wobble: 0.015, spin: 0.15, bars: 0.05 },
  error: { breathe: 0.02, wobble: 0.02, spin: 0.2, bars: 0.08 },
};

const BAR_COUNT = 120;
const rgba = ([r, g, b]: RGB, a: number) => `rgba(${r | 0},${g | 0},${b | 0},${a})`;
const isTestEnv = typeof navigator !== "undefined" && /jsdom/i.test(navigator.userAgent);

/**
 * The Nexus orb. A canvas of layered, organically deforming light blobs with a
 * radial audio-bar ring. ``analyser`` (when set) drives the ring and the blob
 * energy from real audio — the microphone while listening, the reply while
 * speaking. Colours and motion ease between states instead of snapping.
 */
export function NexusOrb({
  state,
  analyser,
  className,
  label,
}: {
  state: OrbState;
  analyser?: RefObject<AnalyserNode | null>;
  className?: string;
  /** Accessible name; the orb is otherwise decorative. */
  label?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stateRef = useRef(state);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || isTestEnv) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    let width = 0;
    let height = 0;
    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = canvas.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);

    const colors: [RGB, RGB, RGB] = PALETTES[stateRef.current].map((c) => [...c] as RGB) as [RGB, RGB, RGB];
    const motion = { ...MOTION[stateRef.current] };
    const bars = new Float32Array(BAR_COUNT);
    let freq: Uint8Array<ArrayBuffer> | null = null;
    let wave: Uint8Array<ArrayBuffer> | null = null;
    let energy = 0;
    let rotation = 0;
    let last = performance.now();
    let frame = 0;

    const blob = (cx: number, cy: number, r: number, t: number, seed: number, wobble: number) => {
      ctx.beginPath();
      const steps = 96;
      for (let i = 0; i <= steps; i++) {
        const a = (i / steps) * Math.PI * 2;
        const d =
          Math.sin(a * 2 + t * 0.9 + seed) * 0.5 +
          Math.sin(a * 3 - t * 1.3 + seed * 2.1) * 0.32 +
          Math.sin(a * 5 + t * 1.7 + seed * 3.7) * 0.18;
        const rr = r * (1 + d * wobble);
        const x = cx + Math.cos(a) * rr;
        const y = cy + Math.sin(a) * rr;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();
    };

    const draw = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000) * (reduced ? 0.25 : 1);
      last = now;
      const t = now / 1000 * (reduced ? 0.25 : 1);
      const s = stateRef.current;

      // Ease colours and motion toward the current state.
      const target = PALETTES[s];
      for (let l = 0; l < 3; l++) for (let k = 0; k < 3; k++) colors[l][k] += (target[l][k] - colors[l][k]) * 0.06;
      const m = MOTION[s];
      motion.breathe += (m.breathe - motion.breathe) * 0.05;
      motion.wobble += (m.wobble - motion.wobble) * 0.05;
      motion.spin += (m.spin - motion.spin) * 0.05;
      motion.bars += (m.bars - motion.bars) * 0.08;

      // Real audio, when an analyser is connected and the state uses it.
      const node = analyser?.current ?? null;
      let level = 0;
      const audible = node && (s === "listening" || s === "speaking");
      if (audible) {
        if (!freq || freq.length !== node.frequencyBinCount) {
          freq = new Uint8Array(node.frequencyBinCount);
          wave = new Uint8Array(node.fftSize);
        }
        node.getByteFrequencyData(freq);
        node.getByteTimeDomainData(wave!);
        let sum = 0;
        for (let i = 0; i < wave!.length; i++) {
          const v = (wave![i] - 128) / 128;
          sum += v * v;
        }
        level = Math.min(1, Math.sqrt(sum / wave!.length) * 4.2);
      }
      energy += (level - energy) * (level > energy ? 0.35 : 0.08);

      // Ring bars: mirrored spectrum when audible, a soft travelling wave otherwise.
      const usable = freq ? Math.floor(freq.length * 0.62) : 0;
      for (let i = 0; i < BAR_COUNT; i++) {
        const mirror = i < BAR_COUNT / 2 ? i : BAR_COUNT - 1 - i;
        let v: number;
        if (audible && freq && usable > 0) {
          const idx = Math.floor((mirror / (BAR_COUNT / 2)) * usable);
          v = Math.pow(freq[idx] / 255, 1.4);
        } else {
          v = (Math.sin(i * 0.32 + t * 1.6) * 0.5 + 0.5) * 0.22 + (Math.sin(i * 0.11 - t * 0.9) * 0.5 + 0.5) * 0.12;
        }
        bars[i] += (v * motion.bars - bars[i]) * (v * motion.bars > bars[i] ? 0.45 : 0.12);
      }

      rotation += dt * motion.spin;
      ctx.clearRect(0, 0, width, height);
      const cx = width / 2;
      const cy = height / 2;
      const half = Math.min(width, height) / 2;
      const R = half * 0.5 * (1 + Math.sin(t * 1.4) * motion.breathe + energy * 0.08);

      // Aura: fades to nothing inside the canvas, so its edge is never visible.
      const aura = ctx.createRadialGradient(cx, cy, R * 0.4, cx, cy, half);
      aura.addColorStop(0, rgba(colors[0], 0.28 + energy * 0.25));
      aura.addColorStop(0.45, rgba(colors[1], 0.1 + energy * 0.1));
      aura.addColorStop(1, rgba(colors[2], 0));
      ctx.fillStyle = aura;
      ctx.beginPath();
      ctx.arc(cx, cy, half, 0, Math.PI * 2);
      ctx.fill();

      // Radial bars.
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(rotation * 0.15);
      ctx.lineCap = "round";
      const inner = R * 1.3;
      for (let i = 0; i < BAR_COUNT; i++) {
        const a = (i / BAR_COUNT) * Math.PI * 2;
        const len = R * (0.025 + bars[i] * 0.55);
        const mix = i / BAR_COUNT;
        const c: RGB = [
          colors[0][0] + (colors[2][0] - colors[0][0]) * Math.abs(Math.sin(mix * Math.PI)),
          colors[0][1] + (colors[2][1] - colors[0][1]) * Math.abs(Math.sin(mix * Math.PI)),
          colors[0][2] + (colors[2][2] - colors[0][2]) * Math.abs(Math.sin(mix * Math.PI)),
        ];
        ctx.strokeStyle = rgba(c, 0.18 + bars[i] * 0.75);
        ctx.lineWidth = Math.max(1.2, R * 0.018);
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * inner, Math.sin(a) * inner);
        ctx.lineTo(Math.cos(a) * (inner + len), Math.sin(a) * (inner + len));
        ctx.stroke();
      }
      ctx.restore();

      // Thinking: two counter-rotating comet arcs.
      if (s === "thinking" || s === "connecting") {
        ctx.save();
        ctx.translate(cx, cy);
        ctx.lineCap = "round";
        for (let k = 0; k < 2; k++) {
          const dir = k === 0 ? 1 : -1.35;
          const start = t * 2.1 * dir + k * Math.PI;
          const radius = R * (1.16 + k * 0.07);
          const sweep = Math.PI * (0.55 + Math.sin(t * 1.7 + k) * 0.2);
          const segments = 22;
          for (let j = 0; j < segments; j++) {
            const a0 = start + (sweep * j) / segments;
            const a1 = start + (sweep * (j + 1)) / segments;
            ctx.strokeStyle = rgba(k === 0 ? colors[0] : colors[2], (j / segments) * 0.95);
            ctx.lineWidth = R * 0.022 * (0.4 + j / segments);
            ctx.beginPath();
            ctx.arc(0, 0, radius, a0, a1);
            ctx.stroke();
          }
        }
        ctx.restore();
      }

      // Connecting: expanding pulse rings.
      if (s === "connecting") {
        for (let k = 0; k < 3; k++) {
          const p = (t * 0.7 + k / 3) % 1;
          ctx.strokeStyle = rgba(colors[0], (1 - p) * 0.45);
          ctx.lineWidth = 1.5;
          ctx.beginPath();
          ctx.arc(cx, cy, R * (1 + p * 0.9), 0, Math.PI * 2);
          ctx.stroke();
        }
      }

      // Body: three additive blobs.
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      const wobble = motion.wobble + energy * 0.14;
      for (let l = 0; l < 3; l++) {
        const ox = Math.cos(rotation + l * 2.1) * R * 0.09;
        const oy = Math.sin(rotation * 1.2 + l * 2.1) * R * 0.09;
        const g = ctx.createRadialGradient(cx + ox - R * 0.25, cy + oy - R * 0.3, R * 0.05, cx + ox, cy + oy, R * 1.05);
        g.addColorStop(0, rgba(colors[l], 0.9));
        g.addColorStop(0.55, rgba(colors[l], 0.42));
        g.addColorStop(1, rgba(colors[l], 0));
        ctx.fillStyle = g;
        blob(cx + ox, cy + oy, R * (0.92 - l * 0.06), t * (0.8 + l * 0.25), l * 1.7, wobble);
        ctx.fill();
      }
      ctx.restore();

      // Glass core and specular highlight.
      const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.75);
      core.addColorStop(0, `rgba(255,255,255,${0.16 + energy * 0.22})`);
      core.addColorStop(0.5, "rgba(255,255,255,0.03)");
      core.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(cx, cy, R * 0.75, 0, Math.PI * 2);
      ctx.fill();

      const spec = ctx.createRadialGradient(cx - R * 0.35, cy - R * 0.42, 0, cx - R * 0.35, cy - R * 0.42, R * 0.45);
      spec.addColorStop(0, "rgba(255,255,255,0.35)");
      spec.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = spec;
      ctx.beginPath();
      ctx.arc(cx - R * 0.35, cy - R * 0.42, R * 0.45, 0, Math.PI * 2);
      ctx.fill();

      frame = requestAnimationFrame(draw);
    };
    frame = requestAnimationFrame(draw);

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [analyser]);

  return (
    <div className={cn("relative aspect-square", className)} role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
      {/* A CSS glow underneath, so the orb reads even before the first frame. */}
      <div
        className={cn(
          "absolute inset-[22%] rounded-full blur-2xl transition-colors duration-700",
          state === "thinking" ? "bg-violet/40" : state === "error" ? "bg-rose/35" : state === "muted" ? "bg-dim/30" : "bg-cyan/30",
        )}
      />
      <canvas ref={canvasRef} className="relative size-full" data-orb-state={state} />
    </div>
  );
}
