.PHONY: install dev migrate check test

install:
	pip install -r requirements.txt

dev:
	uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

migrate:
	alembic upgrade head

migrate-downgrade:
	alembic downgrade -1

migrate-history:
	alembic history

migrate-autogenerate:
	alembic revision --autogenerate -m "$(message)"

check:
	python3 -c "from app.main import app; print('FastAPI app OK')"
	python3 -c "from app.models import Base; print('Models OK')"

test:
	python3 -m pytest tests/ -v