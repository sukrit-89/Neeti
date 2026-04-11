# CODEBASE KNOWLEDGE MAP

Last updated: 2026-04-07

This document is a practical map of the Integrity-AI codebase to help you build full-system understanding quickly.

## 1) System Topology

Frontend (React + TypeScript) talks to FastAPI over REST and WebSocket.
FastAPI uses PostgreSQL for persistence and Redis for rate limiting, pub/sub, and Celery broker/results.
Background evaluation runs through Celery workers and specialized AI agents.
External integrations include Supabase Auth, LiveKit, Judge0, OpenAI/Ollama, and optional Whisper/MediaPipe paths.

## 2) Main Runtime Entry Points

- Backend app bootstrap: app/main.py
- Global config and env resolution: app/core/config.py
- DB URL/session/engine setup: app/core/database.py
- Auth and role guards: app/core/auth.py
- Event bus contracts and publishing: app/core/events.py
- Redis connectivity and cache/pubsub helpers: app/core/redis.py

## 3) Backend API Surface

### Authentication

- app/api/supabase_auth.py
- Register, login, refresh, current user profile.
- Supabase is the identity source of truth.

### Sessions

- app/api/sessions.py
- Create/list/update/start/end sessions.
- Join flow uses 6-character code.
- LiveKit room creation/token generation integrated here.

### Coding and Execution

- app/api/coding_events.py
- Stores coding events, executes code through Judge0.
- Guards include language allowlist, max code size, and execution caps.

### Evaluations

- app/api/evaluations.py
- Read evaluations and trigger asynchronous evaluation pipeline.

### Speech

- app/api/speech.py
- Transcription and analysis endpoints.

### WebSocket

- app/api/websocket.py
- Session-scoped realtime channel with token validation and membership checks.

## 4) Data Model Core

- app/models/models.py

Important entities:

- Session
- Candidate
- CodingEvent
- SpeechSegment
- VisionMetric
- AgentOutput
- Evaluation

Notes:

- Session is the central aggregate root.
- Evaluation is the final synthesized output.
- User table exists for backward compatibility; Supabase auth is primary.

## 5) Schema Contracts

- app/schemas/schemas.py

Defines request/response contracts for auth, sessions, coding events, and evaluation payloads.

## 6) Services Layer Responsibilities

- app/services/livekit_service.py: room create + token generation
- app/services/judge0_service.py: code execution and polling
- app/services/ai_service.py: OpenAI/Ollama dispatch and fallback
- app/services/speech_service.py: transcription path
- app/services/vision_service.py: frame analysis path
- app/services/realtime_service.py: realtime abstraction
- app/services/supabase_service.py: Supabase wrappers

## 7) Agent Pipeline

- app/agents/base.py: shared input/output contracts
- app/agents/coding_agent.py
- app/agents/speech_agent.py
- app/agents/vision_agent.py
- app/agents/reasoning_agent.py
- app/agents/evaluation_agent.py

Pipeline shape:

1. Collect modality data
2. Produce per-agent score/findings/flags
3. Aggregate to final recommendation in evaluation agent

## 8) Workers and Task Orchestration

- app/workers/celery_app.py
- app/workers/agent_tasks.py
- app/workers/session_tasks.py

Key behavior:

- trigger_all_agents enqueues per-agent tasks
- agent outputs are persisted
- final evaluation is generated asynchronously

## 9) Frontend Architecture

- frontend/src/main.tsx: app bootstrap
- frontend/src/App.tsx: route graph and guards
- frontend/src/store/useAuthStore.ts: auth state lifecycle
- frontend/src/store/useSessionStore.ts: session lifecycle and room token context
- frontend/src/lib/api.ts: Axios client, auth headers, token refresh
- frontend/src/lib/websocket.ts: realtime connection/reconnect logic

Critical pages:

- frontend/src/pages/Dashboard.tsx
- frontend/src/pages/SessionCreate.tsx
- frontend/src/pages/SessionJoin.tsx
- frontend/src/pages/InterviewRoom.tsx
- frontend/src/pages/EvaluationReport.tsx

## 10) Infrastructure and Deployment

- docker-compose.yml: local stack topology
- Dockerfile: API container
- Dockerfile.worker: Celery worker container
- Procfile: process entry for Procfile-style deploys
- render.yaml and railway.toml: platform-specific deployment wiring

## 11) Testing Coverage Snapshot

- tests/test_auth.py: auth behavior
- tests/test_sessions.py: session lifecycle
- tests/test_integration.py and tests/test_system.py: broader integration checks
- tests/conftest.py: shared fixtures and async client/session setup

Known coverage gaps usually include deep Celery orchestration, WebSocket behavior, and full failure-mode simulation.

## 12) End-to-End Learning Order (Fastest Path)

1. README.md
2. dev-docs/ARCHITECTURE.md
3. app/main.py
4. app/core/config.py and app/core/database.py
5. app/models/models.py and app/schemas/schemas.py
6. app/api/sessions.py and app/api/evaluations.py
7. app/services/livekit_service.py and app/services/judge0_service.py
8. app/agents/* and app/workers/*
9. frontend/src/App.tsx, frontend/src/lib/api.ts, frontend/src/store/*
10. frontend/src/pages/InterviewRoom.tsx
11. tests/*
12. docker-compose.yml

## 13) Current Risk/Attention Areas

- Supabase dependency is hard critical for protected routes.
- Redis is multi-purpose (rate limiting, pub/sub, Celery), so it is an operational bottleneck.
- Agent orchestration and retries deserve focused hardening if reliability is priority.
- Very large sessions may pressure event-query and processing costs without stricter pagination/retention.

## 14) What This Map Is For

Use this file as a north-star index, then open each file in the reading order above.
For line-by-line mastery, do one module family at a time:

- core -> api -> services -> agents/workers -> frontend -> tests -> infra.
