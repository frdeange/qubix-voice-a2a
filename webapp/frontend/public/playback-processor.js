// Playback worklet: plays queued Float32 chunks. Send an ArrayBuffer of
// Float32 samples to enqueue, or the string "clear" to flush (barge-in).
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._queue = [];
    this._current = null;
    this._pos = 0;
    this.port.onmessage = (event) => {
      if (event.data === "clear") {
        this._queue = [];
        this._current = null;
        this._pos = 0;
      } else {
        this._queue.push(new Float32Array(event.data));
      }
    };
  }

  process(_inputs, outputs) {
    const output = outputs[0][0];
    for (let i = 0; i < output.length; i++) {
      if (!this._current || this._pos >= this._current.length) {
        this._current = this._queue.shift() || null;
        this._pos = 0;
      }
      output[i] = this._current ? this._current[this._pos++] : 0;
    }
    return true;
  }
}

registerProcessor("playback-processor", PlaybackProcessor);
