import { PAIRING_REQUIRED_MESSAGE, PairingError, SenseTransport } from './rtc-client.js';
import { AnswerAudio } from './answer-audio.js';
import { $, icon, readPreferences, savePreferences, mediaErrorMessage, modes, scenarios } from './ui.js';

const answerAudio = new AnswerAudio();
const transport = new SenseTransport({answerAudioHttp: answerAudio.supported});
const preferences = readPreferences();
const CAMERA_TARGET_FPS = 30;
const VIDEO_STALE_MS = 3000;
const INITIAL_VIDEO_WAIT_MS = 3500;
const state = {
  media: null, facing: 'environment', camera: 'off', connection: 'idle', videoStale: false,
  mode: 'scene', guide: 'idle', listening: false, questionPending: false,
  actionBusy: false, demo: false, scenario: 'door', demoPaused: false,
  health: null, backend: null, result: null, lastResultId: null,
  currentTurn: null, lastAnswer: '', audioRetry: false, error: '',
  voiceWakeRequested: false, voiceWakeEnabled: false, voiceWakePhase: 'off', voiceStartNeedsGesture: false,
};
let epoch = 0;
let speech = null;
let speechStartTimer = null;
let questionTimer = null;
let pendingTimer = null;
let lastSpeechId = null;
let demoChanging = false;
let autoVoiceAttempted = false;
let freshQrPending = false;
let voicePlayback = null;

function acceptScannedPairing() {
  const code = window.sensePairingFromQr;
  if (typeof code !== 'string') return false;
  delete window.sensePairingFromQr;
  transport.setPairingCode(code);
  freshQrPending = transport.hasPairingCode;
  return true;
}

acceptScannedPairing();

function suppressAutoVoiceStart() {
  autoVoiceAttempted = true;
  freshQrPending = false;
}

function pairingNeeded() {
  const health = transport.health || state.health;
  return !transport.connected && health?.pairing_required && !transport.hasPairingCode;
}

function setStatus(message = '', error = false, urgent = false) {
  if ($('statusMessage').textContent !== message) $('statusMessage').textContent = message;
  $('statusMessage').classList.toggle('is-error', error);
  const changedError = error && state.error !== message;
  state.error = error ? message : '';
  if (changedError) { urgent = true; try { navigator.vibrate?.([150, 80, 150]); } catch {} }
  if (urgent) {
    $('urgentMessage').textContent = '';
    requestAnimationFrame(() => { $('urgentMessage').textContent = message; });
  }
}

function stopSpeech() {
  clearTimeout(speechStartTimer);
  answerAudio.stop();
  state.audioRetry = false;
  speech = null;
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  finishVoicePlayback();
  syncMicrophone();
  $('remoteAudio').muted = shouldMuteRemote();
}

function speak(text, id = null, replay = false) {
  if (!preferences.voice || !text || state.listening || voiceQuestionActive()) return;
  if (!('speechSynthesis' in window)) {
    setStatus('이 브라우저에서 음성 합성을 사용할 수 없어요.');
    return;
  }
  if (!replay && id && id === lastSpeechId) return;
  if (id) lastSpeechId = id;
  stopSpeech();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = 'ko-KR';
  utterance.rate = preferences.voiceSpeed;
  const localOnly = state.voiceWakeEnabled || voiceWakeAvailable();
  const koreanVoice = window.speechSynthesis.getVoices().find(voice => voice.lang.toLowerCase().startsWith('ko') && (!localOnly || voice.localService));
  if (localOnly && !koreanVoice) {
    setStatus('이 기기의 오프라인 한국어 음성을 사용할 수 없어요. 화면에서 안내를 확인해 주세요.');
    return;
  }
  if (koreanVoice) utterance.voice = koreanVoice;
  speech = utterance;
  syncMicrophone();
  $('remoteAudio').muted = true;
  const restore = () => {
    if (speech === utterance) {
      clearTimeout(speechStartTimer);
      speech = null;
      finishVoicePlayback(id);
      syncMicrophone();
      $('remoteAudio').muted = shouldMuteRemote();
    }
  };
  utterance.onstart = () => {
    if (speech !== utterance) return;
    clearTimeout(speechStartTimer);
    startVoicePlayback(id);
  };
  utterance.onend = restore;
  utterance.onerror = event => {
    if (speech !== utterance) return;
    restore();
    if (!['canceled', 'interrupted'].includes(event.error)) offerAudioRetry('음성 읽기를 시작하지 못했어요. 음성 재생을 눌러 다시 시도해 주세요.');
  };
  speechStartTimer = setTimeout(() => {
    if (speech !== utterance) return;
    stopSpeech();
    offerAudioRetry('음성 읽기가 시작되지 않았어요. 음성 재생을 눌러 다시 시도해 주세요.');
  }, 4500);
  window.speechSynthesis.speak(utterance);
}

function offerAudioRetry(message) {
  if (!preferences.voice || state.listening || voiceQuestionActive() || state.videoStale) return;
  state.audioRetry = true;
  setStatus(message);
  render();
}

function answerCanPlay(result) {
  return preferences.voice && transport.connected && state.result?.id === result.id && !state.listening
    && !voiceQuestionActive() && !state.videoStale && !state.backend?.last_error && !state.backend?.scene_waiting_for_video;
}

async function playServerAnswer(result) {
  const isCurrent = () => answerCanPlay(result);
  if (!isCurrent()) return;
  const outcome = await answerAudio.play(signal => transport.getAnswerAudio(result.id, signal), isCurrent, {
    onStart: () => startVoicePlayback(result.id),
    onEnd: () => finishVoicePlayback(result.id),
  });
  if (!isCurrent() || outcome === 'cancelled') return;
  if (outcome === 'playing') {
    state.audioRetry = false;
    render();
  } else if (outcome === 'failed') {
    offerAudioRetry('답변 음성을 가져오지 못했어요. 연결을 확인한 뒤 음성 재생을 눌러 주세요.');
  } else {
    offerAudioRetry('답변이 준비됐어요. 소리가 나지 않으면 음성 재생을 눌러 주세요.');
  }
}

async function resumeRemoteAudio(reportFailure = false) {
  const audio = $('remoteAudio');
  const stream = audio.srcObject;
  if (!stream) return;
  audio.muted = shouldMuteRemote();
  if (audio.muted) return;
  try {
    await audio.play();
    if (reportFailure && audio.srcObject === stream) { state.audioRetry = false; render(); }
  } catch {
    if (reportFailure && audio.srcObject === stream) offerAudioRetry('음성을 들으려면 음성 재생을 눌러 주세요.');
  }
}

function unlockAnswerAudio() {
  const ready = preferences.voice ? answerAudio.unlock() : Promise.resolve(false);
  if (!transport.usesHttpAnswerAudio && $('remoteAudio').paused) void resumeRemoteAudio();
  return ready;
}

