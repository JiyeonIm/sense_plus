'use strict';

const $ = id => document.getElementById(id);
const formatCount = number => Number(number || 0).toLocaleString('ko-KR');
const formatTime = value => value ? new Date(value).toLocaleTimeString('ko-KR', {hour12: false}) : '—';
const formatAge = value => value == null ? '—' : (value / 1000).toFixed(2) + '초 전';
const formatSize = size => size ? size.join(' × ') : '—';
const audioProviderLabels = {'windows-tts': 'Windows 한국어 음성', 'browser-tts': '휴대폰 음성 합성', 'team-api': '팀 API 음성'};
const PREVIEW_MAX_AGE_MS = 2500;
const csvColumns = ['seq', 'at', 'event', 'session_id', 'mode', 'action', 'turn_id', 'result_id', 'duration_ms', 'status', 'source'];
const transportLabels = {
  new: '연결 준비', connecting: '연결 중', connected: '미디어 수신',
  failed: '연결 실패', disconnected: '연결 끊김', closed: '연결 종료'
};

let latest = [];
let previous = new Map();
let selectedSession = null;
let frameRequest = null;
let frameUrl = null;
let requestBusy = false;

function clearFrame(message) {
  if (frameUrl) URL.revokeObjectURL(frameUrl);
  frameUrl = null;
  $('frame').hidden = true;
  $('frame').removeAttribute('src');
  $('frameMsg').hidden = false;
  if ($('frameMsg').textContent !== message) $('frameMsg').textContent = message;
}

function updateFrameControls() {
  const autoPreview = $('showFrame').checked;
  $('videoMeta').hidden = !autoPreview;
  if (autoPreview) {
    const size = selectedSession?.preview_size;
    $('videoMeta').textContent = (size ? '미리보기 ' + formatSize(size) : '자동 미리보기') + ' · 약 1.3회/초 갱신';
  }
}

function frameIdleMessage() {
  if (selectedSession?.transport !== 'connected') return '영상 연결 대기 중';
  return $('showFrame').checked ? '자동 미리보기 대기 중' : '미리보기 꺼짐';
}

function resetFrame(message = frameIdleMessage()) {
  const pending = frameRequest;
  frameRequest = null;
  pending?.controller.abort();
  clearFrame(message);
  updateFrameControls();
}

function displayFrame(image) {
  if (frameUrl) URL.revokeObjectURL(frameUrl);
  frameUrl = URL.createObjectURL(image);
  $('frame').src = frameUrl;
  $('frame').hidden = false;
  $('frameMsg').hidden = true;
}

function selectFrameSession(session) {
  const changed = selectedSession?.id !== session?.id
    || (selectedSession?.transport === 'connected') !== (session?.transport === 'connected');
  selectedSession = session;
  if (changed) resetFrame();
  else updateFrameControls();
}

function isCurrentFrameRequest(request) {
  return request === frameRequest && $('session').value === request.sessionId
    && selectedSession?.transport === 'connected' && selectedSession.id === request.sessionId
    && $('showFrame').checked;
}

function eventIsError(event) {
  return /error|failed/.test(event.event) || /error|failed/.test(event.status || '');
}

function filteredEvents() {
  const query = $('search').value.trim().toLowerCase();
  const filter = $('eventFilter').value;
  const prefixes = {
    connection: /^(session|rtc)\./,
    media: /^(video|audio)\./,
    control: /^(control|question|guide)\./,
    analysis: /^(analysis|result)\./
  };
  return latest.filter(event => {
    if (query && !Object.values(event).some(value => String(value ?? '').toLowerCase().includes(query))) return false;
    if (filter === 'errors') return eventIsError(event);
    return !filter || prefixes[filter].test(event.event);
  }).reverse();
}

function renderEvents() {
  const rows = filteredEvents();
  const body = $('events');
  body.replaceChildren();
  $('export').disabled = rows.length === 0;
  for (const event of rows) {
    const row = body.insertRow();
    row.classList.toggle('error', eventIsError(event));
    const info = [
      event.action, event.status, event.source,
      event.turn_id && '질문 ' + event.turn_id,
      event.result_id && '결과 ' + event.result_id,
      event.duration_ms != null && event.duration_ms + ' ms'
    ].filter(Boolean).join(' · ');
    for (const value of [formatTime(event.at), event.event, event.session_id?.slice(0, 8), event.mode, info]) {
      row.insertCell().textContent = value || '—';
    }
  }
  if (!rows.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = 5;
    cell.textContent = '표시할 이벤트 없음';
  }
}

function transportLabel(session) {
  if (session.transport === 'connected' && (session.video_age_ms ?? Infinity) > PREVIEW_MAX_AGE_MS) return '최근 영상 없음';
  return transportLabels[session.transport] || session.transport;
}

