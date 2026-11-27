"""Server configuration. Provider credentials never leave this process."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Config:
    ai_url: str = ""
    ai_key: str = ""
    ai_timeout: float = 15.0
    ollama_model: str = ""
    stt_model_path: str = ""
    tts_voice: str = ""
    guide_interval: float = 2.0
    frame_max_age: float = 2.0
    session_ttl: float = 900.0
    max_sessions: int = 8
    audio_tail_ms: int = 300
    ice_servers: list[dict] = field(default_factory=list)
    frontend_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "frontend")

    @property
    def ai_configured(self) -> bool:
        return bool(self.ai_url or self.ollama_model)

    @classmethod
    def from_env(cls) -> "Config":
        # Optional .env support; importing this module does not require dotenv.
        try:
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
        except ImportError:
            pass
        url = os.getenv("SENSE_AI_URL", "").strip()
        if url and urlparse(url).scheme not in {"http", "https"}:
            raise ValueError("SENSE_AI_URL은 http:// 또는 https:// 주소여야 합니다.")
        ollama_model = os.getenv("SENSE_OLLAMA_MODEL", "").strip()
        if url and ollama_model:
            raise ValueError("SENSE_AI_URL과 SENSE_OLLAMA_MODEL 중 하나만 설정해 주세요.")
        ice = json.loads(os.getenv("SENSE_ICE_SERVERS", "[]"))
        if not isinstance(ice, list) or any(not isinstance(item, dict) or "urls" not in item for item in ice):
            raise ValueError("SENSE_ICE_SERVERS는 urls가 있는 객체의 JSON 배열이어야 합니다.")
        return cls(
            ai_url=url,
            ai_key=os.getenv("SENSE_AI_KEY", ""),
            ai_timeout=max(1, min(60, float(os.getenv("SENSE_AI_TIMEOUT", "15")))),
            ollama_model=ollama_model,
            stt_model_path=os.getenv("SENSE_STT_MODEL_PATH", "").strip(),
            tts_voice=os.getenv("SENSE_TTS_VOICE", "").strip(),
            guide_interval=max(1, min(30, float(os.getenv("SENSE_GUIDE_INTERVAL", "2")))),
            frame_max_age=max(.25, min(5, float(os.getenv("SENSE_FRAME_MAX_AGE", "2")))),
            session_ttl=max(30, min(3600, float(os.getenv("SENSE_SESSION_TTL", "900")))),
            max_sessions=max(1, min(32, int(os.getenv("SENSE_MAX_SESSIONS", "8")))),
            audio_tail_ms=max(0, min(1000, int(os.getenv("SENSE_AUDIO_TAIL_MS", "300")))),
            ice_servers=ice,
        )
