# SENSE+ 팀 서버 연결 규격

이 문서는 현재 SENSE+ 개발 서버의 규격입니다. FastRTC 자체의 표준 API와 SENSE+가 추가한 제어 API를 구분합니다. 외부 팀 AI 서버가 이 규격을 이미 지원한다고 가정하지 않습니다.

## 1. 구성과 책임

| 구성 | 역할 | 주요 파일 |
|---|---|---|
| 휴대폰/PC 브라우저 | 카메라·마이크 권한, 로컬 화면, WebRTC 송수신, 상태 표시, 결과 재생 | `frontend/` |
| SENSE+ 서버 | FastRTC 연결, 세션, 질문·프레임 묶기, 결과·음성 반환 | `senseplus/server.py`, `rtc.py`, `core.py` |
| AI 어댑터 | 팀 AI HTTP 요청·응답 검증과 음성 형식 변환 | `senseplus/adapter.py` |
| 팀 AI 서버 | 입력 장면·음성 이해, 실제 안내 문장, 필요 시 TTS | 별도 팀 구현 |
| 실행 도구 | 로컬 실행, 독립 환경 설치, HTTPS 입력 | `run.py`, 설치 스크립트 |

브라우저에서 팀 AI 키를 다루지 않습니다. 팀 AI 서버 주소와 키는 SENSE+ 서버의 환경 변수로만 읽습니다.

## 2. 상태와 연결

### `GET /api/health`

인증 없이 서버 기능 상태를 읽습니다. 기본 응답에 아래 값이 들어갑니다.

```json
{
  "ok": true,
  "version": "0.2.0",
  "rtc_available": true,
  "ai_configured": false,
  "mode": "live",
  "detail": "서버 상태 설명",
  "rtc_configuration": {"iceServers": []}
}
```

`rtc_available`은 실시간 서버를 사용할 수 있는지를 뜻합니다. 브라우저와 실제 연결되었다는 뜻은 아닙니다. `ai_configured`도 주소가 설정되어 있다는 뜻이며 외부 AI 서버의 정상 응답을 보장하지 않습니다.

표준 라이브러리 미리보기인 `preview.py`는 `rtc_available:false`, `ai_configured:false`, `mode:"preview"`를 반환합니다.

### `POST /api/sessions`

빈 JSON 객체로 세션을 생성합니다. `/api/health`의 `pairing_required`가 참이면 브라우저가 관제 QR에서 읽은 값을 `X-Sense-Pairing` 헤더로 보냅니다. 응답은 `session_id`와 `token`입니다. 이후 해당 세션 요청의 헤더에 `Authorization: Bearer <token>`을 보냅니다. 토큰을 URL·공유 ZIP·로그에 넣지 않습니다.

### `POST /webrtc/offer`

FastRTC SDP 협상 경로입니다. SENSE+에서는 생성한 세션 토큰과 `webrtc_id`를 연결합니다.

```json
{
  "sdp": "브라우저가 만든 SDP",
  "type": "offer",
  "webrtc_id": "생성한 session_id"
}
```

브라우저는 같은 연결에 카메라 영상과 마이크 음성을 추가합니다. 서버가 보낸 음성 트랙을 수신해 재생합니다. 기본 ICE 목록은 비어 있으며 다른 네트워크용 STUN/TURN은 별도 구성입니다.

## 3. SENSE+ 제어 API

아래 경로는 FastRTC를 설치한다고 생기는 API가 아니라 이 프로젝트에서 추가한 API입니다.

### `POST /api/sessions/{session_id}/control`

```json
{
  "action": "question.start",
  "mode": "guide",
  "turn_id": "질문별 고유 문자열"
}
```

| action | 동작 |
|---|---|
| `guide.start` | 안내 처리 시작 |
| `guide.pause` | 안내 일시정지, 진행 중 요청·대기 음성 정리 |
| `guide.resume` | 안내 재시작 |
| `guide.stop` | 안내 정지 |
| `question.start` | 안정되고 선명한 최신 프레임을 자동 선택하고 질문 음성 수집 시작 |
| `question.end` | 지정 질문을 마치고 늦은 음성 수신 구간 후 AI 처리 |
| `audio.test` | 짧은 서버 확인음을 WebRTC 오디오 트랙으로 전송 |

