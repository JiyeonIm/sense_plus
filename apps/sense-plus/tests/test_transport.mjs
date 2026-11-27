// Optional Node 22+ tests for browser connection cancellation races.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('../frontend/rtc-client.js', import.meta.url), 'utf8');
const { PairingError, SenseTransport } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
globalThis.RTCPeerConnection = class {};
const media = { getVideoTracks: () => [{ readyState: 'live' }] };
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

test('stop during old session cleanup prevents a new session request', async () => {
  const client = new SenseTransport();
  const cleanup = deferred();
  let requests = 0;
  client._sessionId = 'old-session';
  client._token = 'old-token';
  client._deleteSession = id => id ? cleanup.promise : Promise.resolve();
  client._json = async () => { requests++; throw new Error('unexpected network request'); };
  const attempt = assert.rejects(client.connect(media), /취소/);
  await client.disconnect();
  cleanup.resolve();
  await attempt;
  assert.equal(requests, 0);
  assert.equal(client.sessionId, null);
});

test('immediate restart gets a new attempt while cancelled health is pending', async () => {
  const client = new SenseTransport();
  const healthStarted = deferred();
  const oldHealth = deferred();
  let healthCalls = 0;
  client._json = async path => {
    assert.equal(path, '/api/health');
    healthCalls++;
    if (healthCalls === 1) { healthStarted.resolve(); return oldHealth.promise; }
    return { rtc_available: false, detail: 'new attempt reached server' };
  };
  const first = assert.rejects(client.connect(media), /취소/);
  await healthStarted.promise;
  await client.disconnect();
  await assert.rejects(client.connect(media), /new attempt reached server/);
  oldHealth.resolve({ rtc_available: true });
  await first;
  assert.equal(healthCalls, 2);
  assert.equal(client.sessionId, null);
});

test('late state from a closed session cannot update the new session UI', async () => {
  const client = new SenseTransport();
  const reply = deferred();
  let states = 0;
  client.addEventListener('state', () => { states++; });
  client._sessionId = 'first';
  client._token = 'first-token';
  client._json = () => reply.promise;
  client._deleteSession = async () => {};
  const oldPoll = client._poll(client._generation);
  await client.disconnect();
  client._sessionId = 'second';
  client._token = 'second-token';
  reply.resolve({ latest_result: { text: 'obsolete instruction' } });
  await oldPoll;
  assert.equal(states, 0);
  assert.equal(client.sessionId, 'second');
  assert.equal(client._pollTimer, null);
  await client.disconnect();
});

test('QR fragment is removed at the first script and on a later scan in the same tab', async () => {
  const html = await readFile(new URL('../frontend/index.html', import.meta.url), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(script);
  assert.ok(script.index < html.indexOf('href="./styles.css"'));
  const handlers = new Map();
  const window = {addEventListener: (name, handler) => handlers.set(name, handler)};
  const location = {hash:'#pair=012345abcdef', pathname:'/', search:''};
  const history = {state:{}, replaceState(state, _title, url) {
    assert.equal(state, this.state);
    assert.equal(url, '/');
    location.hash = '';
  }};
  runInNewContext(script[1], {window, location, history});
  assert.equal(location.hash, '');
  assert.equal(window.sensePairingFromQr, '012345abcdef');
  location.hash = '#pair=abcdef012345';
  handlers.get('hashchange')();
  assert.equal(location.hash, '');
  assert.equal(window.sensePairingFromQr, 'abcdef012345');
  location.hash = '#guidePanel';
  handlers.get('hashchange')();
  assert.equal(location.hash, '#guidePanel');
});

test('tab QR survives reload and is sent and cleared only on the rejected pairing request', async t => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  const storage = new Map();
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable:true,
    value:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}
  });
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor);
    else delete globalThis.sessionStorage;
  });
  const first = new SenseTransport();
  first.setPairingCode('012345abcdef');
  const reloaded = new SenseTransport();
  assert.equal(reloaded.hasPairingCode, true);
  let rejectHealth = true;
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    requests.push([path, options.method, options.headers.get('X-Sense-Pairing')]);
    if (path === '/api/health' && !rejectHealth) {
      return Response.json({rtc_available:true,pairing_required:true});
    }
    return Response.json({detail:'forbidden'}, {status:403});
  });
  await assert.rejects(reloaded.connect(media), /forbidden/);
  assert.equal(reloaded.hasPairingCode, true);
  rejectHealth = false;
  await assert.rejects(reloaded.connect(media), error => error instanceof PairingError && /새 QR 코드/.test(error.message));
  assert.equal(reloaded.hasPairingCode, false);
  assert.equal(new SenseTransport().hasPairingCode, false);
  assert.deepEqual(requests, [
    ['/api/health', undefined, null],
    ['/api/health', undefined, null],
    ['/api/sessions', 'POST', '012345ABCDEF']
  ]);
});

test('QR is usable in memory when browser settings block tab storage', t => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable:true,
    get() { throw new Error('Tab storage denied'); }
  });
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor);
    else delete globalThis.sessionStorage;
  });
  const client = new SenseTransport();
  assert.equal(client.hasPairingCode, false);
  client.setPairingCode('ABCDEF012345');
  assert.equal(client.hasPairingCode, true);
  client.setPairingCode('invalid');
  assert.equal(client.hasPairingCode, false);
});

test('answer audio is negotiated only with a capable server', async t => {
  const sessions = [];
  let advertised = false;
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    if (path === '/api/health') return Response.json({rtc_available: true, answer_audio_http: advertised});
    sessions.push(options.headers.get('X-Sense-Answer-Audio'));
    return Response.json({detail: 'fixture stopped after checking headers'}, {status: 409});
  });
  const client = new SenseTransport({answerAudioHttp: true});
  await assert.rejects(client.connect(media), /fixture/);
  advertised = true;
  await assert.rejects(client.connect(media), /fixture/);
  assert.deepEqual(sessions, [null, 'http']);
});

test('answer audio fetch carries session auth and drops a reply after disconnect', async t => {
  const client = new SenseTransport();
  client._pc = {connectionState: 'connected', getSenders: () => [], close() {}};
  client._sessionId = 'voice-session';
  client._token = 'voice-token';
  client._deleteSession = async () => {};
  const pending = deferred();
  const id = 'b'.repeat(32);
  t.mock.method(globalThis, 'fetch', (path, options) => {
    assert.equal(path, `/api/sessions/voice-session/audio/${id}.wav`);
    assert.equal(options.headers.Authorization, 'Bearer voice-token');
    assert.equal(options.cache, 'no-store');
    return pending.promise;
  });
  const receipt = assert.rejects(client.getAnswerAudio(id), /종료된 연결/);
  await client.disconnect(false);
  pending.resolve(new Response(new Uint8Array([82, 73, 70, 70])));
  await receipt;
});
