import re

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


# Match any postgres scheme without an async driver suffix.
# E.g. "postgresql://..." or "postgres://..." → "postgresql+asyncpg://..."
_POSTGRES_PLAIN_SCHEME = re.compile(r"^postgres(?:ql)?://")


def _normalize_url(url: str) -> str:
    """Ensure the URL uses the asyncpg driver.

    Railway and many hosts provide ``DATABASE_URL`` as
    ``postgresql://user:pass@host/db`` (no driver suffix), which
    ``create_async_engine`` rejects.  This transforms it to
    ``postgresql+asyncpg://...`` if needed.
    """
    if _POSTGRES_PLAIN_SCHEME.match(url):
        return "postgresql+asyncpg://" + url.split("://", 1)[1]
    return url


class DatabaseManager:
    """Manages async SQLAlchemy engine and session factory."""

    def __init__(self, database_url: str) -> None:
        self.engine = create_async_engine(
            _normalize_url(database_url), pool_pre_ping=True
        )
        self._session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def close(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self._session_factory()