// Recordings of what nodes passed on (live debug's journal): played and downloaded through the
// authenticated API (an <audio> element cannot send the Authorization header). One recording plays
// at a time.

import { ref } from "vue";
import { t } from "./i18n";
import { api, flash } from "./store";

export const playing = ref<string | null>(null);
let player: HTMLAudioElement | null = null;
let playerUrl = "";

export function stopPlaying() {
  player?.pause();
  if (playerUrl) URL.revokeObjectURL(playerUrl);
  player = null;
  playerUrl = "";
  playing.value = null;
}

async function blobOf(file: string): Promise<Blob | null> {
  try {
    return await api.value!.recording(file);
  } catch {
    flash(t("debug.recordingGone"), "error");
    return null;
  }
}

export async function play(file: string) {
  const wasPlaying = playing.value === file;
  stopPlaying();
  if (wasPlaying) return;
  const blob = await blobOf(file);
  if (!blob) return;
  playerUrl = URL.createObjectURL(blob);
  player = new Audio(playerUrl);
  player.onended = stopPlaying;
  playing.value = file;
  await player.play();
}

export async function download(file: string) {
  const blob = await blobOf(file);
  if (!blob) return;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = file;
  link.click();
  URL.revokeObjectURL(link.href);
}
