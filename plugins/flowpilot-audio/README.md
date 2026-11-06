# flowpilot-audio

Audio processing nodes for [FlowPilot](https://github.com/darkdarin/flowpilot), put between the
client input and voice activity detection or speech-to-text:

- `audio.gain` — a fixed gain in dB, with saturation instead of wrap-around;
- `audio.agc` — automatic gain control: brings speech to a target level, leaves quiet background
  alone and never clips (peak limiter).

Pure Python, no native dependencies; 16-bit PCM.
