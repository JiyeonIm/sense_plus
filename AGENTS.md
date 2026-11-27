# AGENTS.md

## Project Overview

SENSE+ is a multimodal AI assistance prototype that provides camera-based scene understanding and voice interaction.

## Architecture

- `api_server.py`: AI Backend (FastAPI)
- `models/depth.py`: Metric depth estimation
- `models/vlm.py`: Visual language model
- `models/stt.py`: Speech-to-text
- `models/tts.py`: Text-to-speech
- `core/`: AI processing utilities
- `apps/sense-plus/`: Phone Web and PC monitoring application
- `apps/sense-plus/senseplus/adapter.py`: AI Backend HTTP integration
- `apps/sense-plus/senseplus/core.py`: Session and analysis lifecycle
- `apps/sense-plus/senseplus/capture.py`: Frame selection and stability checks
- `apps/sense-plus/senseplus/server.py`: Web application lifecycle

## Development Rules

- Maintain separate Python environments for AI Backend and Phone Web.
- Preserve existing API request and response contracts unless changes are coordinated.
- Reuse HTTP clients and release their resources during server shutdown.
- Preserve request timeouts and response size validation.
- Avoid unbounded queues of inference requests.
- Prefer recent frames over outdated analysis results.
- Do not remove frame freshness checks without appropriate validation.
- Do not modify unrelated modules unnecessarily.
- Do not commit API keys, `.env` files, private certificates, model weights or virtual environments.

## Performance Optimization

- Measure latency before and after changes.
- Separate frame capture, HTTP overhead, model inference and speech generation timings.
- Record experimental parameters and results.
- Evaluate recognition quality alongside latency.
- Treat frame stability thresholds as experimental until validated.

## Safety

- Do not claim that the prototype guarantees safe navigation.
- Do not present uncalibrated depth estimates as exact distances.
- Preserve uncertainty and hazard reporting.
- Use supervised, controlled environments for mobility demonstrations.

## Collaboration

- Use feature branches for experimental changes.
- Keep commits focused on a single purpose.
- Coordinate shared integration changes with team members.
- Update README and experimental documentation when behavior changes.