서버는 `guide`, `scene`, `find`, `read`를 허용합니다. 화면 기본값인 `scene`은 장면이 바뀔 때마다 연속 확인합니다. `guide`는 기존 연속 안내 모드입니다. `find`와 `read`는 한 번 확인합니다. 질문 시작·종료에는 같은 `turn_id`를 사용합니다. `turn_id`는 영문·숫자·밑줄·하이픈으로 이루어진 최대 80자 문자열입니다. 제어 API 응답은 갱신된 세션 상태 객체입니다.

`audio.test` 결과의 `source`는 `transport-test`입니다. 실제 장면 인식, 방향 판단, TTS 성공으로 기록하지 않습니다.

### `GET /api/sessions/{session_id}/state`

세션 상태를 읽습니다. 주요 값은 다음과 같습니다.

- `transport`, `video_frames`, `audio_frames`, `audio_samples`: 전송 상태와 서버 수신 카운터.
- `last_video_age_ms`, `last_audio_age_ms`: 마지막 미디어 수신 뒤 지난 시간.
- `question_recording`, `question_draining`, `question_audio_seconds`, `audio_truncated`: 질문 수집 상태.
- `current_turn_id`, `busy`, `last_error`, `latest_result`: 처리 상태와 결과.
- `guide_active`, `paused`, `mode`, `scene_waiting_for_video`: 안내 조작과 안정 프레임 대기 상태.
- `generation`: 시작·종료 등으로 세션 작업이 무효화될 때 증가하는 값. 브라우저는 이전 작업에서 도착한 상태를 무시합니다.

카운터는 수신 증거이며 내용 인식의 정확도를 뜻하지 않습니다. 마이크 비활성화 상태에 무음 패킷이 올 수도 있습니다.

`latest_result.source`는 `system`, `transport-test`, `ai`처럼 결과의 출처를 구분합니다. `audio_source`가 `http-wav`이면 서버 합성 음성을 HTTPS로 가져옵니다. `webrtc`는 실시간 음성 경로이며 `browser-tts`는 브라우저가 텍스트로 합성하는 경로입니다.

브라우저는 `/api/health`의 `answer_audio_http`를 확인한 뒤, Web Audio를 지원할 때만 `POST /api/sessions`에 `X-Sense-Answer-Audio: http`를 보냅니다. `http-wav` 응답은 같은 세션의 Bearer 토큰으로 `GET /api/sessions/{session_id}/audio/{result_id}.wav`에서 가져옵니다. WAV는 24kHz 모노 16비트 PCM입니다. 서버는 최신 답변 하나만 메모리에 유지하고, 세션 종료·오류·새 결과가 도착하면 교체하거나 제거합니다. 브라우저의 첫 조작에서 음성 재생을 준비하고, 일시 중단되면 **음성 재생**으로 현재 답변을 다시 들을 수 있습니다. 기존 클라이언트와 서버 확인음은 WebRTC 경로를 사용합니다.

### `DELETE /api/sessions/{session_id}`

연결을 종료하고 해당 세션의 메모리와 대기 작업을 정리합니다. 브라우저가 비정상 종료되면 서버 TTL에 따라 세션을 정리합니다.

## 4. 팀 AI 서버로 보내는 요청

`SENSE_AI_URL`에 설정한 주소로 `POST application/json` 요청을 보냅니다. `SENSE_AI_KEY`가 있으면 `Authorization: Bearer ...`를 붙입니다. 브라우저 세션 토큰과는 별개입니다.

팀 코드가 Python 모듈로 제공되면 `senseplus/adapter.py`의 `AIAdapter.analyze(payload)`를 구현해 `SessionManager(config, adapter=...)`에 전달하고 `create_app(manager=...)`으로 서버를 만들 수 있습니다. `payload`는 아래 입력 스키마이며 `analyze`는 `AnalysisResult`를 반환합니다. 음성은 선택 사항이며 제공할 때는 24 kHz 모노 `int16` NumPy 배열을 사용합니다. HTTP 인터페이스를 쓰면 앱 코드 수정을 생략하고 `.env`의 `SENSE_AI_URL`에 같은 PC에서 실행한 모델 서버 주소를 설정할 수 있습니다.

```json
{
  "session_id": "현재 세션",
  "turn_id": "현재 질문 또는 안내 요청",
  "mode": "guide",
  "jpeg_base64": "JPEG 이미지의 base64 문자열",
  "image_captured_at": "2026-11-27T00:00:00+00:00",
  "audio_wav_base64": "WAV base64 또는 null",
  "sample_rate": 16000,
  "audio_truncated": false,
  "requested_at": "2026-11-27T00:00:01+00:00"
}
```

