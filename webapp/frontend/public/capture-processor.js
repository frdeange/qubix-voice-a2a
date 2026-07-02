// Microphone capture worklet: buffers mono Float32 samples and posts them as
// PCM16 (Int16) chunks (~100 ms) to the main thread.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = [];
    this._target = 2400; // 100 ms at 24 kHz
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) {
      const channel = input[0];
      for (let i = 0; i < channel.length; i++) {
        this._buffer.push(channel[i]);
      }
      if (this._buffer.length >= this._target) {
        const pcm = new Int16Array(this._buffer.length);
        for (let i = 0; i < this._buffer.length; i++) {
          const s = Math.max(-1, Math.min(1, this._buffer[i]));
          pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
        this._buffer = [];
      }
    }
    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
