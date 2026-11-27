const UNLOCK_TIMEOUT_MS = 1500;

export class AnswerAudio {
  constructor(createContext) {
    const Context = globalThis.AudioContext || globalThis.webkitAudioContext;
    this._createContext = createContext || (Context ? () => new Context() : null);
    this._context = null;
    this._unlock = null;
    this._pending = null;
    this._source = null;
    this._finishSource = null;
  }

  get supported() { return Boolean(this._createContext); }

  unlock() {
    if (!this.supported) return Promise.resolve(false);
    try {
      if (!this._context || this._context.state === 'closed') this._context = this._createContext();
      const context = this._context;
      if (context.state === 'running') return Promise.resolve(true);

      // Call resume while the tap is still active. A suspended context may
      // leave resume pending, so callers still get a result within a bound.
      const resume = context.resume();
      const attempt = new Promise(resolve => {
        const timer = setTimeout(() => resolve(false), UNLOCK_TIMEOUT_MS);
        Promise.resolve(resume).then(
          () => { clearTimeout(timer); resolve(this._context === context && context.state === 'running'); },
          () => { clearTimeout(timer); resolve(false); },
        );
      });
      this._unlock = attempt;
      void attempt.then(() => { if (this._unlock === attempt) this._unlock = null; });
      return attempt;
    } catch {
      return Promise.resolve(false);
    }
  }

  async play(load, isAllowed = () => true, playback = {}) {
    this.stop();
    const controller = new AbortController();
    this._pending = controller;
    const signal = controller.signal;
    const aborted = new Promise(resolve => {
      signal.addEventListener('abort', () => resolve('cancelled'), { once: true });
    });
    try {
      return await Promise.race([this._play(load, isAllowed, signal, playback), aborted]);
    } finally {
      if (this._pending === controller) this._pending = null;
    }
  }

  async _play(load, isAllowed, signal, {onStart, onEnd}) {
    const cancelled = () => signal.aborted || !isAllowed();
    let source;
    try {
      if (cancelled()) return 'cancelled';
      if (!this.supported) return 'failed';
      if (this._unlock) await this._unlock;
      if (cancelled()) return 'cancelled';
      const context = this._context;
      if (context?.state !== 'running') return 'blocked';

      const bytes = await load(signal);
      if (cancelled()) return 'cancelled';
      const buffer = await new Promise((resolve, reject) => {
        // WebKit implementations have supported both callback and promise forms.
        const decoded = context.decodeAudioData(bytes, resolve, reject);
        if (decoded?.then) decoded.then(resolve, reject);
      });
      if (cancelled()) return 'cancelled';
      if (context.state !== 'running') return 'blocked';

      source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      const finish = () => {
        if (this._source !== source) return;
        this._source = null;
        this._finishSource = null;
        source.onended = null;
        source.disconnect();
        onEnd?.();
      };
      this._source = source;
      this._finishSource = finish;
      source.onended = finish;
      source.start();
      onStart?.();
      return 'playing';
    } catch {
      if (cancelled()) return 'cancelled';
      if (source) {
        if (this._source === source) {
          this._source = null;
          this._finishSource = null;
        }
        source.onended = null;
        source.disconnect();
      }
      return this._context && this._context.state !== 'running' ? 'blocked' : 'failed';
    }
  }

  stop() {
    if (this._pending) this._pending.abort();
    this._pending = null;
    const source = this._source;
    if (source) {
      source.onended = null;
      try { source.stop(); } catch { /* It may not have started. */ }
      this._finishSource?.();
    }
  }

  async close() {
    this.stop();
    const context = this._context;
    this._context = null;
    this._unlock = null;
    if (context && context.state !== 'closed') {
      try { await context.close(); } catch { /* The browser may already be closing the page. */ }
    }
  }
}