- `jpeg_base64`: 최대 640×480 범위로 축소된 JPEG. 원본 비율은 유지합니다.
- `audio_wav_base64`: 질문이 있으면 모노·16비트 PCM·16kHz WAV. 이미지 기반 자동 안내에서는 `null`일 수 있습니다.
- `sample_rate`: 질문 오디오 규격입니다. 오디오가 없는 요청에도 규격 값으로 들어갑니다.
- `image_captured_at`: 현재 구현에서는 **서버에 영상이 수신된 UTC 시각**입니다. 브라우저 카메라 원본의 촬영 시각이 아닙니다.
- `requested_at`: 요청을 만든 서버 UTC 시각입니다.
- `turn_id`: 결과를 현재 작업과 연결하기 위한 식별자입니다.

요청은 인메모리로 인코딩하며 이미지·음성을 파일로 저장하지 않습니다. 요청 JSON 크기는 2MB 이하로 제한합니다.

## 5. 팀 AI 서버의 응답

필수 값은 `text`입니다. 예시는 규격 설명용이며 실제 인식 결과가 아닙니다.

```json
{
  "text": "관찰한 내용에 근거한 안내 문장",
  "kind": "info",
  "direction": "none",
  "audio_wav_base64": null
}
```

| 값 | 규격 |
|---|---|
| `text` | 필수 문자열, 1~2000자 |
| `kind` | `info`, `direction`, `obstacle`, `uncertain` 중 하나. 기본 `info` |
| `direction` | `left`, `right`, `ahead`, `none` 중 하나. 기본 `none` |
| `audio_wav_base64` | 선택. 모델 TTS 음성이 있을 때의 PCM WAV base64 |

서버 TTS 음성은 압축되지 않은 16비트 PCM WAV, 모노 또는 스테레오, 8~48kHz, 20초 이하여야 합니다. 서버가 모노 24kHz로 변환해 WebRTC로 보냅니다. WAV 바이너리는 2MB, 전체 JSON 응답은 3MB 제한입니다. MP3·AAC를 이 필드에 그대로 넣으면 실패합니다.

음성이 없으면 UI가 실제 AI의 `text`를 브라우저 TTS로 읽을 수 있습니다. 이 경로는 서버 TTS와 구분해서 표시해야 합니다. 연결 실패, 시간 초과, 잘못된 응답은 시스템 오류로 처리하며 그럴듯한 가짜 인식 결과로 대체하지 않습니다.

## 6. 질문과 영상의 매칭 규칙

1. `question.start`를 서버가 처리할 때 최신 수신 프레임이 충분히 최근인지 확인합니다.
2. 해당 프레임을 질문의 `turn_id`에 고정합니다.
3. 같은 질문 구간의 음성을 모읍니다. 최대 20초입니다.
4. `question.end` 뒤 기본 300ms 동안 늦게 도착한 오디오를 받습니다.
5. 고정한 이미지와 모은 음성을 하나의 AI 요청에 넣습니다.
6. 모드 변경·정지·세션 종료로 무효화된 요청의 결과는 현재 화면과 음성 큐에 반영하지 않습니다.

HTTP 제어 메시지와 WebRTC 미디어는 별도 경로로 도착하므로 300ms 보정은 근사치입니다. 샘플 단위 동기화, 지연 없는 안내, 움직이는 물체의 시간 정확성을 보장하지 않습니다. 실제 휴대폰으로 말하기 시작·종료와 영상 선택을 확인해야 합니다.

자동 주변 안내는 처리 완료 후 설정된 간격을 두고 다음 요청을 보냅니다. `SENSE_GUIDE_INTERVAL=2`는 정확히 매 2초마다 결과가 나온다는 뜻이 아닙니다. AI 처리 시간과 전송 지연이 추가됩니다.

연속 안내는 AI 응답 시점에 해당 영상이 설정된 최신성 기준보다 오래되었으면 결과를 보류합니다. 질문 모드에서 사용자가 선택한 프레임은 별도 스냅샷이므로 이 연속 안내의 응답 폐기 규칙과 구분됩니다. 느린 모델을 연결할 때는 오래된 안내를 그대로 내보내기보다 모델 처리 시간과 프레임 기준을 함께 검토해야 합니다.

## 7. 환경 변수

