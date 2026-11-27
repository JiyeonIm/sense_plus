// Exercise the app and inline QR bootstrap with browser and transport doubles.
import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

const frontend = new URL('../frontend/', import.meta.url);
const [html, uiSource, appSource] = await Promise.all(
  ['index.html', 'ui.js', 'app.js'].map(name => readFile(new URL(name, frontend), 'utf8')),
);
const qrScript = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const uiScript = uiSource.replace(/^export /gm, '');
const appScript = appSource.replace(/^import [^\r\n]*\r?\n/gm, '');
const QR = 'A1B2C3D4E5F6';
const STARTUP = '센서 플러스가 실행되었습니다.';
const OBSERVATION = '화면 중앙에 의자가 보입니다.';
const tick = () => new Promise(resolve => setImmediate(resolve));

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

async function until(match, label) {
  const deadline = performance.now() + 1500;
  while (!match()) {
    assert.ok(performance.now() < deadline, label);
    await tick();
  }
}

function target(properties = {}) {
  const handlers = new Map();
  return {
    ...properties,
    addEventListener(name, handler) {
      if (!handlers.has(name)) handlers.set(name, []);
      handlers.get(name).push(handler);
    },
    emit(name, detail = {}) {
      return Promise.all((handlers.get(name) || []).map(handler => handler({ target: this, detail })));
    },
  };
}

function cameraStream() {
  const track = () => ({ readyState: 'live', enabled: true, stop() { this.readyState = 'ended'; } });
  const video = track();
  const mic = track();
  return { video, mic, getTracks: () => [video, mic], getAudioTracks: () => [mic], getVideoTracks: () => [video] };
}

