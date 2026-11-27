/** Browser-side FastRTC transport. No AI credentials or media are stored here. */
const PAIRING_KEY = 'sense-pairing-code';
export const PAIRING_REQUIRED_MESSAGE = 'PC 관제 화면의 QR 코드를 스캔해 주세요.';
const PAIRING_REJECTED_MESSAGE = '연결 QR이 만료됐거나 바뀌었어요. PC 관제 화면의 새 QR 코드를 스캔해 주세요.';

export class PairingError extends Error {}

function normalizePairingCode(code) {
  return typeof code === 'string' && /^[0-9a-f]{12}$/i.test(code) ? code.toUpperCase() : '';
}

export class SenseTransport extends EventTarget {
  constructor({ answerAudioHttp = false } = {}) {
    super();
    this._answerAudioHttp = answerAudioHttp;
    this._pairingCode = '';
    try { this._pairingCode = normalizePairingCode(sessionStorage.getItem(PAIRING_KEY)); }
    catch { /* Pairing can also remain in memory for this page. */ }
    this._pc = null;
    this._sessionId = null;
    this._token = null;
    this._health = null;
    this._generation = 0;
    this._pollTimer = null;
    this._connectTask = null;
    this._pollErrors = 0;
    this._remoteStream = null;
  }

  get connected() { return this._pc?.connectionState === 'connected'; }
  get sessionId() { return this._sessionId; }
  get health() { return this._health; }
  get hasPairingCode() { return Boolean(this._pairingCode); }
  get usesHttpAnswerAudio() { return this._answerAudioHttp && this._health?.answer_audio_http === true; }

  setPairingCode(value) {
    this._pairingCode = normalizePairingCode(value);
    try {
      if (this._pairingCode) sessionStorage.setItem(PAIRING_KEY, this._pairingCode);
      else sessionStorage.removeItem(PAIRING_KEY);
    } catch { /* Pairing can also remain in memory for this page. */ }
  }

  _emit(name, detail) { this.dispatchEvent(new CustomEvent(name, { detail })); }
  _status(state, message) { this._emit('status', { state, message, health: this._health }); }