function replayAnswer() {
  if (!preferences.voice || state.listening || voiceQuestionActive() || state.videoStale) return;
  const result = state.result;
  if (result?.audio_source === 'http-wav') {
    void answerAudio.unlock().then(() => playServerAnswer(result));
  } else if (state.lastAnswer) {
    speak(state.lastAnswer, result?.id, true);
    render();
  } else {
    unlockAnswerAudio();
    void resumeRemoteAudio(true);
  }
}

function aiConfigured() { return state.backend ? state.backend.ai_configured === true : state.health?.ai_configured === true; }
function sceneRunning() { return !state.demo && state.mode === 'scene' && state.guide === 'active' && aiConfigured(); }
function voiceWakeAvailable() { return state.health?.voice_wake_available === true && state.health.answer_audio_http === true && answerAudio.supported; }
function voiceQuestionActive() { return state.voiceWakePhase === 'question' || state.voiceWakePhase === 'processing'; }
function shouldMuteRemote() { return !preferences.voice || state.videoStale || state.listening || voiceQuestionActive() || Boolean(speech) || state.guide === 'paused' || state.connection !== 'connected' || Boolean(state.backend?.last_error && aiConfigured()) || Boolean(state.backend?.scene_waiting_for_video); }
function isCameraLive() { return Boolean(state.media?.getVideoTracks().some(track => track.readyState === 'live')); }
function setMic(enabled) { state.media?.getAudioTracks().forEach(track => { track.enabled = enabled; }); }

function syncMicrophone() {
  const wakeListening = state.voiceWakeEnabled && !state.questionPending && ['waiting', 'question'].includes(state.voiceWakePhase);
  setMic(transport.connected && !document.hidden && !state.videoStale && !state.backend?.last_error
    && !speech && !voicePlayback && (state.listening || wakeListening));
}

function updateVoiceWake(next) {
  state.voiceWakeEnabled = state.voiceWakeRequested && next?.voice_wake_enabled === true;
  state.voiceWakePhase = state.voiceWakeEnabled ? next.voice_wake_phase : 'off';
  if (state.voiceWakeEnabled) state.voiceStartNeedsGesture = false;
  syncMicrophone();
}

function disarmVoiceWake() {
  state.voiceWakeRequested = false;
  updateVoiceWake();
}

function startVoicePlayback(id) {
  if (!state.voiceWakeEnabled || !transport.connected || !id) return;
  const playback = {id, sessionId: transport.sessionId, mode: state.mode};
  voicePlayback = playback;
  syncMicrophone();
  playback.started = transport.control('voice.playback.start', playback.mode, id, {emitState: false}).catch(() => {});
}

function finishVoicePlayback(id) {
  const playback = voicePlayback;
  if (!playback || (id && playback.id !== id)) return;
  voicePlayback = null;
  syncMicrophone();
  void playback.started.then(() => {
    if (!state.voiceWakeRequested || !transport.connected || transport.sessionId !== playback.sessionId) return;
    return transport.control('voice.playback.end', playback.mode, playback.id, {emitState: false});
  }).catch(() => {});
}

function releaseMedia() {
  state.voiceStartNeedsGesture = false;
  state.media?.getTracks().forEach(track => { track.onended = null; track.stop(); });
  state.media = null;
  $('camera').srcObject = null;
  state.camera = 'off';
}

function renderPreferences() {
  document.body.classList.toggle('high-contrast', preferences.contrast);
  document.body.dataset.textSize = preferences.textSize;
  $('voiceToggle').setAttribute('aria-checked', String(preferences.voice));
  $('voiceStateText').textContent = preferences.voice ? '켜짐' : '꺼짐';
  icon($('voiceIcon'), preferences.voice ? 'volume' : 'muted');
  $('voiceWakeSetting').hidden = !voiceWakeAvailable() && !state.voiceWakeEnabled;
  $('voiceWakeToggle').setAttribute('aria-checked', String(state.voiceWakeEnabled));
  $('voiceWakeToggle').disabled = state.actionBusy || state.demo || state.listening;
  $('voiceWakeStateText').textContent = state.voiceWakeEnabled ? '켜짐' : '꺼짐';
  $('contrastSetting').checked = preferences.contrast;
  document.querySelectorAll('input[name=textSize]').forEach(input => { input.checked = input.value === preferences.textSize; });
  $('voiceSpeed').value = String(preferences.voiceSpeed);
  $('voiceSpeedValue').value = `${preferences.voiceSpeed.toFixed(1)}배`;
  $('remoteAudio').muted = shouldMuteRemote();
}

function renderConnection() {
  const connected = transport.connected;
  const preview = state.health?.mode === 'preview' || state.health?.rtc_available === false;
  const needsQr = pairingNeeded();
  const label = state.demo ? '디자인 시연' : state.videoStale ? '영상 수신 중단' : connected ? 'PC 연결됨' : needsQr ? 'QR 연결 필요' : state.connection === 'reconnecting' ? '연결 확인 필요' : state.connection === 'connecting' ? '연결 중' : state.connection === 'checking' ? '연결 확인 중' : preview ? '화면 미리보기' : '연결 전';
  $('connectionLabel').textContent = label;
  $('connectionDot').className = `status-dot${state.videoStale || state.connection === 'error' ? ' error' : connected ? ' connected' : ''}`;
  $('detailMedia').textContent = isCameraLive() ? '사용 허용됨' : state.camera === 'requesting' ? '권한 요청 중' : state.camera === 'error' ? '권한·장치 확인 필요' : '요청 전';
  $('detailSecure').textContent = window.isSecureContext ? 'HTTPS / localhost' : 'HTTPS 연결 필요';
  $('detailTransport').textContent = connected ? '연결됨' : state.connection === 'connecting' ? '연결 중' : preview ? 'UI 미리보기 모드' : '연결 전';
  $('detailAI').textContent = aiConfigured() ? '설정됨 · 결과 확인 필요' : '아직 연결되지 않음';
  $('detailVideo').textContent = `${Number(state.backend?.video_frames || 0).toLocaleString('ko-KR')} 프레임`;
  $('detailAudio').textContent = `${Number(state.backend?.audio_samples || 0).toLocaleString('ko-KR')} 샘플`;
  const age = state.backend?.last_video_age_ms;
  $('detailFrameAge').textContent = typeof age === 'number' ? `${(age / 1000).toFixed(1)}초 전` : '—';
  $('connectionNote').textContent = state.videoStale
    ? '카메라 영상 수신이 멈췄어요. 카메라 다시 연결을 눌러 주세요.'
    : state.demo ? '디자인 시연 중에는 실제 영상·음성을 보내지 않아요.' : needsQr ? PAIRING_REQUIRED_MESSAGE : preview ? '지금은 UI 미리보기예요. 카메라 입력은 확인할 수 있고, FastRTC 서버 실행 후 실시간 통신을 연결할 수 있어요.' : connected ? '서버 수신 수치와 확인음으로 연결을 점검해 보세요.' : state.health ? '카메라·마이크를 켠 뒤 서버에 연결할 수 있어요.' : '서버 응답을 아직 받지 못했어요. 서버 실행 여부를 확인해 주세요.';
  $('connectAction').disabled = state.actionBusy || state.demo || needsQr || (connected && !state.videoStale) || preview;
  $('connectAction').lastChild.textContent = state.videoStale ? '카메라 다시 연결' : connected ? '서버 연결됨' : '서버 연결';
  $('testAudioAction').disabled = !connected || state.videoStale || state.actionBusy || state.demo || state.listening || state.questionPending;
}

