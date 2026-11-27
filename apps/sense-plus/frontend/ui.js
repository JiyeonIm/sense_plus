export const $ = (id) => document.getElementById(id);

export function icon(element, name) {
  element.querySelector('use')?.setAttribute('href', `#i-${name}`);
}

export function readPreferences() {
  const defaults = { voice: true, contrast: true, textSize: 'large', voiceSpeed: 1 };
  try {
    const saved = JSON.parse(localStorage.getItem('sense-plus-v2-preferences') || '{}');
    return {
      voice: typeof saved.voice === 'boolean' ? saved.voice : defaults.voice,
      contrast: typeof saved.contrast === 'boolean' ? saved.contrast : defaults.contrast,
      textSize: ['normal', 'large', 'largest'].includes(saved.textSize) ? saved.textSize : defaults.textSize,
      voiceSpeed: Math.min(1.3, Math.max(0.7, Number(saved.voiceSpeed) || 1)),
    };
  } catch { return defaults; }
}

export function savePreferences(preferences) {
  try { localStorage.setItem('sense-plus-v2-preferences', JSON.stringify(preferences)); } catch { /* Storage can be unavailable in private browsing. */ }
}

export function mediaErrorMessage(error) {
  if (error?.name === 'NotAllowedError' || error?.name === 'PermissionDeniedError') return '카메라·마이크 권한이 허용되지 않았어요. 브라우저의 사이트 설정에서 권한을 확인해 주세요.';
  if (error?.name === 'NotFoundError' || error?.name === 'DevicesNotFoundError') return '사용할 수 있는 카메라나 마이크를 찾지 못했어요. 장치 연결을 확인해 주세요.';
  if (error?.name === 'NotReadableError' || error?.name === 'TrackStartError') return '다른 앱이 카메라나 마이크를 사용 중일 수 있어요. 해당 앱을 닫고 다시 시도해 주세요.';
  if (error?.name === 'OverconstrainedError') return '이 기기에서 선택한 카메라를 열 수 없어요. 다른 카메라로 다시 시도해 주세요.';
  return error?.message || '장치를 연결하지 못했어요. 잠시 후 다시 시도해 주세요.';
}

export const modes = {
  guide: { name: '주변 이동 안내', title: '주변을 함께 살펴요.', intro: '카메라로 주변을 확인하고, 필요한 안내를 음성으로 받아보세요.', action: '안내 시작' },
  scene: { name: '주변 설명', title: '무엇이 보이나요?', intro: '카메라에 보이는 주변 상황을 확인해요.', action: '주변 확인' },
  find: { name: '물건 찾기', title: '어떤 물건을 찾나요?', intro: '찾는 물건을 음성으로 말하고, 주변을 비춰 주세요.', action: '화면 확인' },
  read: { name: '정보 읽기', title: '글자를 비춰 주세요.', intro: '읽고 싶은 문서나 표시를 카메라에 담아 주세요.', action: '정보 읽기' },
};

export const scenarios = {
  door: { title: '이동 안내', label: '현재 보이는 상황', observation: '오른쪽에 출입구가 보여요.', guidance: '화면 기준 오른쪽을 확인해 주세요.', icon: 'eye', overlay: '시연 예시 · 오른쪽 출입구' },
  obstacle: { title: '장애물 알림', label: '현재 보이는 상황', observation: '화면 앞쪽에 의자가 보여요.', guidance: '잠시 멈춰 주변을 확인해 주세요.', icon: 'alert', overlay: '시연 예시 · 앞쪽 장애물' },
  uncertain: { title: '안내 일시정지', label: '주변 확인이 어려워요', observation: '화면이 어두워 확인하기 어려워요.', guidance: '잠시 멈춘 뒤 카메라 방향을 조정해 주세요.', icon: 'camera', overlay: '시연 예시 · 시야 확인 어려움' },
};