| 변수 | 기본값 | 의미 |
|---|---|---|
| `SENSE_AI_URL` | 빈 값 | 팀 AI POST 주소. `SENSE_OLLAMA_MODEL`과 함께 설정할 수 없음 |
| `SENSE_AI_KEY` | 빈 값 | 팀 AI 서버용 Bearer 키 |
| `SENSE_OLLAMA_MODEL` | 빈 값 | PC의 Ollama에 설치된 영상 모델 이름 |
| `SENSE_STT_MODEL_PATH` | 빈 값 | Whisper CTranslate2 모델의 로컬 폴더 |
| `SENSE_TTS_VOICE` | 빈 값 | Windows에 설치된 음성 이름 |
| `SENSE_ICE_SERVERS` | `[]` | 브라우저·서버에서 사용할 ICE 서버 목록 |
| `SENSE_AI_TIMEOUT` | `15` | AI 요청 제한 시간(초) |
| `SENSE_GUIDE_INTERVAL` | `2` | 자동 안내 처리 사이의 대기 간격(초) |
| `SENSE_FRAME_MAX_AGE` | `2` | 사용할 수 있는 최신 프레임의 최대 나이(초) |
| `SENSE_SESSION_TTL` | `900` | 제어 연결이 갱신되지 않는 세션의 만료 시간(초) |
| `SENSE_MAX_SESSIONS` | `8` | 최대 세션 수 |
| `SENSE_AUDIO_TAIL_MS` | `300` | 질문 종료 뒤 오디오 추가 수신 대기(ms) |

`.env`는 프로젝트 루트에서 읽습니다. 파일을 바꾸면 서버를 다시 시작합니다. 시스템 환경 변수로 이미 설정된 값이 있으면 그 값을 우선합니다.

## 8. 실제 통합 완료의 기준

최소 한 개의 실제 스마트폰에서 다음을 직접 확인해야 합니다.

- HTTPS·권한 허용·후면 카메라·음성 재생.
- 서버 수신 영상 카운터와 음성 카운터 증가.
- 서버 확인음 재생.
- 실제 팀 AI에 장면과 질문이 전달되고, 답변이 해당 질문으로 돌아옴.
- 연속 3회 질문에서 지난 답변이 현재 답변을 덮어쓰지 않음.
- 정지·연결 종료·권한 거절·네트워크 단절 시 상태가 명확히 안내됨.
- VoiceOver/TalkBack으로 핵심 조작 가능.

5초 응답 목표를 평가한다면 **발화 종료부터 휴대폰에서 실제 소리가 나기 시작하는 시점**을 측정합니다. 서버가 응답한 시각이나 화면에 결과가 표시된 시각과 동일하지 않습니다. 실제 AI 모델 정확도와 이동 안전성은 별도 데이터·시나리오로 평가해야 합니다.

## v0.2: 휴대폰 및 PC 관제

기존 AI 요청/응답 스키마와 FastRTC 연결 방식은 유지합니다. `run.py`는 사용자 앱과 별도의 관리자 앱을 동일 프로세스/이벤트 루프에서 실행합니다. 관리자는 `127.0.0.1:8766`에서만 접속할 수 있습니다. 휴대폰 모드에서는 `POST /api/sessions`에 PC에 표시된 `X-Sense-Pairing` 코드가 필요합니다. 사용자 화면이 자동으로 입력을 요청합니다. 관리 API는 휴대폰 포트에 존재하지 않습니다.

최근 영상과 PCM은 기존 처리 메모리에만 남으며, 관리자 조회 결과는 수신 프레임 JPEG 및 집계 정보로 제한합니다. 로그는 최근 400개 메타데이터 이벤트만 유지하며, 질문/답변 원문과 토큰 및 영상/음성 파일을 저장하지 않습니다. 영상은 운영자가 명시적으로 표시해야 보이며 오래된 프레임은 폐기합니다. AI 처리시간은 서버의 팀 어댑터 호출 기준입니다. 실제 WebRTC/스피커 지연은 별도 측정해야 합니다.

휴대폰 모드의 로컬 QR은 HTTPS 접속 주소와 해당 서버 실행 중 유효한 페어링 값을 담습니다. 브라우저는 HTTP 요청에 포함되지 않는 URL 조각 `#pair=`에서 값을 읽은 뒤 주소 표시줄에서 제거하고 해당 탭의 세션에 보관합니다. `/api/monitor`는 QR 표시 여부와 갱신 식별자를 제공합니다. 서버가 재시작되면 새 QR을 스캔해야 합니다.

관제 `/api/monitor`의 `video_size`는 수신 프레임의 해상도이고 `preview_size`는 메모리에서 축소한 영상의 크기입니다. 휴대폰은 카메라에 30fps를 요청하고 `video_fps`는 서버가 디코딩한 영상의 최근 도착 간격으로 계산합니다. 자동 미리보기는 이미지 갱신이 느린 관제용 표시입니다. 수신량은 디코딩된 영상·음성 프레임과 16 kHz로 변환한 샘플 수입니다.