function resultView() {
  if (state.demo) {
    const demo = scenarios[state.scenario];
    return { label: demo.label, title: demo.observation, body: '', direction: demo.guidance, icon: demo.icon };
  }
  if (state.videoStale) return { label: '영상 수신 중단', title: '안내를 멈췄어요.', body: '카메라 다시 연결을 눌러 주세요.', icon: 'alert' };
  if (state.backend?.last_error && state.backend.mode === state.mode && aiConfigured()) return { label: '안내 보류', title: '지금은 확인하기 어려워요.', body: String(state.backend.last_error), icon: 'alert' };
  if (state.listening) return { label: '음성 질문', title: '듣고 있어요.', body: '말씀을 마치면 ‘말하기 완료’를 눌러 주세요. 20초가 지나면 자동으로 완료돼요.', icon: 'mic' };
  if (state.voiceWakePhase === 'question') return { label: '헤이 센스', title: '질문을 듣고 있어요.', body: '말씀이 끝나면 답변을 준비해요.', icon: 'mic' };
  if (state.voiceWakePhase === 'processing') return state.lastResultId
    ? { label: '답변 대기', title: '질문을 확인하고 있어요.', body: '답변이 준비되면 읽어 드려요.', icon: 'eye' }
    : { label: '음성 안내 시작', title: '주변 안내를 준비하고 있어요.', body: '', icon: 'volume' };
  if (state.questionPending) return { label: '답변 대기', title: '질문을 확인하고 있어요.', body: '안내가 준비되면 알려드릴게요.', icon: 'eye' };
  if (sceneRunning() && state.backend?.scene_waiting_for_video) return { label: '영상이 안정되기를 기다리는 중', title: '카메라를 잠시 고정해 주세요.', body: '영상이 선명해지면 자동으로 다시 확인해요.', icon: 'camera' };
  if (state.backend?.busy && state.backend.mode === state.mode && state.guide === 'idle' && !state.result) return { label: '화면 확인 중', title: '현재 장면을 확인하고 있어요.', body: '안내가 준비되면 알려드릴게요.', icon: 'eye' };
  if (state.result?.source === 'startup') return { label: '음성 안내 시작', title: state.result.text, body: '“헤이 센스, 내 앞에 뭐가 있어?”라고 말해 보세요.', icon: 'volume' };
  if (state.result?.source === 'ai') return { label: state.result.kind === 'obstacle' ? '장애물 알림' : state.result.kind === 'uncertain' ? '확인하기 어려운 상황' : sceneRunning() ? '주변 확인 중' : '안내 결과', title: state.result.text, body: '', icon: state.result.kind === 'obstacle' ? 'alert' : state.result.kind === 'uncertain' ? 'camera' : 'eye' };
  if (state.guide === 'paused') return { label: '안내 일시정지', title: '잠시 멈춰 있어요.', body: '준비되면 안내를 이어갈 수 있어요.', icon: 'pause' };
  if (state.guide === 'active' && !aiConfigured()) return { label: 'AI 연결 전', title: '영상·음성 통신 시험 중', body: '주변 분석과 이동 안내는 AI 연결 후 사용할 수 있어요.', icon: 'link' };
  if (state.guide === 'active') return { label: '주변 확인 중', title: '현재 장면을 확인하고 있어요.', body: '확인된 정보가 도착하면 안내할게요.', icon: 'eye' };
  return null;
}