function arrivalLabel(session) {
  switch (session.av_arrival_state) {
    case 'observed': return session.av_arrival_gap_ms + ' ms';
    case 'audio_muted': return '질문 음성 수신 대기';
    case 'stale': return '최근 미디어 부족';
    default: return '수신 대기';
  }
}

function renderDetails(session, fps) {
  const list = $('detail');
  list.replaceChildren();
  const values = session ? {
    '세션 ID': session.id,
    '연결 상태': transportLabel(session),
    '시작 시각': formatTime(session.created_at),
    '모드': session.mode,
    '최근 영상 / 음성': formatAge(session.video_age_ms) + ' / ' + formatAge(session.audio_age_ms),
    '영상·음성 도착차': arrivalLabel(session),
    '수신 영상 해상도': formatSize(session.video_size),
    '카메라 FPS 목표': '30 fps',
    '서버 수신 FPS': fps == null ? '—' : fps.toFixed(1) + ' fps',
    '수신 프레임': formatCount(session.video_frames),
    '수신 오디오 프레임': formatCount(session.audio_frames),
    '수신 오디오 샘플': formatCount(session.audio_samples),
    '변환 오디오 길이': session.audio_seconds.toFixed(2) + '초',
    '질문 녹음': session.recording ? '입력 중' : session.draining ? '마지막 패킷 대기' : '대기',
    '질문 ID': session.turn_id || '—',
    '결과 ID': session.result_id || '—',
    '결과 출처': session.result_source || '—',
    '답변 음성': audioProviderLabels[session.audio_provider] || '—',
    'AI가 사용한 영상': session.last_ai_frame_number == null ? '—' :
      formatCount(session.last_ai_frame_number) + '번 · ' + formatTime(session.last_ai_frame_received_at),
    'AI 응답 시간': session.analysis_ms == null ? '—' : session.analysis_ms + ' ms',
    '처리 상태': session.error ? '확인 필요' : session.busy ? '처리 중' : session.paused ? '일시정지' : '대기/수신'
  } : {'연결 상태': '연결 대기', '카메라 FPS 목표': '30 fps', '서버 수신 FPS': '—'};
  for (const [key, value] of Object.entries(values)) {
    const term = document.createElement('dt');
    const description = document.createElement('dd');
    term.textContent = key;
    description.textContent = value;
    list.append(term, description);
  }
}

function renderAudio(session) {
  const active = session?.audio_level_active;
  const rms = active ? session.audio_rms : 0;
  $('rms').value = rms;
  $('rmsText').textContent = active ? (rms * 100).toFixed(1) + '% RMS' : '대기';
  const canvas = $('waveform');
  const context = canvas.getContext('2d');
  if (!context) return;
  const {width, height} = canvas;
  const middle = height / 2;
  context.clearRect(0, 0, width, height);
  context.fillStyle = '#344a58';
  context.fillRect(0, middle, width, 1);
  if (!active) return;
  const peaks = session.audio_waveform;
  const step = width / 40;
  const start = width - peaks.length * step;
  context.fillStyle = '#f1d243';
  peaks.forEach((peak, index) => {
    const extent = peak * (middle - 4);
    context.fillRect(start + index * step + 1, middle - extent, step - 2, Math.max(1, extent * 2));
  });
}

function renderPairing(monitor) {
  const phoneMode = monitor.pairing_required === true;
  const link = $('userUrl');
  link.hidden = phoneMode;
  link.textContent = phoneMode ? '' : monitor.user_url;
  if (phoneMode) link.removeAttribute('href');
  else link.href = monitor.user_url;
  $('pairHint').textContent = phoneMode
    ? '같은 Wi-Fi에 연결한 휴대폰의 카메라 앱으로 이 QR을 스캔하세요.'
    : '이 PC에서만 접속할 수 있습니다.';
  const qr = $('pairQr');
  const source = phoneMode ? '/api/pairing-qr.png?v=' + encodeURIComponent(monitor.pairing_qr_version) : null;
  if (source && qr.getAttribute('src') !== source) qr.src = source;
  if (!source) qr.removeAttribute('src');
  qr.hidden = !source;
}

function renderSessionOptions(sessions) {
  const select = $('session');
  const options = sessions.map(session => [session.id, session.id.slice(0, 8) + ' · ' + transportLabel(session)]);
  if (!options.length) options.push(['', '연결된 기기 없음']);
  const currentOptions = Array.from(select.options, option => [option.value, option.textContent]);
  if (JSON.stringify(options) === JSON.stringify(currentOptions)) return;
  const selection = select.value;
  select.replaceChildren(...options.map(([value, label]) => new Option(label, value)));
  if (options.some(([value]) => value === selection)) select.value = selection;
}

