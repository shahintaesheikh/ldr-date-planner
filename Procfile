# Railway start command — runs migrations then boots the API.
web: alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
