# Church Auto Check-in System

An intelligent church attendance platform powered by face recognition, with real-time check-in, face library management, reception workflows, and admin operations.

Language:
- English (default)
- Chinese: [README.zh-CN.md](./README.zh-CN.md)

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Quick Start](#quick-start)
- [Progress](#progress)
- [Roadmap](#roadmap)
- [Project Structure](#project-structure)
- [API Overview](#api-overview)
- [Operations](#operations)
- [Testing](#testing)
- [License](#license)

---

## Overview

Church Auto Check-in System is designed for church attendance and care workflows.
It connects live camera streams with CompreFace to identify attendees, records attendance events, and provides dedicated interfaces for reception, debugging, and admin teams.

Core capabilities:
- Real-time check-in with multi-frame voting and dedupe controls
- End-to-end face library management (local + CompreFace sync)
- Reception queue for unknown faces and manual resolution
- Care-center analytics (risk tiers, cohorts, profile views, CSV export)
- Event and attendance management with history queries and export
- Real-time WebSocket channels for events and debug video frames

## Key Features

### 1. Real-time Recognition Pipeline
- RTSP and camera index input support
- Multi-frame voting for better recognition stability
- Threshold + margin protection against false positives
- Per-service dedupe to avoid repeated check-ins
- Sunday service behavior controls and session split logic
- Inactive or missing members are filtered before check-in and pending decisions

### 2. Face Library Management
- Member photo upload, replace, delete, and active-version handling
- Local storage plus remote CompreFace synchronization
- Thumbnail paths for faster list rendering
- Rebuild/sync workflows for operational recovery

### 3. Reception Workbench
- Live attendance and unknown-case queue
- Candidate search and manual resolution flows
- Keyword search across name, Chinese name, pinyin, initials, and gender filters
- Photo-picker mode with signed-in / not-signed-in filtering; signed-in members are ordered by the selected event's check-in time, newest first
- Attendance records distinguish recognition, manual search, pending queue, and historical backfill sources

### 4. Admin Workbench
- Member CRUD and profile fields (group, birthday, notes)
- Attendance event creation, update, archive
- Attendance history filtering and CSV export
- Weekly care brief generation/send management
- New-member form supports gender, controlled groups, Latin-name validation, and automatic check-in for the current Sunday service

### 5. Care Center
- Risk-based member list with multi-condition filters
- Individual profile panels (recent check-ins, Sunday absence streak, trends)
- Cohort recommendations and aggregate care report views
- Care list CSV export with Chinese business labels

### 6. System Controls and Diagnostics
- Stream start/stop and runtime restart
- Hyperparameter hot updates for recognition tuning
- Health/self-check endpoints and runtime status payload
- Debug video and overlay toggles
- Chinese/English language switching across the web workbenches
- Process-level metrics (CPU, memory, GPU, load)

## Architecture

```text
Web UI (index / reception / debug / admin)
            |
            v
FastAPI Backend (REST + WebSocket)
            |
            v
Core Services (RuntimePipeline, RecognitionEngine, CameraService,
              EventDispatcher, FaceLibraryService, Cleanup services)
            |
            v
SQLite (members, attendance, events, queue, logs)
            |
            v
CompreFace REST API
```

## Tech Stack

- FastAPI 0.115.12
- Uvicorn 0.34.0
- SQLAlchemy 2.0.39
- SQLite
- OpenCV 4.11.0
- Pillow 11.1.0
- Requests 2.32.3
- Pytest 8.3.5
- CompreFace REST API

## Quick Start

### Prerequisites

- Python 3.9+
- CompreFace service
- Camera device or RTSP source

### Install

1. Clone repository
```bash
git clone https://github.com/ffffuturexu/church-auto-checkin-system.git
cd church-auto-checkin-system
```

2. Create venv
```bash
python -m venv venv
source venv/bin/activate
```

3. Install dependencies
```bash
pip install -r requirements.txt
```

4. Configure `config.ini`
```ini
[CompreFace]
ApiKey = your-api-key
BaseUrl = http://compreface-host:8000

[Script]
Threshold = 0.70
Margin = 0.20
DedupeSeconds = 60
FrameSkip = 2
VoteWindowSec = 1.5
VoteMinSamples = 5
VoteRatio = 0.65
UnknownMinSimilarity = 0.65
UnknownMinFaceSize = 64
MaxQueueSize = 3
PredictionCount = 5

[DataSource]
RTSP_URL = rtsp://camera-url
CameraIndex = 0
RtspTcp = true
```

5. Initialize database
```bash
python -c "from app.core.database import init_db; init_db()"
```

6. Start server
```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

7. Open app
- Home: http://localhost:8001/
- Reception: http://localhost:8001/reception
- Debug: http://localhost:8001/debug
- Admin: http://localhost:8001/admin

## Progress

- MVP business flow is complete across check-in, face library, reception, admin, and care center.
- WebSocket dual-channel event/video streaming is stable in production workflow.
- Mobile-focused UI refinements have been applied across primary pages.
- Historical attendance migration, reporting, and thumbnail flow are in place.
- Reception photo-picker signed-in results now prioritize the most recent check-ins for the selected event; the ordering regression test passes in the `autocheckin` conda environment.
- Attendance history now exposes a durable `source` field with automatic SQLite migration and source-based dashboard counts.
- Recognition candidate filtering, new-member admin flow, and Chinese/English UI localization are implemented.

### Latest Release

- **v0.6.2 (2026-11-20)**: Add a schedule-review demo for ministry coordinators, including weekly schedule filtering, staffing-gap visibility, request review actions, and a linked entry point from the home page; refine debug workspace guidance and localization coverage.
- **v0.6.1 (2026-09-05)**: Add attendance source classification and SQLite migration, filter inactive recognition candidates, improve new-member registration with automatic Sunday check-in, and add Chinese/English UI localization.
- **v0.5.5 (2026-08-23)**: Sort signed-in photo-picker results by selected-event check-in time descending and add regression coverage.

## Roadmap

### Engineering
- [ ] CI/CD automation (unit + integration test pipelines)
- [ ] Containerized deployment (Docker + Compose)
- [x] Health-check endpoints and runtime diagnostics baseline
- [ ] Alerting strategy completion (on-call notifications and rules)

### Feature Expansion
- [ ] Multi-camera support and load balancing
- [x] Group/time-based statistics dashboards (admin + care views)
- [ ] Group/time-based alert rules
- [ ] Recognition model tuning and A/B testing
- [x] Basic performance monitoring dashboard

### Security
- [ ] Full role-based auth (Reception / Admin / Ops)
- [ ] Data access controls and audit hardening
- [ ] Secondary confirmation for sensitive actions

### User Experience
- [x] English / Chinese UI localization baseline
- [x] Mobile responsive design
- [ ] Dark theme support
- [ ] Bulk member import workflow

## Project Structure

```text
church-auto-checkin-system/
├── app/
├── scripts/
├── tests/
├── data/
├── records/
├── config.ini
├── requirements.txt
├── docs/
├── README.md
├── README.zh-CN.md
└── LICENSE
```

## API Overview

- Members: `/members`, `/members/search`, `/members/photo-picker`
- Attendance: `/attendance/current-service`, `/attendance/manual-checkin`, `/attendance/history`
- Attendance history and CSV responses expose `source`: `recognition`, `manual_search`, `pending_queue`, or `backfill`; existing SQLite databases receive the field automatically at startup.
- Events: `/events`
- Face library: `/face-library/...`
- Care: `/care/members`, `/care/report`, `/care/cohorts`
- Reception queue: `/reception/queue/unknown`
- System: `/health`, `/system/self-check`, `/system/status`, `/system/stream/start`
- WebSocket: `/ws/channel-a`, `/ws/channel-b`

See OpenAPI docs at `/docs` and `/redoc`.

Recognition events also carry the attendance source, while inactive or missing members are excluded before success or pending decisions.

## Operations

- Service/unit files and maintenance scripts are in `scripts/systemd/`.
- Birthday reminder and weekly care brief jobs are included.
- Common maintenance procedures: [docs/MAINTENANCE.md](./docs/MAINTENANCE.md)

## Testing

```bash
pytest tests/ -v
pytest tests/test_api_minimal.py -v
pytest tests/ --cov=app --cov-report=html
```

The v0.6.1 regression coverage includes attendance-source API assertions and protection against inactive members entering recognition success or pending flows. Run tests in the `autocheckin` conda environment.

## License

MIT License. See [LICENSE](./LICENSE).