function app({ hash = `#pair=${QR}`, cachedPairing = '', hidden = false, autoplay = true, gestureAudio = true,
  permitMedia = async () => {}, voiceWakeAvailable = true, healthDelay = null, audioDelay = null, voice = true } = {}) {
  const log = [];
  const getCalls = name => log.filter(item => item.name === name);
  const elements = new Map();
  const activation = { isActive: false };
  const element = id => {
    if (!elements.has(id)) {
      const child = { setAttribute() {}, textContent: '' };
      elements.set(id, target({
        id, dataset: {}, hidden: false, disabled: false, classList: { toggle() {} }, setAttribute() {},
        querySelector: () => child, lastChild: child,
        async play() { this.paused = false; }, pause() { this.paused = true; },
        focus() { document.activeElement = this; }, closest() { return this.hidden ? this : null; },
      }));
    }
    return elements.get(id);
  };
  const document = target({
    hidden, body: element('body'), activeElement: null,
    getElementById: element, querySelectorAll: () => [],
  });
  const location = { hash, pathname: '/', search: '' };
  const history = { state: null, replaceState() { location.hash = ''; } };
  const window = target({ isSecureContext: true, RTCPeerConnection: class {} });
  const streams = [];
  const navigator = {
    userActivation: activation,
    permissions: { async query() { return { state: 'granted' }; } },
    mediaDevices: { async getUserMedia() {
      log.push({name: 'getUserMedia', active: activation.isActive});
      await permitMedia(getCalls('getUserMedia').length);
      const stream = cameraStream();
      streams.push(stream);
      return stream;
    } },
  };

  const health = {
    mode: 'live', rtc_available: true, answer_audio_http: true,
    ai_configured: true, pairing_required: true, voice_wake_available: voiceWakeAvailable,
  };
  const idle = { mode: 'scene', last_video_age_ms: 1, video_frames: 1, ai_configured: true };
  let client;
  class PairingError extends Error {}
  class SenseTransport {
    constructor() {
      this.setPairingCode(cachedPairing);
      this.connected = false;
      this.usesHttpAnswerAudio = true;
      this.events = target();
      this.sessionCount = 0;
      client = this;
    }
    addEventListener(...args) { this.events.addEventListener(...args); }
    get hasPairingCode() { return Boolean(this.pairingCode); }
    setPairingCode(code) { this.pairingCode = /^[a-f0-9]{12}$/i.test(code) ? code.toUpperCase() : ''; }
    async checkHealth() {
      log.push({name: 'health'});
      if (healthDelay) await healthDelay;
      this.health = health;
      return health;
    }
    async connect(stream) {
      log.push({name: 'connect', code: this.pairingCode, mic: stream.mic.enabled});
      if (!this.hasPairingCode) throw new PairingError('QR 연결 필요');
      this.connected = true;
      this.sessionId = `session-${++this.sessionCount}`;
      await this.events.emit('status', { state: 'connected', health });
      await this.events.emit('state', idle);
    }
    async disconnect() {
      log.push({name: 'disconnect'});
      this.connected = false;
      this.sessionId = null;
    }
    async control(action, mode, resultId) {
      log.push({name: action, mode, resultId, session: this.sessionId});
      if (action === 'guide.start') {
        const state = { ...idle, guide_active: true, voice_wake_enabled: false, voice_wake_phase: 'off' };
        await this.events.emit('state', state);
        return state;
      }
      if (action === 'voice.enable') {
        const state = {
          ...idle, guide_active: true, voice_wake_enabled: true, voice_wake_phase: 'playing',
          latest_result: {
            id: `startup-${this.sessionCount}`, source: 'startup', mode: 'scene', text: STARTUP,
            kind: 'info', audio_available: true, audio_source: 'http-wav',
          },
        };
        await this.events.emit('state', state);
        return state;
      }
      return idle;
    }
    async showVisualObservation() {
      await this.events.emit('state', {
        ...idle, guide_active: true, voice_wake_enabled: false, voice_wake_phase: 'off', current_turn_id: 'scene-1',
        latest_result: {
          id: 'observation-1', source: 'ai', mode: 'scene', turn_id: 'scene-1', scene_observation: true,
          text: OBSERVATION, kind: 'info', audio_available: true, audio_source: 'http-wav',
        },
      });
    }
    async getAnswerAudio(id) {
      log.push({name: 'answer-audio', id});
      return new ArrayBuffer(8);
    }
  }

  class AnswerAudio {
    supported = true;
    active = false;
    async unlock() {
      const hasGesture = activation.isActive;
      log.push({name: 'unlock', active: hasGesture, connected: client.connected});
      if (audioDelay && !hasGesture) await audioDelay;
      if (autoplay || (gestureAudio && hasGesture)) this.active = true;
      return this.active;
    }
    async play(load, isAllowed, { onStart }) {
      if (!this.active) return 'blocked';
      if (!isAllowed()) return 'cancelled';
      await load(new AbortController().signal);
      if (!isAllowed()) return 'cancelled';
      onStart();
      return 'playing';
    }
    stop() {}
    async close() {}
  }

  runInNewContext(`${qrScript}\n${uiScript}\n${appScript}`, {
    window, document, location, history, navigator, performance, setTimeout, clearTimeout,
    requestAnimationFrame: callback => callback(),
    localStorage: { getItem: () => JSON.stringify({voice}) },
    SenseTransport, PairingError, AnswerAudio, PAIRING_REQUIRED_MESSAGE: 'QR 연결 필요',
  }, { filename: 'qr-app.vm.js' });

  return {
    element, getCalls, log, streams, location, client,
    async scan(code) { location.hash = `#pair=${code}`; await window.emit('hashchange'); },
    async visibility(isHidden) { document.hidden = isHidden; await document.emit('visibilitychange'); },
    tap(id) {
      const button = element(id);
      assert.equal(button.hidden, false, `${id} should be visible`);
      assert.equal(button.disabled, false, `${id} should be enabled`);
      activation.isActive = true;
      try { return button.emit('click'); }
      finally { activation.isActive = false; }
    },
  };
}

