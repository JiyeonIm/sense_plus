# SENSE+

시각장애인과 저시력 사용자가 휴대폰 카메라와 음성으로 주변 정보를 확인하는 개발 프로젝트입니다. 웹앱과 PC 관제는 [`apps/sense-plus`](apps/sense-plus/)에 있고, 기존 팀 모델 실험 코드는 저장소 루트의 `core/`, `models/`, `test*.py`에 있습니다.

## 팀 인수인계

1. Python 3.11 또는 3.12를 준비하고 `apps/sense-plus`에서 Windows는 `setup_windows.bat`, Mac은 `setup_mac.command`를 한 번 실행합니다.
2. **iPhone 연결 및 QR 표시**는 Windows `start_phone_windows.bat`, Mac `start_phone_mac.command`를 실행합니다. 이전 SENSE+가 켜져 있으면 그 터미널에서 `Ctrl+C`로 종료한 뒤 전환합니다. 시연 PC의 Wi-Fi IPv4를 입력하고 브라우저 주소창에 `http://127.0.0.1:8766/`을 입력합니다. [iPhone 인증서 설치와 연결](apps/sense-plus/README_KO.md#iphone-인증서-설치와-연결)
3. **PC 단독 시험**은 `start_windows.bat` 또는 `start_mac.command`를 실행합니다. 사용자 앱 주소는 `http://127.0.0.1:8765/`입니다. 이 모드의 관제 화면에는 QR이 표시되지 않습니다.
4. 기본 설정은 영상·음성 연결 시험입니다. Windows에서 실제 장면 분석에 쓰는 Ollama, 한국어 STT·TTS 설정은 [모델 연동 규격](apps/sense-plus/docs/INTEGRATION.md#windows-로컬-모델)을 따릅니다.

수업에서 시연 PC의 QR로 연결할 iPhone에 필요한 공개 인증서를 받을 수 있습니다. [수업 시연용 `SENSE-test-root.cer` 다운로드](apps/sense-plus/docs/certificates/SENSE-test-root.cer?raw=true) · [한국어 설치 안내](apps/sense-plus/README_KO.md#iphone-인증서-설치와-연결)

## 구현 상태

- **휴대폰** — 색과 텍스트로 구분한 큰 버튼으로 주변 확인, 음성 질문, 종료를 조작합니다. 주변 확인은 한 번 누르면 장면이 바뀔 때마다 이어집니다. 영상이 흐리면 안정될 때까지 기다리고, 영상 수신이 끊기면 멈춥니다.
- **PC 관제** — 영상·음성은 FastRTC/WebRTC로 수신합니다. 카메라 목표는 30fps이며 관제에 실제 수신 FPS, 선택된 분석 프레임, 마이크 파형, 요청·응답 ID, 이벤트 로그와 CSV가 표시됩니다.
- **로컬 모델** — Ollama의 Qwen3-VL, Whisper small 한국어 전사, Windows Heami 음성을 연결했습니다. 팀 데이터셋과 배포할 STT·TTS 모델이 준비되면 `SpeechToText`, `TextToSpeech`, `AIAdapter` 규격으로 교체할 수 있습니다.
- **검증 범위** — Windows에서 Python 테스트 49개, Node 테스트 6개를 통과했습니다. 합성 입력과 실제 로컬 모델로 연속 장면 분석과 음성 왕복을 확인했습니다. 최신 iPhone 실기기 음성 왕복, 장시간 수신, VoiceOver·TalkBack, 보행 안전성은 재검증이 필요합니다. [검증 기록](apps/sense-plus/TEST_STATUS.md)

[한국어 사용 설명서](apps/sense-plus/README_KO.md) · [연동 규격](apps/sense-plus/docs/INTEGRATION.md)


## AI Backend 설치 및 실행 (Windows)

### 1. 개발 환경

- Python 3.11
- NVIDIA GPU 및 CUDA 호환 드라이버 권장
- Git
- OpenAI API Key

실행 확인 환경:

- NVIDIA GeForce RTX 5060 Laptop GPU (8GB)
- NVIDIA GeForce RTX 2080 Ti (11GB)

### 2. 저장소 다운로드

```powershell
git clone -b feature/integration https://github.com/JiyeonIm/sense_plus.git
cd sense_plus
```

### 3. 가상환경 생성

Conda를 사용하는 경우:

```powershell
conda create -n sense_plus python=3.11 -y
conda activate sense_plus
```

또는 Python venv 사용:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 4. 라이브러리 설치

```powershell
python -m pip install --upgrade pip

python -m pip install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128

python -m pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cu128
```

GPU 동작 확인:

```powershell
python -c "import torch; print(torch.cuda.is_available()); print(torch.ones(3, device='cuda'))"
```

### 5. Backend 환경 변수 설정

프로젝트 최상위 `.env.example`을 `.env`로 복사합니다.

```powershell
Copy-Item .env.example .env
```

`.env` 파일에 OpenAI API Key를 입력합니다.

```dotenv
OPENAI_API_KEY=YOUR_API_KEY

SENSE_VLM_MODEL=gpt-6-luna
SENSE_VLM_DETAIL=low
SENSE_VLM_MAX_TOKENS=120
SENSE_VLM_REASONING=none
SENSE_SERVER_TTS=1
```

API Key 및 실제 `.env` 파일은 Git에 업로드하지 않습니다.

### 6. AI Backend 실행

프로젝트 최상위 디렉터리에서 실행합니다.

```powershell
python -m uvicorn api_server:app --host 127.0.0.1 --port 8000
```

최초 실행 시 Depth Anything V2 모델 가중치가 자동 다운로드됩니다.

정상 실행 확인:

```text
[Depth] Device: cuda
INFO: Application startup complete.
[API] OpenAI 연결 준비 완료
```

Health Check:

http://127.0.0.1:8000/health

### 7. Phone Web 설치 및 실행

새 터미널을 열고 아래 명령어를 실행합니다.

```powershell
cd apps/sense-plus
.\setup_windows.bat
```

Phone Web은 AI Backend와 별도의 가상환경을 사용합니다.

`apps/sense-plus/.env.example`을 `.env`로 복사한 뒤 다음 설정을 적용합니다.

```dotenv
SENSE_AI_URL=http://127.0.0.1:8000/analyze
SENSE_OLLAMA_MODEL=
SENSE_AI_TIMEOUT=15
SENSE_GUIDE_INTERVAL=2
SENSE_FRAME_MAX_AGE=2
```

Phone Web 실행:

```powershell
.\start_phone_windows.bat
```

PC 관제 화면:

http://127.0.0.1:8766

### 8. 스마트폰 연결

- PC와 스마트폰은 서로 통신 가능한 로컬 네트워크에 연결합니다.
- PC가 유선 LAN을 사용하고 스마트폰이 Wi-Fi를 사용해도 연결할 수 있습니다.
- PC의 IPv4 주소를 확인하고 Phone Web 실행 시 입력합니다.
- iPhone 인증서 설치 및 신뢰 설정은 기존 한국어 사용 설명서를 참고합니다.
- 관제 화면에서 생성된 QR 코드로 접속합니다.

### 9. 문제 해결

| 증상 | 확인 사항 |
|---|---|
| Conda activate 실패 | PowerShell 초기화 및 conda init 확인 |
| CUDA 인식 실패 | NVIDIA Driver 및 CUDA 지원 PyTorch 확인 |
| Hugging Face symlink 경고 | 모델 실행이 정상이라면 무시 가능 |
| 스마트폰 접속 실패 | IP 주소, 서브넷, 공유기 및 방화벽 확인 |
| 카메라 영상이 오래되었다는 오류 | 영상 수신 상태와 프레임 최신성 확인 |
| OpenAI API 호출 실패 | API Key, 모델 접근 권한 및 사용 한도 확인 |

### 10. 사용 시 주의사항

본 프로젝트는 시각 보조 AI 서비스의 프로토타입입니다. 거리 추정 및 장면 인식 결과에는 오차가 발생할 수 있으며, 실제 보행 안전을 보장하지 않습니다. 모든 이동 시연은 보조자가 동행하는 통제된 환경에서 진행합니다.