function render() {
  const focused = document.activeElement;
  const live = isCameraLive();
  const needsQr = pairingNeeded() && !state.demo;
  const mode = modes[state.mode];
  const view = resultView();
  const needsRecheck = state.demo ? state.scenario !== 'door' : ['uncertain', 'obstacle'].includes(state.result?.kind);
  const checkingOnce = !sceneRunning() && aiConfigured() && state.backend?.mode === state.mode && (state.backend.busy || state.backend.guide_active);
  const waiting = state.actionBusy || checkingOnce || state.questionPending || voiceQuestionActive();
  const canStartVoice = voiceWakeAvailable() && preferences.voice && state.mode === 'scene' && !state.voiceWakeEnabled;
  const hasSession = state.demo || live || transport.connected || state.videoStale || waiting || state.listening;
  $('camera').hidden = !live || state.demo;
  $('cameraPlaceholder').hidden = live || state.demo;
  $('cameraLive').hidden = !live || state.demo;
  $('demoScene').hidden = !state.demo;
  $('demoScene').dataset.scenario = state.scenario;
  $('cameraStage').classList.toggle('mirrored', state.facing === 'user' && !state.demo);
  $('cameraDemoBadge').hidden = !state.demo;
  $('resultDemoBadge').hidden = !state.demo;
  $('cameraOverlay').hidden = !state.demo && !live;
  $('cameraOverlayText').textContent = state.demo ? scenarios[state.scenario].overlay : '현재 비추는 방향을 확인해요';
  $('cameraStatus').querySelector('span').textContent = state.demo ? '시연용 가상 장면' : live ? '카메라 켜짐' : state.camera === 'requesting' ? '권한 요청 중' : '카메라 꺼짐';
  $('cameraStatus').classList.toggle('active', live);
  const micTransmitting = state.media?.getAudioTracks().some(track => track.enabled) === true;
  let micLabel = live ? '마이크 대기' : '마이크 꺼짐';
  if (state.demo) micLabel = '마이크 사용 안 함';
  else if (micTransmitting) micLabel = state.listening || state.voiceWakePhase === 'question' ? '질문 듣는 중' : '헤이 센스 대기 중';
  $('micStatus').querySelector('span').textContent = micLabel;
  $('micStatus').classList.toggle('recording', micTransmitting);
  $('enableCamera').disabled = state.actionBusy || state.camera === 'requesting' || needsQr;
  $('switchCamera').disabled = !live || state.actionBusy || state.listening || state.demo;
  if (state.demo) $('pageTitle').textContent = state.demoPaused ? '안내 일시정지' : scenarios[state.scenario].title;
  else $('pageTitle').textContent = state.mode === 'scene' ? '주변 확인' : mode.name;
  $('introText').hidden = Boolean(view) || needsQr;
  if (!view) $('introText').textContent = state.mode === 'scene' ? canStartVoice ? '주변을 비춰 주세요. 시작한 뒤 “헤이 센스”라고 부르면 질문할 수 있어요.' : '멈춰서 주변을 비춰 주세요. 한 번 누르면 계속 확인해요.' : mode.intro;
  $('guidePanel').classList.toggle('has-result', Boolean(view));
  $('guidePanel').classList.toggle('is-obstacle', state.demo ? state.scenario === 'obstacle' : state.result?.kind === 'obstacle');
  $('resultArea').hidden = !view;
  if (view) {
    $('resultLabel').textContent = view.label;
    $('resultTitle').textContent = view.title;
    $('resultBody').textContent = view.body || '';
    $('resultDirection').hidden = !view.direction;
    $('resultDirectionText').textContent = view.direction || '';
    icon($('resultIcon'), view.icon);
  }
  $('listeningBars').hidden = !state.listening && state.voiceWakePhase !== 'question';
  $('serviceNotice').hidden = state.demo || aiConfigured() || needsQr || Boolean(view) || state.health?.mode === 'preview';
  $('serviceNoticeText').textContent = state.health ? 'AI 연결 전이에요. 영상과 음성 전송을 시험할 수 있어요.' : state.connection === 'error' ? '서버 상태를 확인할 수 없어요.' : '연결 상태 확인 중';
  $('demoControls').hidden = !state.demo;
  document.querySelectorAll('[data-scenario]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.scenario === state.scenario)));
  $('demoToggle').querySelector('span').textContent = state.demo ? '시연 끝내기' : '디자인 시연';
  $('guideAction').hidden = needsQr || state.videoStale || state.listening || waiting
    || (sceneRunning() && !(canStartVoice && state.voiceStartNeedsGesture));
  $('guideAction').disabled = $('guideAction').hidden;
  let actionLabel = state.mode === 'scene' ? (state.result?.source === 'ai' ? '다시 확인' : '주변 확인') : mode.action;
  let actionIcon = state.mode === 'scene' ? 'eye' : 'play';
  if (state.actionBusy) { actionLabel = '준비 중'; actionIcon = 'link'; }
  else if (state.demo) { actionLabel = state.scenario === 'uncertain' ? '카메라 다시 확인' : state.demoPaused ? '안내 계속' : state.scenario === 'door' ? '일시정지' : '다시 확인'; actionIcon = state.demoPaused ? 'play' : state.scenario === 'door' ? 'pause' : 'repeat'; }
  else if (canStartVoice) {
    actionLabel = transport.connected ? '음성 안내 시작' : '카메라·마이크로 시작';
    actionIcon = transport.connected ? 'volume' : 'camera';
  }
  else if (needsRecheck) { actionLabel = state.result.kind === 'uncertain' ? '카메라 다시 확인' : '다시 확인'; actionIcon = 'repeat'; }
  else if (state.guide === 'active' && state.mode === 'guide') { actionLabel = '일시정지'; actionIcon = 'pause'; }
  else if (state.guide === 'paused' && state.mode === 'guide') { actionLabel = '안내 계속'; actionIcon = 'play'; }
  $('guideActionLabel').textContent = actionLabel;
  icon($('guideActionIcon'), actionIcon);
  $('questionAction').hidden = needsQr || state.videoStale || state.demo || (waiting && !state.listening);
  $('questionAction').disabled = $('questionAction').hidden || state.actionBusy;
  $('questionActionLabel').textContent = state.listening ? '말하기 완료' : '음성 질문';
  icon($('questionActionIcon'), state.listening ? 'check' : 'mic');
  $('sessionActions').hidden = !hasSession || needsQr;
  $('pairingPanel').hidden = !needsQr;
  $('quickConnect').hidden = needsQr || state.demo || !state.videoStale || state.actionBusy;
  $('quickConnect').disabled = $('quickConnect').hidden;
  $('privacyNote').hidden = needsQr || state.demo || (hasSession && !state.voiceWakeEnabled);
  $('privacyNote').textContent = state.voiceWakeEnabled
    ? '“헤이 센스”라고 부르면 질문할 수 있어요. 마이크 음성은 PC로 전송돼요.'
    : canStartVoice ? '시작하면 카메라 영상과 마이크 음성을 PC로 보내요.' : '영상은 PC로, 음성은 질문할 때 전송해요.';
  $('guideAction').classList.toggle('pause-action', !needsRecheck && (state.guide === 'active' || (state.demo && !state.demoPaused)));
  $('guideAction').classList.toggle('recover-action', needsRecheck);
  $('resultArea').classList.toggle('warning-result', state.videoStale || (state.demo && state.scenario !== 'door') || state.result?.kind === 'obstacle' || state.result?.kind === 'uncertain' || Boolean(state.backend?.last_error && aiConfigured()));
  $('repeatAction').disabled = !state.lastAnswer || !preferences.voice || state.listening;
  $('repeatAction').hidden = $('repeatAction').disabled || waiting || state.videoStale || state.audioRetry;
  $('audioResume').hidden = !state.audioRetry || !preferences.voice || state.videoStale || state.listening;
  $('audioResume').disabled = $('audioResume').hidden;
  $('stopAction').disabled = false;
  document.querySelectorAll('[data-mode]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.mode === state.mode));
    button.disabled = waiting || state.videoStale || state.listening || state.demo;
  });
  $('moreModes').hidden = needsQr || state.demo || state.videoStale || state.listening || waiting;
  $('returnGuide').hidden = $('moreModes').hidden || state.mode === 'scene';
  $('returnGuide').disabled = state.videoStale;
  renderPreferences();
  renderConnection();
  const mainControls = ['quickConnect', 'guideAction', 'questionAction', 'stopAction', 'audioResume', 'repeatAction', 'returnGuide'];
  if (mainControls.includes(focused?.id) && focused.closest('[hidden]')) {
    mainControls.map($).find(button => !button.disabled && !button.closest('[hidden]'))?.focus({ preventScroll: true });
  }
}

async function operate(task) {
  if (state.actionBusy) return;
  state.actionBusy = true;
  const token = epoch;
  render();
  try { await task(token); }
  catch (error) {
    if (token === epoch) {
      state.listening = false;
      state.questionPending = false;
      stopSpeech();
      setStatus(error?.message || '연결을 완료하지 못했어요. 다시 시도해 주세요.', true);
    }
  } finally {
    if (token === epoch) state.actionBusy = false;
    render();
  }
}

