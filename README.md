# LDR Date Planner

Constraint-satisfaction + ideation agent for long-distance date planning. Two calendars in, one specific proposal out, delivered via SMS.

## Architecture

```
Web App (React) → FastAPI Core → Postgres
                    ↓
              LangGraph Agent
                    ↓
         Calendar Adapters / Twilio (SMS)
```

## Phase 0 — Repo scaffold

- PostgreSQL schema (8 tables: users, couples, calendar_connections, traits, date_activities, proposals, feedback, sms_thread)
- FastAPI skeleton with health check endpoint
- Alembic migrations for schema management

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env

# Run migrations
alembic upgrade head

# Start the server
uvicorn app.main:app --reload
```

## Health check

```
GET /health
→ {"status": "ok", "database": "connected", "version": "0.1.0"}
```