test('fresh QR connects with muted microphone before trying audio, then starts exactly once', async () => {
  const page = app();
  await until(() => page.getCalls('answer-audio').length === 1, 'opening audio should load automatically');
  assert.equal(page.location.hash, '');
  assert.deepEqual(page.getCalls('getUserMedia'), [{name: 'getUserMedia', active: false}]);
  assert.deepEqual(page.getCalls('connect'), [{name: 'connect', code: QR, mic: false}]);
  assert.ok(page.log.findIndex(item => item.name === 'connect') < page.log.findIndex(item => item.name === 'unlock'));
  assert.equal(page.getCalls('unlock')[0].connected, true);
  assert.equal(page.getCalls('guide.start').length, 0);
  assert.equal(page.getCalls('voice.enable').length, 1);
  assert.deepEqual(page.getCalls('answer-audio'), [{name: 'answer-audio', id: 'startup-1'}]);
  assert.equal(page.element('resultTitle').textContent, STARTUP);
  assert.equal(page.streams[0].mic.enabled, false);
});

test('autoplay blocked starts visual guidance and lets the same muted session play the opener after a tap', async () => {
  const page = app({autoplay: false});
  await until(() => page.getCalls('guide.start').length === 1 && page.element('guideActionLabel').textContent === '음성 안내 시작'
    && page.element('guideAction').hidden === false, 'audio gesture fallback should be visible');
  assert.equal(page.client.connected, true);
  assert.equal(page.streams[0].mic.enabled, false);
  assert.equal(page.getCalls('voice.enable').length, 0);
  assert.equal(page.getCalls('guide.start')[0].mode, 'scene');
  await page.client.showVisualObservation();
  await tick();
  assert.equal(page.element('resultTitle').textContent, OBSERVATION);
  assert.equal(page.element('guideActionLabel').textContent, '음성 안내 시작');
  assert.equal(page.getCalls('answer-audio').length, 0);
  await page.tap('guideAction');
  await until(() => page.getCalls('answer-audio').length === 1, 'one tap should play the startup WAV');
  assert.equal(page.getCalls('getUserMedia').length, 1);
  assert.equal(page.getCalls('connect').length, 1);
  assert.equal(page.getCalls('guide.start').length, 1);
  assert.equal(page.getCalls('voice.enable').length, 1);
  assert.equal(page.getCalls('voice.enable')[0].session, 'session-1');
  assert.deepEqual(page.getCalls('answer-audio'), [{name: 'answer-audio', id: 'startup-1'}]);
  assert.ok(page.getCalls('unlock').some(attempt => attempt.active === true));
  assert.equal(page.element('resultTitle').textContent, STARTUP);
});

test('a tap that cannot unlock audio keeps visual guidance and never arms the microphone', async () => {
  const page = app({autoplay: false, gestureAudio: false});
  await until(() => page.getCalls('guide.start').length === 1 && page.element('guideAction').hidden === false,
    'visual guidance should have a visible audio retry');
  await page.client.showVisualObservation();
  await page.tap('guideAction');
  assert.equal(page.getCalls('guide.start').length, 1);
  assert.equal(page.getCalls('connect').length, 1);
  assert.equal(page.getCalls('voice.enable').length, 0);
  assert.equal(page.streams[0].mic.enabled, false);
  assert.equal(page.element('resultTitle').textContent, OBSERVATION);
  assert.equal(page.element('guideActionLabel').textContent, '음성 안내 시작');
  assert.equal(page.element('guideAction').hidden, false);
});

test('absent, invalid and cached-only pairing never trigger automatic camera permission', async () => {
  for (const options of [{hash: ''}, {hash: '#pair=invalid'}, {hash: '', cachedPairing: QR}]) {
    const page = app(options);
    await tick();
    assert.equal(page.getCalls('getUserMedia').length, 0);
    assert.equal(page.getCalls('connect').length, 0);
  }
});