async function renderPreview() {
  const session = selectedSession;
  if (!$('showFrame').checked || session?.transport !== 'connected') return;
  if (session.video_age_ms == null || session.video_age_ms > PREVIEW_MAX_AGE_MS) {
    resetFrame('최근 수신 영상 없음');
    return;
  }
  if (frameRequest) return;
  const request = {sessionId: session.id, controller: new AbortController()};
  frameRequest = request;
  try {
    const response = await fetch('/api/frame/' + encodeURIComponent(session.id), {
      cache: 'no-store',
      signal: AbortSignal.any([request.controller.signal, AbortSignal.timeout(PREVIEW_MAX_AGE_MS)])
    });
    if (!isCurrentFrameRequest(request)) return;
    if (!response.ok) {
      clearFrame('최근 수신 영상 없음');
      return;
    }
    const image = await response.blob();
    if (isCurrentFrameRequest(request)) displayFrame(image);
  } catch {
    if (isCurrentFrameRequest(request)) clearFrame('최근 프레임 조회 실패');
  } finally {
    if (request === frameRequest) frameRequest = null;
  }
}

async function poll() {
  if (requestBusy) return;
  requestBusy = true;
  try {
    const response = await fetch('/api/monitor', {cache: 'no-store', signal: AbortSignal.timeout(3500)});
    if (!response.ok) throw new Error('Monitor request failed');
    const monitor = await response.json();
    $('status').textContent = monitor.rtc_available ? 'FastRTC 준비됨' : 'FastRTC 미실행';
    renderPairing(monitor);
    $('sessions').textContent = formatCount(monitor.sessions.length);
    $('frames').textContent = formatCount(monitor.sessions.reduce((total, session) => total + session.video_frames, 0));
    $('samples').textContent = formatCount(monitor.sessions.reduce((total, session) => total + session.audio_samples, 0));
    $('ai').textContent = monitor.local_models?.vision
      ? ['로컬 영상', monitor.local_models.stt && 'STT', monitor.local_models.tts && 'TTS'].filter(Boolean).join(' · ')
      : monitor.ai_configured ? '팀 API 설정됨' : '미연결';

    renderSessionOptions(monitor.sessions);
    const session = monitor.sessions.find(item => item.id === $('session').value);
    selectFrameSession(session);
    const now = performance.now();
    const baseline = session && previous.get(session.id);
    const fallbackFps = baseline && now > baseline.at && session.video_age_ms != null && session.video_age_ms <= PREVIEW_MAX_AGE_MS
      ? Math.max(0, (session.video_frames - baseline.frames) * 1000 / (now - baseline.at))
      : null;
    const fps = session && Object.hasOwn(session, 'video_fps')
      ? (Number.isFinite(session.video_fps) ? session.video_fps : null)
      : fallbackFps;
    previous = new Map(monitor.sessions.map(item => [item.id, {frames: item.video_frames, at: now}]));
    renderAudio(session);
    renderDetails(session, fps);
    latest = monitor.events;
    renderEvents();
    void renderPreview();
  } catch {
    $('status').textContent = '관제 서버 연결 끊김';
    previous.clear();
    renderAudio(null);
    renderDetails(null, null);
    selectedSession = null;
    resetFrame('서버 연결 끊김');
  } finally {
    requestBusy = false;
  }
}

function csvCell(value) {
  let text = String(value ?? '');
  if (/^[\s]*[=+\-@]|^[\t\r\n]/u.test(text)) text = "'" + text;
  return '"' + text.replaceAll('"', '""') + '"';
}

$('showFrame').addEventListener('change', () => {
  resetFrame();
  void renderPreview();
});
$('session').addEventListener('change', () => {
  selectedSession = null;
  resetFrame('세션 전환 중');
  renderAudio(null);
  poll();
});
$('search').addEventListener('input', renderEvents);
$('eventFilter').addEventListener('change', renderEvents);
$('export').addEventListener('click', () => {
  const rows = filteredEvents().map(event => csvColumns.map(column => csvCell(event[column])).join(','));
  const csv = [csvColumns.join(','), ...rows].join('\r\n');
  const url = URL.createObjectURL(new Blob(['\ufeff', csv], {type: 'text/csv;charset=utf-8'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'sense-events.csv';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
$('showFrame').checked = false;
renderEvents();
poll();
const pollTimer = setInterval(poll, 750);
window.addEventListener('pagehide', () => {
  clearInterval(pollTimer);
  $('showFrame').checked = false;
  resetFrame('종료');
});