async function ensureMedia(token = epoch, {visibleOnly = false} = {}) {
  if (token !== epoch || (visibleOnly && document.hidden)) return null;
  if (isCameraLive()) return state.media;
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw new Error('카메라·마이크를 사용하려면 HTTPS 주소로 접속해 주세요. 컴퓨터에서는 localhost로 확인할 수 있어요.');
  state.camera = 'requesting';
  setStatus(state.voiceWakeRequested
    ? '카메라·마이크를 허용해 주세요. PC에서 “헤이 센스”를 기다려요.'
    : '카메라·마이크를 허용해 주세요. 마이크 입력은 질문을 시작할 때 전송해요.');
  render();
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: state.facing },
        width: { ideal: 1280 },
        height: { ideal: 720 },
        frameRate: { ideal: CAMERA_TARGET_FPS, max: CAMERA_TARGET_FPS },
      },
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (error) {
    if (token === epoch) state.camera = 'error';
    throw new Error(mediaErrorMessage(error));
  }
  if (token !== epoch || (visibleOnly && document.hidden)) {
    stream.getTracks().forEach(track => track.stop());
    return null;
  }
  stream.getAudioTracks().forEach(track => { track.enabled = false; });
  state.media = stream;
  state.camera = 'on';
  $('camera').srcObject = stream;
  stream.getTracks().forEach(track => {
    track.onended = () => {
      if (state.media !== stream) return;
      stopEverything({ message: '장치 연결이 끝났어요. 카메라를 다시 켜 주세요.' });
    };
  });
  try { await $('camera').play(); } catch { /* Browser may finish starting playback after metadata loads. */ }
  if (token !== epoch) return null;
  setStatus('카메라가 준비됐어요.');
  render();
  return stream;
}

async function ensureConnected(token = epoch, {visibleOnly = false} = {}) {
  if (!state.health && !transport.health) {
    state.health = await transport.checkHealth();
    if (token !== epoch) return false;
  }
  if ((transport.health || state.health)?.mode === 'preview') {
    throw new Error('화면 미리보기예요. 실제로 연결하려면 PC 관제 화면의 QR을 스캔해 주세요.');
  }
  if (pairingNeeded()) throw new PairingError(PAIRING_REQUIRED_MESSAGE);
  const media = await ensureMedia(token, {visibleOnly});
  if (!media || token !== epoch || (visibleOnly && document.hidden)) return false;
  if (transport.connected) { state.connection = 'connected'; return true; }
  state.connection = 'connecting';
  render();
  try { await transport.connect(media); }
  catch (error) {
    if (error instanceof PairingError && token === epoch) releaseMedia();
    throw error;
  }
  if (token !== epoch) return false;
  state.connection = transport.connected ? 'connected' : 'connecting';
  state.health = transport.health || state.health;
  return transport.connected;
}

async function refreshHealth() {
  if (state.connection !== 'connecting') state.connection = 'checking';
  renderConnection();
  try { state.health = await transport.checkHealth(); state.connection = transport.connected ? 'connected' : 'idle'; }
  catch { state.health = null; state.connection = 'error'; }
  render();
}

