/**
 * A small, deterministic voice-activity detector for hands-free turns. It is fed
 * one RMS level per audio chunk and decides when an utterance has started and
 * ended. Pure state — no audio APIs — so it is unit-tested directly.
 */

export interface VadConfig {
  /** Minimum absolute level that can count as speech. */
  minThreshold: number;
  /** Speech must exceed the adaptive noise floor by this factor. */
  floorFactor: number;
  /** Silence after speech that ends the utterance. */
  endSilenceMs: number;
  /** Speech shorter than this is treated as a noise blip and discarded. */
  minSpeechMs: number;
  /** Hard cap: the utterance is sent once it reaches this length. */
  maxUtteranceMs: number;
}

export const DEFAULT_VAD: VadConfig = {
  minThreshold: 0.012,
  floorFactor: 2.6,
  endSilenceMs: 1100,
  minSpeechMs: 350,
  // The backend accepts at most 30 s; leave headroom for the WAV header and rounding.
  maxUtteranceMs: 28_000,
};

export type VadEvent = "none" | "speech_start" | "utterance_end" | "discard" | "max_length";

export class VoiceActivityDetector {
  private floor = 0.006;
  private speaking = false;
  private speechMs = 0;
  private silenceMs = 0;
  private totalMs = 0;
  private readonly config: VadConfig;

  constructor(config: Partial<VadConfig> = {}) {
    this.config = { ...DEFAULT_VAD, ...config };
  }

  get inUtterance(): boolean {
    return this.speaking;
  }

  reset(): void {
    this.speaking = false;
    this.speechMs = 0;
    this.silenceMs = 0;
    this.totalMs = 0;
  }

  /** Feed one chunk's level and duration; returns what happened. */
  push(level: number, chunkMs: number): VadEvent {
    const threshold = Math.max(this.config.minThreshold, this.floor * this.config.floorFactor);
    const loud = level >= threshold;
    if (!this.speaking) {
      // Track the room's noise floor only while nobody is talking.
      this.floor = this.floor * 0.95 + Math.min(level, 0.05) * 0.05;
      if (!loud) return "none";
      this.speaking = true;
      this.speechMs = chunkMs;
      this.silenceMs = 0;
      this.totalMs = chunkMs;
      return "speech_start";
    }
    this.totalMs += chunkMs;
    if (loud) {
      this.speechMs += chunkMs;
      this.silenceMs = 0;
    } else {
      this.silenceMs += chunkMs;
    }
    if (this.totalMs >= this.config.maxUtteranceMs) {
      this.reset();
      return "max_length";
    }
    if (this.silenceMs >= this.config.endSilenceMs) {
      const enough = this.speechMs >= this.config.minSpeechMs;
      this.reset();
      return enough ? "utterance_end" : "discard";
    }
    return "none";
  }
}

export interface BargeInConfig {
  /** Absolute level the user's voice must exceed while Nexus is speaking (echo cancellation removes most playback). */
  minLevel: number;
  /** Continuous loud audio needed before Nexus is interrupted (filters clicks and residual echo). */
  minMs: number;
}

export const DEFAULT_BARGE_IN: BargeInConfig = { minLevel: 0.05, minMs: 280 };

/**
 * Decides when the user is talking over Nexus's spoken reply. Fed one RMS level per mic chunk while the reply
 * plays; returns true once the user has been loud for long enough. Pure state, unit-tested directly.
 */
export class BargeInDetector {
  private loudMs = 0;
  private readonly config: BargeInConfig;

  constructor(config: Partial<BargeInConfig> = {}) {
    this.config = { ...DEFAULT_BARGE_IN, ...config };
  }

  reset(): void {
    this.loudMs = 0;
  }

  push(level: number, chunkMs: number): boolean {
    if (level >= this.config.minLevel) this.loudMs += chunkMs;
    else this.loudMs = Math.max(0, this.loudMs - chunkMs);
    if (this.loudMs >= this.config.minMs) {
      this.loudMs = 0;
      return true;
    }
    return false;
  }
}
