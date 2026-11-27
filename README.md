# SENSE+

시각장애인과 저시력 사용자가 휴대폰 카메라와 음성으로 주변 정보를 확인하는 개발 프로젝트입니다. 웹앱과 PC 관제는 [`apps/sense-plus`](apps/sense-plus/)에 있고, 기존 팀 모델 실험 코드는 저장소 루트의 `core/`, `models/`, `test*.py`에 있습니다.

## 팀 인수인계

1. `apps/sense-plus`에서 Windows는 `setup_windows.bat`를 한 번 실행한 뒤 `start_windows.bat`를 실행합니다. Python 3.11 또는 3.12가 필요합니다. Mac과 휴대폰 연결 절차는 [빠른 시작](apps/sense-plus/START_HERE_KO.txt)에 있습니다.
2. PC 관제는 [127.0.0.1:8766](http://127.0.0.1:8766/), PC에서 여는 사용자 앱은 [127.0.0.1:8765](http://127.0.0.1:8765/)입니다. iPhone에서 처음 시험할 때는 [iPhone 인증서 설치와 연결](apps/sense-plus/README_KO.md#iphone-인증서-설치와-연결) 순서대로 진행합니다.
3. 기본 설정은 영상·음성 연결 시험입니다. Windows에서 실제 장면 분석에 쓰는 Ollama, 한국어 STT·TTS 설정은 [모델 연동 규격](apps/sense-plus/docs/INTEGRATION.md#windows-로컬-모델)을 따릅니다.

## 구현 상태

- **휴대폰** — 색과 텍스트로 구분한 큰 버튼으로 주변 확인, 음성 질문, 종료를 조작합니다. 주변 확인은 한 번 누르면 장면이 바뀔 때마다 이어집니다. 영상이 흐리면 안정될 때까지 기다리고, 영상 수신이 끊기면 멈춥니다.
- **PC 관제** — 영상·음성은 FastRTC/WebRTC로 수신합니다. 카메라 목표는 30fps이며 관제에 실제 수신 FPS, 선택된 분석 프레임, 마이크 파형, 요청·응답 ID, 이벤트 로그와 CSV가 표시됩니다.
- **로컬 모델** — Ollama의 Qwen3-VL, Whisper small 한국어 전사, Windows Heami 음성을 연결했습니다. 팀 데이터셋과 배포할 STT·TTS 모델이 준비되면 `SpeechToText`, `TextToSpeech`, `AIAdapter` 규격으로 교체할 수 있습니다.
- **검증 범위** — Windows에서 Python 테스트 49개, Node 테스트 6개를 통과했습니다. 합성 입력과 실제 로컬 모델로 연속 장면 분석과 음성 왕복을 확인했습니다. 최신 iPhone 실기기 음성 왕복, 장시간 수신, VoiceOver·TalkBack, 보행 안전성은 재검증이 필요합니다. [검증 기록](apps/sense-plus/TEST_STATUS.md)

[한국어 사용 설명서](apps/sense-plus/README_KO.md) · [연동 규격](apps/sense-plus/docs/INTEGRATION.md)
