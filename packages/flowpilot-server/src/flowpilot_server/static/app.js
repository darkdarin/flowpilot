// FlowPilot dev client: protocol v1 over WebSocket, microphone and text turns, speech playback.
"use strict";

const $ = (id) => document.getElementById(id);
const TARGET_RATE = 16000;
const FRAME_SAMPLES = 320; // 20 ms at 16 kHz

let ws = null;
let refSeq = 1;
let streamSeq = 1;
let conversationId = crypto.randomUUID();
const turns = new Map(); // turn_id -> view
const pendingRefs = new Map(); // ref -> view
const speechStreams = new Map(); // server stream id -> {view, rate, channels}
let mic = null; // active microphone capture
let playbackCtx = null;
let playbackAt = 0;

$("token").value = localStorage.getItem("flowpilot.token") || "";

function setStatus(text, isError = false) {
  $("status").textContent = text;
  $("status").className = isError ? "error" : "";
}

function setConnected(connected) {
  for (const id of ["text", "send", "mic", "reset", "graph"]) $(id).disabled = !connected;
  $("connect").textContent = connected ? "Disconnect" : "Connect";
}

function send(msg) {
  ws.send(JSON.stringify(msg));
}

function connect() {
  if (ws) {
    ws.close();
    return;
  }
  localStorage.setItem("flowpilot.token", $("token").value);
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${scheme}://${location.host}/api/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () =>
    send({ type: "hello", protocol: 1, token: $("token").value, client: { name: "browser" }, host_ops: [] });
  ws.onmessage = (event) => (typeof event.data === "string" ? onMessage(JSON.parse(event.data)) : onAudio(event.data));
  ws.onclose = () => {
    ws = null;
    stopMic(false);
    setConnected(false);
    setStatus("Disconnected.");
  };
}

function onMessage(msg) {
  switch (msg.type) {
    case "hello.ok": {
      const select = $("graph");
      select.innerHTML = "";
      for (const g of msg.graphs) select.add(new Option(g, g, false, g === msg.default_graph));
      setConnected(true);
      const missing = Object.entries(msg.missing_host_ops || {})
        .map(([g, ops]) => `${g}: ${ops.join(", ")}`)
        .join("; ");
      setStatus(missing ? `Connected. Graphs needing Home Assistant will degrade (${missing}).` : "Connected.");
      break;
    }
    case "turn.started": {
      const view = pendingRefs.get(msg.ref);
      pendingRefs.delete(msg.ref);
      if (view) {
        view.turnId = msg.turn_id;
        turns.set(msg.turn_id, view);
      }
      break;
    }
    case "turn.transcript": {
      const view = turns.get(msg.turn_id);
      if (!view) break;
      view.heard.textContent = msg.text;
      view.heard.className = msg.final ? "you" : "you partial";
      if (msg.final) {
        view.mark("final");
        if (mic && mic.turnId === msg.turn_id) stopMic(true); // the graph decided the phrase is over
      } else view.mark("partial");
      break;
    }
    case "turn.delta": {
      const view = turns.get(msg.turn_id);
      if (!view) break;
      view.mark("delta");
      view.answer.textContent += msg.text;
      break;
    }
    case "turn.response": {
      const view = turns.get(msg.turn_id);
      if (view) view.answer.textContent = msg.response.text;
      break;
    }
    case "stream.start": {
      const view = turns.get(msg.turn_id);
      speechStreams.set(msg.stream, { view, rate: msg.format.sample_rate, channels: msg.format.channels });
      break;
    }
    case "stream.end":
      speechStreams.delete(msg.stream);
      break;
    case "turn.end": {
      const view = turns.get(msg.turn_id);
      if (view) view.end(msg);
      if (mic && mic.turnId === msg.turn_id) stopMic(true);
      break;
    }
    case "error":
      setStatus(`Error: ${msg.message}`, true);
      if (msg.code === "auth") ws.close();
      break;
  }
}

function onAudio(buffer) {
  const stream = new DataView(buffer).getUint32(0);
  const info = speechStreams.get(stream);
  if (!info) return;
  if (info.view) info.view.mark("speech");
  const pcm = new Int16Array(buffer.slice(4));
  if (!playbackCtx) playbackCtx = new AudioContext();
  const frames = pcm.length / info.channels;
  const audio = playbackCtx.createBuffer(info.channels, frames, info.rate);
  for (let ch = 0; ch < info.channels; ch++) {
    const data = audio.getChannelData(ch);
    for (let i = 0; i < frames; i++) data[i] = pcm[i * info.channels + ch] / 32768;
  }
  const source = playbackCtx.createBufferSource();
  source.buffer = audio;
  source.connect(playbackCtx.destination);
  playbackAt = Math.max(playbackAt, playbackCtx.currentTime + 0.05);
  source.start(playbackAt);
  playbackAt += audio.duration;
}

