/**
 * PCM helpers for the Nexus voice endpoint, which accepts exactly one format:
 * 16-bit little-endian, mono, 16 kHz WAV (the backend's "linear16").
 */

export const TARGET_SAMPLE_RATE = 16_000;

/** Concatenate captured Float32 chunks into one buffer. */
export function concatChunks(chunks: Float32Array[]): Float32Array {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Float32Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

/** Resample by box-averaging (downsampling) or linear interpolation (upsampling). */
export function resample(input: Float32Array, fromRate: number, toRate: number = TARGET_SAMPLE_RATE): Float32Array {
  if (fromRate === toRate || input.length === 0) return input.slice();
  const ratio = fromRate / toRate;
  const length = Math.floor(input.length / ratio);
  const out = new Float32Array(length);
  if (ratio > 1) {
    for (let i = 0; i < length; i++) {
      const start = Math.floor(i * ratio);
      const end = Math.min(input.length, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end; j++) sum += input[j];
      out[i] = end > start ? sum / (end - start) : input[start] ?? 0;
    }
  } else {
    for (let i = 0; i < length; i++) {
      const pos = i * ratio;
      const j = Math.floor(pos);
      const frac = pos - j;
      out[i] = (input[j] ?? 0) * (1 - frac) + (input[j + 1] ?? input[j] ?? 0) * frac;
    }
  }
  return out;
}

/** Encode mono float samples (-1..1) as a 16-bit PCM WAV file. */
export function encodeWav(samples: Float32Array, sampleRate: number = TARGET_SAMPLE_RATE): ArrayBuffer {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const write = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };
  write(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  write(8, "WAVE");
  write(12, "fmt ");
  view.setUint32(16, 16, true); // PCM chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  write(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}

/** Captured chunks at ``rate`` → a 16 kHz mono WAV blob ready for upload. */
export function toUploadWav(chunks: Float32Array[], rate: number): Blob {
  return new Blob([encodeWav(resample(concatChunks(chunks), rate))], { type: "audio/wav" });
}

export function base64ToArrayBuffer(base64: string): ArrayBuffer {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

/** Root-mean-square level of one chunk. */
export function rms(chunk: Float32Array): number {
  if (chunk.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
  return Math.sqrt(sum / chunk.length);
}