  async _json(path, options = {}, token = this._token, timeout = 12000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const headers = new Headers(options.headers || {});
      if (token) headers.set('Authorization', `Bearer ${token}`);
      if (options.body) headers.set('Content-Type', 'application/json');
      const response = await fetch(path, { ...options, headers, signal: controller.signal, cache: 'no-store' });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = typeof data?.detail === 'string' ? data.detail : data?.message;
        throw Object.assign(new Error(detail || `서버 요청을 처리하지 못했어요. (${response.status})`), {status: response.status});
      }
      if (!data) throw new Error('서버 응답 형식을 확인해 주세요.');
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('서버 응답이 늦어요. 연결 상태를 확인해 주세요.');
      throw error;
    } finally { clearTimeout(timer); }
  }

  async checkHealth() {
    this._health = await this._json('/api/health', {}, null, 6000);
    return this._health;
  }

  async connect(mediaStream) {
    if (this.connected) return this._health;
    if (this._connectTask) return this._connectTask;
    const task = this._connect(mediaStream);
    this._connectTask = task;
    try { return await task; }
    finally { if (this._connectTask === task) this._connectTask = null; }
  }

  async _connect(mediaStream) {
    if (!globalThis.RTCPeerConnection) throw new Error('이 브라우저에서 실시간 연결을 사용할 수 없어요.');
    if (!mediaStream?.getVideoTracks().some(track => track.readyState === 'live')) {
      throw new Error('먼저 카메라를 켜 주세요.');
    }
    const generation = ++this._generation;
    await this._closeActiveConnection();
    if (generation !== this._generation) throw new Error('연결을 취소했어요.');
    this._status('connecting', '서버에 연결하고 있어요.');
    try {
      const health = await this.checkHealth();
      if (generation !== this._generation) throw new Error('연결을 취소했어요.');
      if (!health.rtc_available) throw new Error(health.detail || '지금은 화면 미리보기 모드예요. FastRTC 서버로 실행해 주세요.');

      const pairingCode = health.pairing_required ? this._pairingCode : '';
      if (health.pairing_required && !pairingCode) throw new PairingError(PAIRING_REQUIRED_MESSAGE);
      const headers = pairingCode ? {'X-Sense-Pairing': pairingCode} : {};
      if (this.usesHttpAnswerAudio) headers['X-Sense-Answer-Audio'] = 'http';
      let session;
      try {
        session = await this._json('/api/sessions', { method: 'POST', body: '{}', headers }, null);
      } catch (error) {
        if (error.status !== 403 || !pairingCode || generation !== this._generation || pairingCode !== this._pairingCode) throw error;
        this.setPairingCode('');
        throw new PairingError(PAIRING_REJECTED_MESSAGE);
      }
      if (generation !== this._generation) {
        await this._deleteSession(session.session_id, session.token);
        throw new Error('연결을 취소했어요.');
      }
      if (!session.session_id || !session.token) throw new Error('서버 세션을 만들지 못했어요.');
      this._sessionId = session.session_id;
      this._token = session.token;
      const pc = new RTCPeerConnection({ iceServers: health.rtc_configuration?.iceServers || health.ice_servers || [] });
      this._pc = pc;
      this._remoteStream = new MediaStream();

      pc.addEventListener('track', event => {
        if (generation !== this._generation || event.track.kind !== 'audio') return;
        if (!this._remoteStream.getTracks().some(track => track.id === event.track.id)) {
          this._remoteStream.addTrack(event.track);
        }
        this._emit('audio', { stream: this._remoteStream });
      });
      pc.addEventListener('connectionstatechange', () => {
        if (generation !== this._generation) return;
        if (pc.connectionState === 'connected') this._status('connected', '실시간 서버 연결됨');
        else if (pc.connectionState === 'disconnected') this._status('reconnecting', '연결이 잠시 끊겼어요.');
        else if (pc.connectionState === 'failed') {
          this._emit('error', { message: '실시간 연결이 끊겼어요. 안내를 멈추고 다시 연결해 주세요.' });
          void this.disconnect();
        }
      });

      // FastRTC uses this channel for its connection lifecycle.
      const channel = pc.createDataChannel('text');
      channel.addEventListener('message', event => {
        if (generation !== this._generation) return;
        try {
          const message = JSON.parse(event.data);
          if (message.type === 'sense.error') this._emit('error', { message: String(message.message || '서버 오류') });
        } catch { /* Library control messages may be plain strings. */ }
      });
      for (const track of mediaStream.getTracks()) {
        if (track.kind === 'video' && health.video_uplink_only && typeof pc.addTransceiver === 'function') {
          // The phone uses the local preview. It only needs audio back from the PC.
          pc.addTransceiver(track, { direction: 'sendonly', streams: [mediaStream] });
        } else {
          pc.addTrack(track, mediaStream);
        }
      }
      await pc.setLocalDescription(await pc.createOffer());
      await this._waitForIce(pc);
      if (generation !== this._generation) throw new Error('연결을 취소했어요.');
      const answer = await this._json('/webrtc/offer', {
        method: 'POST',
        body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type, webrtc_id: this._sessionId }),
      }, this._token, 20000);
      if (answer.status === 'failed' || !answer.sdp || answer.type !== 'answer') {
        throw new Error('서버가 실시간 연결을 수락하지 못했어요. 연결 수와 서버 설정을 확인해 주세요.');
      }
      if (generation !== this._generation) throw new Error('연결을 취소했어요.');
      await pc.setRemoteDescription(answer);
      await this._waitForConnected(pc);
      if (generation !== this._generation) throw new Error('연결을 취소했어요.');
      this._pollErrors = 0;
      this._poll(generation);
      return health;
    } catch (error) {
      if (generation === this._generation) {
        await this.disconnect(false);
        if (error instanceof PairingError) this._status('idle');
        else this._status('error', error.message);
      }
      throw error;
    }
  }

  _waitForIce(pc) {
    if (pc.iceGatheringState === 'complete') return Promise.resolve();
    return new Promise((resolve, reject) => {
      const finish = (error) => {
        clearTimeout(timer);
        pc.removeEventListener('icegatheringstatechange', changed);
        pc.removeEventListener('connectionstatechange', closed);
        error ? reject(error) : resolve();
      };
      const changed = () => { if (pc.iceGatheringState === 'complete') finish(); };
      const closed = () => { if (pc.connectionState === 'closed') finish(new Error('연결을 취소했어요.')); };
      const timer = setTimeout(() => finish(new Error('연결 경로를 찾지 못했어요. Wi-Fi와 STUN/TURN 설정을 확인해 주세요.')), 10000);
      pc.addEventListener('icegatheringstatechange', changed);
      pc.addEventListener('connectionstatechange', closed);
    });
  }

  _waitForConnected(pc) {
    if (pc.connectionState === 'connected') return Promise.resolve();
    return new Promise((resolve, reject) => {
      const finish = error => {
        clearTimeout(timer);
        pc.removeEventListener('connectionstatechange', changed);
        error ? reject(error) : resolve();
      };
      const changed = () => {
        if (pc.connectionState === 'connected') finish();
        else if (['failed', 'closed'].includes(pc.connectionState)) finish(new Error('실시간 연결에 실패했어요.'));
      };
      const timer = setTimeout(() => finish(new Error('영상·음성 연결이 지연되고 있어요. 같은 Wi-Fi와 방화벽 설정을 확인해 주세요.')), 18000);
      pc.addEventListener('connectionstatechange', changed);
    });
  }

  async _poll(generation) {
    if (generation !== this._generation || !this._sessionId) return;
    try {
      const state = await this._json(`/api/sessions/${encodeURIComponent(this._sessionId)}/state`, {}, this._token, 6000);
      if (generation !== this._generation) return;
      this._pollErrors = 0;
      this._emit('state', state);
    } catch (error) {
      if (generation !== this._generation) return;
      this._pollErrors += 1;
      if (this._pollErrors >= 3) {
        this._emit('error', { message: '서버 상태를 확인할 수 없어 연결을 종료했어요.' });
        await this.disconnect();
        return;
      }
    }
    if (generation === this._generation) this._pollTimer = setTimeout(() => this._poll(generation), 800);
  }

  async control(action, mode = 'guide', turnId, { emitState = true } = {}) {
    if (!this.connected || !this._sessionId) throw new Error('먼저 실시간 서버에 연결해 주세요.');
    const generation = this._generation;
    const body = { action, mode };
    if (turnId) body.turn_id = turnId;
    const response = await this._json(`/api/sessions/${encodeURIComponent(this._sessionId)}/control`, { method: 'POST', body: JSON.stringify(body) });
    if (generation !== this._generation) throw new Error('종료된 연결의 응답이에요.');
    if (emitState) this._emit('state', response.state || response);
    return response;
  }

  async getAnswerAudio(resultId, signal) {
    if (!this.connected || !this._sessionId || !/^[a-f0-9]{32}$/.test(resultId)) {
      throw new Error('재생할 답변 음성이 없어요.');
    }
    const generation = this._generation;
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) abort();
    else signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, 8000);
    try {
      const path = `/api/sessions/${encodeURIComponent(this._sessionId)}/audio/${resultId}.wav`;
      const response = await fetch(path, {
        headers: { Authorization: `Bearer ${this._token}` }, signal: controller.signal, cache: 'no-store',
      });
      if (!response.ok) throw new Error('답변 음성을 가져오지 못했어요.');
      const bytes = await response.arrayBuffer();
      if (generation !== this._generation) throw new Error('종료된 연결의 음성이에요.');
      return bytes;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }

  async _deleteSession(id, token) {
    if (!id || !token) return;
    try { await this._json(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }, token, 4000); }
    catch { /* Server TTL also clears abandoned sessions. */ }
  }

  async disconnect(notify = true) {
    ++this._generation;
    // Detach a cancelled attempt immediately: a new click can start fresh while
    // the old HTTP request settles, and its generation guards cannot affect us.
    this._connectTask = null;
    const cleanup = this._closeActiveConnection();
    if (notify) this._status('disconnected', '서버 연결이 종료됐어요.');
    await cleanup;
  }

  async _closeActiveConnection() {
    clearTimeout(this._pollTimer);
    this._pollTimer = null;
    const id = this._sessionId;
    const token = this._token;
    const pc = this._pc;
    this._pc = null;
    this._sessionId = null;
    this._token = null;
    this._remoteStream = null;
    if (pc) pc.close();
    await this._deleteSession(id, token);
  }
}