function newTurnView(label) {
  const el = document.createElement("div");
  el.className = "turn";
  el.innerHTML = '<div class="you"></div><div class="answer"></div><div class="meta"></div>';
  $("turns").prepend(el);
  const view = {
    heard: el.children[0],
    answer: el.children[1],
    meta: el.children[2],
    t0: performance.now(),
    marks: {},
    mark(name) {
      if (!(name in this.marks)) this.marks[name] = Math.round(performance.now() - this.t0);
      this.render();
    },
    end(msg) {
      this.status = msg.status;
      this.render();
    },
    render() {
      const parts = Object.entries(this.marks).map(([k, v]) => `${k} ${v} ms`);
      if (this.status) parts.push(this.status);
      this.meta.textContent = parts.join(" · ");
    },
  };
  view.heard.textContent = label;
  return view;
}

function startTurn(input, label) {
  const ref = String(refSeq++);
  const view = newTurnView(label);
  pendingRefs.set(ref, view);
  send({
    type: "turn.start",
    ref,
    graph: $("graph").value,
    language: $("language").value || null,
    source: { kind: "browser" },
    external: { "browser.conversation_id": conversationId },
    input,
  });
  return { ref, view };
}

function sendText() {
  const text = $("text").value.trim();
  if (!text) return;
  $("text").value = "";
  startTurn({ utterance: { text } }, text);
}

const WORKLET = `
class Capture extends AudioWorkletProcessor {
  process(inputs) {
    if (inputs[0] && inputs[0][0]) this.port.postMessage(inputs[0][0].slice(0));
    return true;
  }
}
registerProcessor("capture", Capture);`;

async function startMic() {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: false, autoGainControl: false },
  });
  const ctx = new AudioContext();
  await ctx.resume(); // some browsers start contexts suspended
  const url = URL.createObjectURL(new Blob([WORKLET], { type: "text/javascript" }));
  await ctx.audioWorklet.addModule(url);
  const node = new AudioWorkletNode(ctx, "capture");
  const sourceNode = ctx.createMediaStreamSource(stream);
  sourceNode.connect(node);

  const streamId = streamSeq++;
  const { view, ref } = startTurn(
    { audio: { stream: streamId, format: { encoding: "s16le", sample_rate: TARGET_RATE, channels: 1 } } },
    "…",
  );
  mic = { stream, ctx, node, streamId, view, ref, turnId: null, pending: [], position: 0 };
  const ratio = ctx.sampleRate / TARGET_RATE;
  node.port.onmessage = (event) => {
    if (!mic) return;
    if (!mic.turnId) mic.turnId = view.turnId;
    // Downsample by averaging the input samples that fall into each output sample.
    const input = event.data;
    for (let i = 0; i < input.length; i++) {
      mic.acc = (mic.acc || 0) + input[i];
      mic.count = (mic.count || 0) + 1;
      mic.position += 1;
      if (mic.position >= ratio) {
        mic.position -= ratio;
        mic.pending.push(mic.acc / mic.count);
        mic.acc = 0;
        mic.count = 0;
      }
    }
    while (mic.pending.length >= FRAME_SAMPLES) {
      const frame = mic.pending.splice(0, FRAME_SAMPLES);
      const out = new DataView(new ArrayBuffer(4 + FRAME_SAMPLES * 2));
      out.setUint32(0, streamId);
      frame.forEach((s, i) => out.setInt16(4 + i * 2, Math.max(-1, Math.min(1, s)) * 32767, true));
      if (ws && view.turnId) ws.send(out.buffer);
    }
  };
  $("mic").textContent = "■ Stop";
  $("mic").classList.add("recording");
}

function stopMic(sendEnd) {
  if (!mic) return;
  const m = mic;
  mic = null;
  m.node.port.onmessage = null;
  m.stream.getTracks().forEach((t) => t.stop());
  m.ctx.close();
  m.view.mark("mic_off");
  if (sendEnd && ws) send({ type: "stream.end", stream: m.streamId });
  $("mic").textContent = "🎙 Talk";
  $("mic").classList.remove("recording");
}

$("connect").onclick = connect;
$("send").onclick = sendText;
$("text").onkeydown = (e) => e.key === "Enter" && sendText();
$("mic").onclick = () => (mic ? stopMic(true) : startMic().catch((e) => setStatus(`Microphone: ${e.message}`, true)));
$("reset").onclick = () => {
  conversationId = crypto.randomUUID();
  setStatus("New conversation.");
};
