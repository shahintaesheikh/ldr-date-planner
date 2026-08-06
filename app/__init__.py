from .config import Settings
from .database import DatabaseManager

settings = Settings()
db = DatabaseManager(settings.database_url)

__all__ = ["settings", "db"]