import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../frontend/answer-audio.js', import.meta.url), 'utf8');
const { AnswerAudio } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function fakeContext() {
  return {
    state: 'suspended',
    destination: {},
    sources: [],
    resumeCalls: 0,
    resume() { this.resumeCalls++; this.state = 'running'; return Promise.resolve(); },
    async close() { this.state = 'closed'; },
    async decodeAudioData(bytes) { return { bytes }; },
    createBufferSource() {
      const source = {
        connect(destination) { this.destination = destination; },
        start() { this.started = true; },
        stop() { this.stopped = true; },
        disconnect() { this.disconnected = true; },
      };
      this.sources.push(source);
      return source;
    },
  };
}

test('unlock resumes from the gesture and playback waits for that resume', async () => {
  const context = fakeContext();
  const allowed = deferred();
  context.resume = () => { context.resumeCalls++; return allowed.promise.then(() => { context.state = 'running'; }); };
  const player = new AnswerAudio(() => context);
  const unlock = player.unlock();
  assert.equal(context.resumeCalls, 1);
  const wave = new ArrayBuffer(12);
  const result = player.play(async () => wave);
  assert.equal(context.sources.length, 0);

  allowed.resolve();
  assert.equal(await unlock, true);
  assert.equal(await result, 'playing');
  assert.equal(context.sources[0].buffer.bytes, wave);
  assert.equal(context.sources[0].destination, context.destination);
  assert.equal(context.sources[0].started, true);
});

test('blocked contexts are retryable without loading audio until unlocked', async () => {
  const context = fakeContext();
  const player = new AnswerAudio(() => context);
  let calls = 0;
  const load = async () => { calls++; return new ArrayBuffer(12); };
  assert.equal(await player.play(load), 'blocked');
  assert.equal(calls, 0);
  await player.unlock();
  context.state = 'interrupted';
  assert.equal(await player.play(load), 'blocked');
  assert.equal(calls, 0);
  assert.equal(await player.unlock(), true);
  assert.equal(await player.play(load), 'playing');
  assert.equal(calls, 1);
});

test('stop aborts an outstanding load and a late download cannot play', async () => {
  const context = fakeContext();
  const player = new AnswerAudio(() => context);
  await player.unlock();
  const waiting = deferred();
  const entered = deferred();
  let firstSignal;
  const old = player.play(signal => { firstSignal = signal; entered.resolve(); return waiting.promise; });
  await entered.promise;
  assert.equal(firstSignal.aborted, false);
  player.stop();
  assert.equal(await old, 'cancelled');
  assert.equal(firstSignal.aborted, true);
  assert.equal(await player.play(async () => new ArrayBuffer(12)), 'playing');
  waiting.resolve(new ArrayBuffer(12));
  await Promise.resolve();
  assert.equal(context.sources.length, 1);
});

test('playback rechecks the current answer and context after decoding', async () => {
  const context = fakeContext();
  const player = new AnswerAudio(() => context);
  await player.unlock();
  let current = true;
  context.decodeAudioData = async () => { current = false; return {}; };
  assert.equal(await player.play(async () => new ArrayBuffer(12), () => current), 'cancelled');
  context.decodeAudioData = async () => { context.state = 'interrupted'; return {}; };
  assert.equal(await player.play(async () => new ArrayBuffer(12)), 'blocked');
  assert.equal(context.sources.length, 0);
});

test('callback decoding is supported and media failures are reported separately', async () => {
  const context = fakeContext();
  const player = new AnswerAudio(() => context);
  await player.unlock();
  assert.equal(await player.play(async () => { throw new Error('request failed'); }), 'failed');
  context.decodeAudioData = () => Promise.reject(new Error('invalid WAV'));
  assert.equal(await player.play(async () => new ArrayBuffer(12)), 'failed');
  context.decodeAudioData = (bytes, success) => { success({ bytes }); };
  const firstWave = new ArrayBuffer(12);
  assert.equal(await player.play(async () => firstWave), 'playing');
  const first = context.sources[0];
  assert.equal(first.buffer.bytes, firstWave);
  assert.equal(first.started, true);
  const secondWave = new ArrayBuffer(24);
  assert.equal(await player.play(async () => secondWave), 'playing');
  assert.equal(context.sources[1].buffer.bytes, secondWave);
  assert.equal(context.sources[1].started, true);
  assert.equal(first.stopped, true);
  assert.equal(first.disconnected, true);
  await player.close();
  assert.equal(context.sources[1].stopped, true);
  assert.equal(context.state, 'closed');
});
