// AudioWorkletProcessor that converts mic input to 16-bit signed PCM and
// posts each block to the main thread. Runs on the audio rendering thread,
// so it does the Int16 conversion here rather than shipping raw Float32 and
// converting on the main thread — keeps the main thread free for WS I/O and
// UI updates.
//
// The AudioContext this is registered on is constructed with
// { sampleRate: 16000 } (see TestAgentPanel.tsx), which forces the browser
// to resample the mic's native capture rate down to 16kHz before any node
// in the graph — including this one — ever sees a sample. No resampling
// logic needed here; input is already 16kHz mono by the time it arrives.
class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channel = input[0]; // mono — only one input channel requested
    if (!channel || channel.length === 0) return true;

    const pcm16 = new Int16Array(channel.length);
    for (let i = 0; i < channel.length; i++) {
      const s = Math.max(-1, Math.min(1, channel[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
    return true;
  }
}

registerProcessor("pcm-capture-processor", PcmCaptureProcessor);