AI 요청이 들어오면 서버가 최대 3초 동안 프레임 간 움직임과 선명도를 평가합니다. 주변 확인(`scene`)은 한 번 시작하면 바뀐 장면의 프레임만 분석하고, 영상이 들어오지만 흐릴 때는 `scene_waiting_for_video`를 표시하며 다음 안정 프레임을 기다립니다. 영상 연결을 잃으면 주변 확인을 멈춥니다. 물건 찾기와 정보 읽기는 한 번씩 확인합니다.

음성 질문은 화면을 먼저 고른 다음 마이크를 켜며, 선택한 프레임을 그 질문과 함께 사용합니다. 주변 확인 중에 질문하면 답변이 끝난 뒤 주변 확인을 재개합니다. 관제에 표시되는 `last_ai_frame_number`와 `last_ai_frame_received_at`은 마지막 AI 요청에 사용한 영상의 순번과 PC 수신 시각입니다. 프레임과 음성은 메모리에서 처리합니다.

`audio_waveform`은 질문 도중 최근 약 1초의 최대 진폭을 최대 40개 값으로 제공합니다. 질문 종료, 연결 해제, 입력 지연 시 관제에서 파형이 사라집니다. `av_arrival_state`는 수신 대기·질문 음성 대기·입력 지연·수신 도착차 관측을 구분합니다. `av_arrival_gap_ms`는 측정 가능한 경우에만 표시하며 두 미디어 콜백의 서버 도착 시간 차이입니다. 카메라와 마이크의 촬영·녹음 시계 간 동기화는 별도 검증이 필요합니다.

## Windows 로컬 모델

팀 저장소 `JiyeonIm/sense_plus`의 `7fbc4ae` 커밋에서 Ollama 시험 코드에 쓰인 `qwen3-vl:4b-instruct`를 영상 모델로 사용합니다. 팀 데이터셋과 STT·TTS 파일이 준비되기 전 시험용 음성 구성은 다국어 Whisper small과 Windows `Microsoft Heami Desktop`입니다. 팀 저장소의 OpenAI API 경로는 별도입니다.

1. 설치된 Ollama에서 `ollama pull qwen3-vl:4b-instruct`로 모델을 한 번 받습니다. 앱은 `http://127.0.0.1:11434`에서 PC에 설치된 영상 모델만 사용합니다.
2. 프로젝트 폴더에서 `.venv\Scripts\python.exe -m pip install faster-whisper==1.2.1`로 기존 가상환경에 선택 런타임을 설치합니다. [Systran/faster-whisper-small 모델](https://huggingface.co/Systran/faster-whisper-small)의 CTranslate2 파일을 로컬 폴더에 준비합니다. `model.bin`, `config.json`, `tokenizer.json`, `vocabulary.txt`를 함께 둡니다. 음성 인식은 지정된 폴더에서 파일을 읽습니다.
3. 프로젝트 `.env`에서 `SENSE_AI_URL`을 비우고 아래 설정을 입력한 뒤 서버를 다시 실행합니다. `SENSE_TTS_VOICE`에는 Windows에 실제로 설치된 음성 이름을 입력합니다. 모델 파일은 배포 소스와 따로 보관합니다.

```dotenv
SENSE_OLLAMA_MODEL=qwen3-vl:4b-instruct
SENSE_STT_MODEL_PATH=C:/path/to/faster-whisper-small
SENSE_TTS_VOICE=Microsoft Heami Desktop
SENSE_AI_TIMEOUT=55
```

`SpeechToText.transcribe(pcm)`은 16 kHz 모노 `int16` 질문 음성을 받아 공백을 정리한 텍스트를 반환합니다. `TextToSpeech.synthesize(text)`는 답변 텍스트를 받아 24 kHz 모노 `int16` 배열 `(1, n)`을 반환합니다. 팀 모델을 받으면 `senseplus/speech.py`에서 두 규격을 구현해 `create_speech_providers()`에 연결합니다. `LocalAdapter`는 STT 결과와 선택한 JPEG를 VLM에 넣고, 검증된 응답만 TTS에 전달합니다. 일반 주변 확인에서는 STT를 호출하지 않습니다. 서버 TTS 실패 시 휴대폰 음성 합성을 사용하고, 관제 **답변 음성**이 실제 사용한 경로를 표시합니다. 관제와 CSV는 질문 전사문을 기록하지 않습니다.