async function waitForCameraFrame(token) {
  const deadline = performance.now() + INITIAL_VIDEO_WAIT_MS;
  while (token === epoch && transport.connected) {
    const age = state.backend?.last_video_age_ms;
    if (typeof age === 'number' && age < VIDEO_STALE_MS) return true;
    if (performance.now() >= deadline) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  if (token !== epoch) return false;
  throw new Error('영상이 아직 도착하지 않았어요. 카메라를 확인한 뒤 다시 시도해 주세요.');
}

async function startVoiceWake(token, {fromQr = false, gestureAudio = null} = {}) {
  if (!fromQr && state.voiceStartNeedsGesture && !await gestureAudio) {
    if (token === epoch) setStatus('영상은 확인 중이에요. 소리를 켜려면 음성 안내 시작을 눌러 주세요.');
    return;
  }
  if (token !== epoch) return;
  state.voiceWakeRequested = true;
  try {
    const connected = await ensureConnected(token, {visibleOnly: fromQr});
    if (token !== epoch) return;
    if (fromQr && document.hidden) {
      await stopEverything({message: '화면을 떠나 자동 시작을 멈췄어요.'});
      return;
    }
    if (!connected || !await waitForCameraFrame(token)) return;
    if (fromQr) {
      // Safari can allow Web Audio once the page is capturing media.
      const audioReady = !document.hidden && await answerAudio.unlock();
      if (token !== epoch) return;
      if (document.hidden) {
        await stopEverything({message: '화면을 떠나 자동 시작을 멈췄어요.'});
        return;
      }
      if (!audioReady) {
        // Automatic playback can be blocked even though capture and WebRTC work.
        state.voiceStartNeedsGesture = true;
        await transport.control('guide.start', 'scene');
        if (token === epoch) {
          setStatus('PC에서 영상을 확인하고 있어요. 소리를 켜려면 음성 안내 시작을 한 번 눌러 주세요.');
        }
        return;
      }
    }
    stopSpeech();
    state.mode = 'scene';
    state.currentTurn = null;
    state.result = null;
    state.lastAnswer = '';
    state.lastResultId = null;
    setStatus('음성으로 주변 안내를 시작하는 중이에요.');
    await transport.control('voice.enable', 'scene');
    if (token === epoch && !state.voiceWakeEnabled) {
      throw new Error('음성 호출을 시작하지 못했어요. 다시 시도해 주세요.');
    }
  } catch (error) {
    if (fromQr && token === epoch) {
      if (document.hidden) {
        await stopEverything({message: '화면을 떠나 자동 시작을 멈췄어요.'});
        return;
      }
      if (!transport.connected && state.media) releaseMedia();
    }
    throw error;
  } finally {
    if (token === epoch && !state.voiceWakeEnabled) disarmVoiceWake();
  }
}

async function maybeStartVoiceWake() {
  const paired = (transport.health || state.health)?.pairing_required === true;
  if ((paired ? !freshQrPending : autoVoiceAttempted)
      || state.voiceWakeEnabled || !voiceWakeAvailable() || !preferences.voice || pairingNeeded()
      || !window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection
      || document.hidden || state.demo || state.actionBusy || state.mode !== 'scene') return;
  if (paired) {
    freshQrPending = false;
    await operate(token => startVoiceWake(token, {fromQr: true}));
    if (freshQrPending) void maybeStartVoiceWake();
    return;
  }
  autoVoiceAttempted = true;
  const token = epoch;
  if (!await answerAudio.unlock() || token !== epoch || document.hidden || state.demo || state.actionBusy || state.mode !== 'scene') return;
  await operate(startVoiceWake);
}

async function toggleVoiceWake() {
  if ((!voiceWakeAvailable() && !state.voiceWakeEnabled) || state.actionBusy || state.demo || state.listening) return;
  suppressAutoVoiceStart();
  if (state.voiceWakeEnabled) {
    await operate(async token => {
      disarmVoiceWake();
      stopSpeech();
      if (transport.connected) await transport.control('voice.disable', state.mode);
      if (token === epoch) {
        state.result = null;
        state.lastAnswer = '';
        setStatus('헤이 센스를 껐어요. 음성 질문 버튼은 계속 쓸 수 있어요.');
      }
    });
    return;
  }
  if (!preferences.voice) {
    preferences.voice = true;
    savePreferences(preferences);
  }
  const gestureAudio = unlockAnswerAudio();
  $('settingsDialog').close();
  await operate(token => startVoiceWake(token, {gestureAudio}));
}

async function guideAction() {
  suppressAutoVoiceStart();
  const gestureAudio = unlockAnswerAudio();
  if (state.demo) {
    if (state.scenario === 'door') state.demoPaused = !state.demoPaused;
    else { state.scenario = 'door'; state.demoPaused = false; }
    if (state.demoPaused) { stopSpeech(); setStatus('시연 예시를 잠시 멈췄어요.'); }
    else speakDemo();
    render();
    return;
  }
  await operate(async token => {
    if (!state.health) {
      const health = transport.health || await transport.checkHealth();
      if (token !== epoch) return;
      state.health = health;
    }
    if (voiceWakeAvailable() && preferences.voice && state.mode === 'scene' && !state.voiceWakeEnabled) {
      await startVoiceWake(token, {gestureAudio});
      return;
    }
    if (!await ensureConnected(token)) return;
    if (!await waitForCameraFrame(token)) return;
    if (sceneRunning()) return;
    stopSpeech();
    const action = state.guide === 'active' && state.mode === 'guide' ? 'guide.pause' : state.guide === 'paused' && state.mode === 'guide' ? 'guide.resume' : 'guide.start';
    state.currentTurn = null;
    state.result = null;
    state.lastAnswer = '';
    if (action === 'guide.pause') state.guide = 'paused';
    await transport.control(action, state.mode);
    if (token !== epoch) return;
    if (state.mode === 'guide') state.guide = action === 'guide.pause' ? 'paused' : 'active';
    if (action === 'guide.pause') setStatus('안내를 잠시 멈췄어요.');
    else if (!aiConfigured()) setStatus('영상이 PC에 도착했어요.');
    else setStatus(state.mode === 'scene' ? '주변 확인을 시작했어요. 장면이 바뀌면 다시 확인해요.' : '현재 화면을 확인하고 있어요.');
  });
}

async function questionAction() {
  if (state.demo) return;
  suppressAutoVoiceStart();
  unlockAnswerAudio();
  if (state.listening) {
    await finishQuestion();
    return;
  }
  if (voiceQuestionActive()) return;
  const focusQuestion = document.activeElement === $('questionAction');
  await operate(async token => {
    if (!await ensureConnected(token)) return;
    if (!await waitForCameraFrame(token)) return;
    stopSpeech();
    $('remoteAudio').muted = true;
    if (aiConfigured()) setStatus('화면이 안정되면 질문을 듣기 시작해요.');
    const answer = await transport.control('question.start', state.mode);
    if (token !== epoch) return;
    state.currentTurn = answer?.current_turn_id || answer?.state?.current_turn_id || null;
    if (!state.currentTurn) throw new Error('서버에서 질문 번호를 받지 못했어요. 연결 상태를 확인해 주세요.');
    state.listening = true;
    state.guide = 'idle';
    state.questionPending = false;
    state.result = null;
    syncMicrophone();
    setStatus('듣고 있어요. 말씀을 마치면 말하기 완료를 눌러 주세요. 최대 20초 후 자동으로 완료돼요.');
    clearTimeout(questionTimer);
    questionTimer = setTimeout(() => { if (state.listening) finishQuestion(); }, 20000);
  });
  if (focusQuestion && state.listening && document.activeElement === $('stopAction')) {
    $('questionAction').focus({ preventScroll: true });
  }
}

async function finishQuestion() {
  if (!state.listening) return;
  clearTimeout(questionTimer);
  setMic(false);
  state.listening = false;
  state.questionPending = true;
  render();
  await operate(async token => {
    await transport.control('question.end', state.mode, state.currentTurn);
    if (token !== epoch) return;
    $('remoteAudio').muted = shouldMuteRemote();
    if (!aiConfigured()) {
      state.currentTurn = null;
      state.questionPending = false;
      setStatus('질문 전송을 마쳤어요. 현재 서버에는 AI 분석이 연결되지 않았어요.');
    } else {
      setStatus('질문을 전송했어요. 답변을 기다리고 있어요.');
      clearTimeout(pendingTimer);
      pendingTimer = setTimeout(() => {
        if (state.questionPending) { state.questionPending = false; setStatus('답변이 늦어지고 있어요. 연결 상태를 확인한 뒤 다시 질문해 주세요.', true); render(); }
      }, state.health?.answer_wait_ms ?? 30000);
    }
  });
}

async function stopEverything({ message = '카메라와 연결을 종료했어요.', keepDemo = false } = {}) {
  const token = ++epoch;
  suppressAutoVoiceStart();
  clearTimeout(questionTimer);
  clearTimeout(pendingTimer);
  state.listening = false;
  disarmVoiceWake();
  stopSpeech();
  setMic(false);
  releaseMedia();
  state.guide = 'idle';
  state.questionPending = false;
  state.actionBusy = true;
  state.result = null;
  state.lastAnswer = '';
  state.lastResultId = null;
  state.currentTurn = null;
  state.backend = null;
  state.videoStale = false;
  state.connection = 'idle';
  if (!keepDemo) state.demo = false;
  $('remoteAudio').pause();
  $('remoteAudio').srcObject = null;
  render();
  try { await transport.disconnect(false); } catch { /* Local tracks are already stopped. */ }
  if (token !== epoch) return;
  state.actionBusy = false;
  setStatus(message);
  render();
}

async function switchCamera() {
  unlockAnswerAudio();
  await operate(async token => {
    const wasConnected = transport.connected;
    const resumeWake = state.voiceWakeEnabled;
    disarmVoiceWake();
    stopSpeech();
    state.guide = 'idle';
    state.result = null;
    state.lastAnswer = '';
    state.backend = null;
    state.videoStale = false;
    await transport.disconnect(false);
    if (token !== epoch) return;
    releaseMedia();
    state.facing = state.facing === 'environment' ? 'user' : 'environment';
    await ensureMedia(token);
    if (wasConnected && token === epoch) await ensureConnected(token);
    if (resumeWake && token === epoch) { await startVoiceWake(token); return; }
    if (token === epoch) setStatus('카메라를 전환했어요. 준비되면 안내를 다시 시작해 주세요.');
  });
}

function speakDemo() {
  const scenario = scenarios[state.scenario];
  state.lastAnswer = `시연 예시입니다. ${scenario.observation} ${scenario.guidance}`;
  if (preferences.voice) { speak(state.lastAnswer); setStatus('시연 예시를 표시했어요. 실제 주변 분석 결과가 아니에요.'); }
  else setStatus(state.lastAnswer);
}

async function toggleDemo() {
  if (demoChanging) return;
  demoChanging = true;
  $('demoToggle').disabled = true;
  try {
    if (state.demo) { await stopEverything({ message: '디자인 시연을 끝냈어요. 실제 카메라를 켜서 입력을 확인할 수 있어요.' }); return; }
    const token = epoch + 1;
    await stopEverything({ message: '' });
    if (token !== epoch) return;
    state.demo = true;
    state.demoPaused = false;
    state.mode = 'scene';
    state.scenario = 'door';
    speakDemo();
    render();
  } finally { demoChanging = false; $('demoToggle').disabled = false; }
}

async function changeMode(mode) {
  if (state.actionBusy || state.listening || state.questionPending || voiceQuestionActive() || state.demo) return;
  await operate(async token => {
    disarmVoiceWake();
    state.voiceStartNeedsGesture = false;
    stopSpeech();
    state.guide = 'paused';
    state.result = null;
    state.lastAnswer = '';
    if (transport.connected) await transport.control('guide.stop', state.mode);
    if (token !== epoch) return;
    state.guide = 'idle';
    state.mode = mode;
    state.currentTurn = null;
    state.result = null;
    state.lastAnswer = '';
    setStatus(`${modes[mode].name}을 선택했어요.`);
  });
}

function receiveBackend(next) {
  if (!next || state.demo) return;
  if (state.backend && next.session_id === state.backend.session_id && next.generation < state.backend.generation) return;
  const wasWaitingForVideo = state.backend?.scene_waiting_for_video === true;
  state.backend = next;
  if (state.videoStale) { render(); return; }
  if (transport.connected && next.video_frames > 0 && next.last_video_age_ms > VIDEO_STALE_MS) {
    const hadWork = state.guide !== 'idle' || state.listening || state.questionPending || state.voiceWakeRequested
      || next.guide_active || next.question_recording || next.question_draining || next.busy;
    ++epoch;
    state.videoStale = true;
    state.actionBusy = false;
    state.guide = 'idle';
    state.listening = false;
    state.questionPending = false;
    disarmVoiceWake();
    state.voiceStartNeedsGesture = false;
    state.result = null;
    state.lastAnswer = '';
    state.currentTurn = null;
    clearTimeout(questionTimer);
    clearTimeout(pendingTimer);
    setMic(false);
    stopSpeech();
    setStatus('카메라 영상이 끊겨 안내를 멈췄어요. 카메라 다시 연결을 눌러 주세요.', true);
    if (hadWork) void transport.control('guide.stop', state.mode).catch(() => {});
    render();
    return;
  }
  const previousVoicePhase = state.voiceWakePhase;
  updateVoiceWake(next);
  if (state.voiceWakePhase === 'question' && previousVoicePhase !== 'question' && !state.listening) {
    stopSpeech();
    setStatus('질문을 듣고 있어요.');
  }
  if (state.mode === 'scene' && next.mode === 'scene') {
    state.guide = next.paused ? 'paused' : next.guide_active && aiConfigured() ? 'active' : 'idle';
    if (sceneRunning() && next.scene_waiting_for_video) {
      if (!wasWaitingForVideo) {
        state.result = null;
        state.lastAnswer = '';
        stopSpeech();
        setStatus('영상이 흔들려 잠시 기다리고 있어요. 카메라를 고정하면 자동으로 이어가요.');
      }
      render();
      return;
    }
    if (wasWaitingForVideo) setStatus('주변을 다시 확인하고 있어요.');
    $('remoteAudio').muted = shouldMuteRemote();
  }
  if (next.last_error && next.mode === state.mode) {
    const newError = state.error !== String(next.last_error);
    if (aiConfigured() && state.guide === 'active') state.guide = 'paused';
    setMic(false);
    state.listening = false;
    state.questionPending = false;
    state.result = null;
    state.lastAnswer = '';
    state.currentTurn = null;
    clearTimeout(pendingTimer);
    clearTimeout(questionTimer);
    if (newError) stopSpeech();
    setStatus(String(next.last_error), true);
    if (newError && aiConfigured()) speak('안내를 멈췄어요. ' + String(next.last_error));
    render();
    return;
  }
  const result = next.latest_result;
  if (result && typeof result.text === 'string') {
    const id = result.id || `${result.turn_id || ''}:${result.created_at || ''}:${result.text}`;
    if (id !== state.lastResultId && result.mode === state.mode && next.mode === state.mode) {
      if (result.source === 'ai' || result.source === 'startup') {
        const currentTurnMatches = result.source === 'startup' ? state.voiceWakeEnabled : Boolean(result.turn_id && ((result.turn_id === next.current_turn_id && (!state.currentTurn || result.turn_id === state.currentTurn))
          || (result.scene_observation && sceneRunning() && !state.currentTurn)));
        if (!transport.connected || state.connection !== 'connected' || state.listening || voiceQuestionActive() || state.guide === 'paused' || !currentTurnMatches) { render(); return; }
        state.lastResultId = id;
        state.result = result;
        state.lastAnswer = result.text;
        state.questionPending = false;
        state.currentTurn = null;
        clearTimeout(pendingTimer);
        const attention = next.paused && ['obstacle','uncertain'].includes(result.kind);
        if (attention) {
          state.guide = 'paused';
          try { navigator.vibrate?.([200,100,200]); } catch {}
          setStatus('안내를 일시정지했어요. ' + result.text, false, true);
          speak('잠시 멈춰 주세요. ' + result.text, id);
        } else if (!preferences.voice || (state.voiceStartNeedsGesture && result.scene_observation)) {
          setStatus(result.text);
        } else {
          if (result.source === 'startup') setStatus(result.audio_available ? result.text : '시작 음성을 준비하지 못했어요. 화면에서 헤이 센스 사용 방법을 확인해 주세요.');
          else setStatus('안내 결과가 도착했어요.');
          if (result.audio_source === 'http-wav') void playServerAnswer(result);
          else if (result.audio_available === false) speak(result.text, id);
          else if ($('remoteAudio').paused) void resumeRemoteAudio(true);
        }
      } else if (result.text) {
        state.lastResultId = id;
        state.questionPending = false;
        setStatus(result.text);
      }
    }
  }
  render();
}

transport.addEventListener('status', event => {
  const detail = event.detail || {};
  state.connection = detail.state || state.connection;
  if (detail.health) state.health = detail.health;
  if (detail.state === 'connected' && !transport.usesHttpAnswerAudio) void resumeRemoteAudio();
  if (detail.message && !state.demo) setStatus(detail.message, ['error', 'failed'].includes(detail.state));
  if (['error', 'failed', 'disconnected', 'closed', 'reconnecting'].includes(detail.state)) {
    state.listening = false;
    state.questionPending = false;
    state.voiceStartNeedsGesture = false;
    if (detail.state === 'reconnecting') updateVoiceWake();
    else disarmVoiceWake();
    if (state.guide === 'active') state.guide = 'paused';
    state.result = null;
    state.lastAnswer = '';
    state.currentTurn = null;
    clearTimeout(questionTimer);
    clearTimeout(pendingTimer);
    stopSpeech();
    if (['error','failed','disconnected','reconnecting'].includes(detail.state)) { setStatus('연결이 끊겼어요. 안내를 멈췄어요.', true, true); speak('연결이 끊겼어요. 안내를 멈췄어요.'); }
  }
  render();
});
transport.addEventListener('state', event => receiveBackend(event.detail));
transport.addEventListener('error', event => {
  state.connection = 'error';
  state.questionPending = false;
  state.listening = false;
  disarmVoiceWake();
  state.voiceStartNeedsGesture = false;
  state.result = null;
  state.lastAnswer = '';
  state.currentTurn = null;
  clearTimeout(questionTimer);
  clearTimeout(pendingTimer);
  if (state.guide === 'active') state.guide = 'paused';
  stopSpeech();
  setStatus(event.detail?.message || '연결 오류가 발생했어요.', true);
  render();
});
transport.addEventListener('audio', event => {
  if (state.demo || !event.detail?.stream) return;
  $('remoteAudio').srcObject = event.detail.stream;
  if (!transport.usesHttpAnswerAudio) void resumeRemoteAudio();
});

function connectFromUi() {
  suppressAutoVoiceStart();
  unlockAnswerAudio();
  return operate(async token => {
    const reconnect = state.videoStale;
    if (reconnect) {
      state.backend = null;
      state.videoStale = false;
      state.lastResultId = null;
      state.connection = 'connecting';
      $('remoteAudio').srcObject = null;
      releaseMedia();
      await transport.disconnect(false);
      if (token !== epoch) return;
    }
    if (await ensureConnected(token)) {
      setStatus(reconnect ? '카메라를 다시 연결했어요. 필요할 때 다시 확인해 주세요.' : 'PC와 연결됐어요. 주변 확인이나 음성 질문을 선택해 주세요.');
    }
  });
}
$('quickConnect').addEventListener('click', connectFromUi);
$('enableCamera').addEventListener('click', () => {
  suppressAutoVoiceStart();
  void operate(token => ensureMedia(token));
});
$('guideAction').addEventListener('click', guideAction);
$('questionAction').addEventListener('click', questionAction);
$('switchCamera').addEventListener('click', switchCamera);
$('stopAction').addEventListener('click', () => stopEverything());
$('disconnectAction').addEventListener('click', () => stopEverything());
$('demoToggle').addEventListener('click', async () => {
  $('settingsDialog').close();
  await toggleDemo();
  $('guidePanel').focus();
});
$('repeatAction').addEventListener('click', replayAnswer);
$('returnGuide').addEventListener('click', () => changeMode('scene'));
document.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', async () => {
  await changeMode(button.dataset.mode);
  $('moreModes').open = false;
  if (!$('guideAction').hidden) $('guideAction').focus();
}));
document.querySelectorAll('[data-scenario]').forEach(button => button.addEventListener('click', () => {
  if (!state.demo) return;
  state.scenario = button.dataset.scenario;
  state.demoPaused = state.scenario === 'uncertain';
  speakDemo();
  render();
}));
$('voiceToggle').addEventListener('click', () => {
  preferences.voice = !preferences.voice;
  if (!preferences.voice) stopSpeech();
  else unlockAnswerAudio();
  savePreferences(preferences);
  render();
  setStatus(preferences.voice ? '음성 안내를 켰어요.' : '음성 안내를 껐어요.');
});
$('voiceWakeToggle').addEventListener('click', toggleVoiceWake);
$('settingsTrigger').addEventListener('click', () => $('settingsDialog').showModal());
$('settingsDone').addEventListener('click', () => $('settingsDialog').close());
$('contrastSetting').addEventListener('change', event => { preferences.contrast = event.target.checked; savePreferences(preferences); renderPreferences(); });
document.querySelectorAll('input[name=textSize]').forEach(input => input.addEventListener('change', () => { preferences.textSize = input.value; savePreferences(preferences); renderPreferences(); }));
$('voiceSpeed').addEventListener('input', event => { preferences.voiceSpeed = Number(event.target.value); savePreferences(preferences); renderPreferences(); });
$('connectionTrigger').addEventListener('click', () => { renderConnection(); $('connectionDialog').showModal(); });
$('connectAction').addEventListener('click', connectFromUi);
$('refreshHealth').addEventListener('click', () => void refreshHealth().then(maybeStartVoiceWake));
$('testAudioAction').addEventListener('click', () => operate(async () => {
  stopSpeech();
  if (!preferences.voice) { preferences.voice = true; savePreferences(preferences); renderPreferences(); }
  void resumeRemoteAudio(true);
  await transport.control('audio.test', state.mode);
  setStatus('연결 확인음을 요청했어요. 이 소리는 통신 테스트용이에요.');
}));
$('audioResume').addEventListener('click', replayAnswer);
document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('click', event => {
  if (event.target !== dialog) return;
  const rectangle = dialog.getBoundingClientRect();
  if (event.clientX < rectangle.left || event.clientX > rectangle.right || event.clientY < rectangle.top || event.clientY > rectangle.bottom) dialog.close();
}));
window.addEventListener('pagehide', () => {
  epoch += 1;
  suppressAutoVoiceStart();
  state.listening = false;
  disarmVoiceWake();
  clearTimeout(questionTimer);
  clearTimeout(pendingTimer);
  stopSpeech();
  void answerAudio.close();
  setMic(false);
  releaseMedia();
  transport.disconnect().catch(() => {});
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden && state.listening) finishQuestion();
  syncMicrophone();
  if (!document.hidden) void maybeStartVoiceWake();
});
window.addEventListener('hashchange', () => {
  if (!acceptScannedPairing()) return;
  if (!transport.connected) {
    setStatus(!transport.hasPairingCode ? PAIRING_REQUIRED_MESSAGE
      : voiceWakeAvailable() && preferences.voice ? 'QR을 확인했어요. 자동 연결을 준비하고 있어요.'
      : 'QR을 확인했어요. 주변 확인을 시작할 수 있어요.');
  }
  render();
  void maybeStartVoiceWake();
});
window.addEventListener('pageshow', event => {
  if (event.persisted) stopEverything({ message: '페이지로 돌아왔어요. 카메라와 연결을 다시 시작해 주세요.' });
});
render();
void refreshHealth().then(maybeStartVoiceWake);