test('a fresh QR respects disabled voice and unavailable local voice service', async () => {
  for (const options of [{voice: false}, {voiceWakeAvailable: false}]) {
    const page = app(options);
    await tick();
    assert.equal(page.getCalls('getUserMedia').length, 0);
    assert.equal(page.getCalls('voice.enable').length, 0);
  }
});

test('a valid QR received in a hidden tab starts once after it becomes visible', async () => {
  const page = app({hidden: true});
  await tick();
  assert.equal(page.getCalls('getUserMedia').length, 0);
  await page.visibility(false);
  await until(() => page.getCalls('voice.enable').length === 1, 'foreground should consume the QR');
  await page.visibility(true);
  await page.visibility(false);
  await tick();
  assert.equal(page.getCalls('getUserMedia').length, 1);
  assert.equal(page.getCalls('voice.enable').length, 1);
});

test('a denied camera request stays stopped and keeps a reachable manual start', async () => {
  const denied = Object.assign(new Error('denied'), {name: 'NotAllowedError'});
  const page = app({permitMedia: async attempt => { if (attempt === 1) throw denied; }});
  await until(() => /권한이 허용되지/.test(page.element('statusMessage').textContent), 'permission denial should be shown');
  await page.visibility(true);
  await page.visibility(false);
  await tick();
  assert.equal(page.getCalls('getUserMedia').length, 1);
  assert.equal(page.getCalls('connect').length, 0);
  await page.tap('guideAction');
  await until(() => page.getCalls('voice.enable').length === 1, 'manual start should retry device access');
  assert.equal(page.getCalls('getUserMedia').length, 2);
  assert.equal(page.getCalls('connect').length, 1);
});

test('a manual tap during the initial health request does not start a second QR session', async () => {
  const health = deferred();
  const page = app({autoplay: false, healthDelay: health.promise});
  await until(() => page.getCalls('health').length === 1, 'health should be pending');
  const tapped = page.tap('guideAction');
  health.resolve();
  await tapped;
  await until(() => page.getCalls('answer-audio').length === 1, 'the tap should activate the opener');
  assert.equal(page.getCalls('getUserMedia').length, 1);
  assert.equal(page.getCalls('connect').length, 1);
  assert.equal(page.getCalls('voice.enable').length, 1);
});

test('Stop during permission wait discards late tracks and requires a fresh QR for autostart', async () => {
  const permission = deferred();
  const page = app({permitMedia: async attempt => { if (attempt === 1) await permission.promise; }});
  await until(() => page.getCalls('getUserMedia').length === 1, 'the first QR should ask for permission');
  assert.equal(page.element('sessionActions').hidden, false);
  await page.tap('stopAction');
  permission.resolve();
  await tick();
  assert.equal(page.streams[0].video.readyState, 'ended');
  assert.equal(page.streams[0].mic.readyState, 'ended');
  await page.visibility(true);
  await page.visibility(false);
  await tick();
  assert.equal(page.getCalls('getUserMedia').length, 1);
  assert.equal(page.getCalls('connect').length, 0);
  await page.scan(QR);
  await until(() => page.getCalls('voice.enable').length === 1, 'another QR should start a new attempt');
  assert.equal(page.getCalls('getUserMedia').length, 2);
  assert.equal(page.getCalls('connect').length, 1);
});

test('Stop during audio unlock prevents late guidance or voice activation', async () => {
  const audio = deferred();
  const page = app({autoplay: false, audioDelay: audio.promise});
  await until(() => page.getCalls('unlock').length === 1, 'audio unlock should be pending');
  await page.tap('stopAction');
  audio.resolve();
  await tick();
  assert.equal(page.getCalls('guide.start').length, 0);
  assert.equal(page.getCalls('voice.enable').length, 0);
  assert.equal(page.client.connected, false);
  assert.equal(page.streams[0].video.readyState, 'ended');
  assert.equal(page.streams[0].mic.readyState, 'ended');
});
