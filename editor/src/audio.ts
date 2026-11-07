// Audio for turns started from the editor: a microphone recording or a file, as the 16 kHz mono
// 16-bit WAV that speech nodes expect (the runtime's turn endpoint takes WAV).

const RATE = 16000;

/** Decode any audio the browser can play (WAV, MP3, OGG, WebM) and make it a mono WAV
 * (16 kHz unless `RATE` is given). */
export async function toWav(data: Blob, rate = RATE): Promise<Blob> {
  const RATE = rate;
  const ctx = new AudioContext();
  let decoded: AudioBuffer;
  try {
    decoded = await ctx.decodeAudioData(await data.arrayBuffer());
  } finally {
    void ctx.close();
  }
  const frames = Math.max(1, Math.ceil(decoded.duration * RATE));
  const offline = new OfflineAudioContext(1, frames, RATE);
  const source = offline.createBufferSource();
  source.buffer = decoded;
  source.connect(offline.destination);
  source.start();
  const samples = (await offline.startRendering()).getChannelData(0);

  const out = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const text = (at: number, value: string) => [...value].forEach((c, i) => out.setUint8(at + i, c.charCodeAt(0)));
  text(0, "RIFF");
  out.setUint32(4, 36 + samples.length * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  out.setUint32(16, 16, true);
  out.setUint16(20, 1, true); // PCM
  out.setUint16(22, 1, true); // mono
  out.setUint32(24, RATE, true);
  out.setUint32(28, RATE * 2, true);
  out.setUint16(32, 2, true);
  out.setUint16(34, 16, true);
  text(36, "data");
  out.setUint32(40, samples.length * 2, true);
  samples.forEach((s, i) => out.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 0x7fff, true));
  return new Blob([out.buffer], { type: "audio/wav" });
}

/** The microphone is available only in a secure context (HTTPS or localhost). */
export const micAvailable = window.isSecureContext && !!navigator.mediaDevices?.getUserMedia;

/** Records the microphone until ``stop()``, which returns the recording. */
export async function startRecording(): Promise<{ stop: () => Promise<Blob> }> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
  const recorder = new MediaRecorder(stream);
  const chunks: Blob[] = [];
  recorder.ondataavailable = (e) => chunks.push(e.data);
  recorder.start();
  return {
    stop: () =>
      new Promise((resolve) => {
        recorder.onstop = () => {
          stream.getTracks().forEach((t) => t.stop());
          resolve(new Blob(chunks, { type: recorder.mimeType }));
        };
        recorder.stop();
      }),
  };
}

/** A short sound of one's own for a node setting: a mono 22 kHz WAV data URL and its length;
 * longer than `maxSeconds` is refused. */
export async function soundDataUrl(file: Blob, maxSeconds: number): Promise<{ url: string; seconds: number }> {
  const wav = await toWav(file, 22050);
  const seconds = (wav.size - 44) / (22050 * 2);
  if (seconds > maxSeconds) throw new Error(`longer than ${maxSeconds} s`);
  const url = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^;]*;/, "data:audio/wav;"));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(wav);
  });
  return { url, seconds };
}
