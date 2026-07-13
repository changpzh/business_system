from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "APS 生产排程业务系统")
    database_path: Path = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "business.db")))
    algorithm_base_url: str = os.getenv("ALGORITHM_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    algorithm_timeout_seconds: int = int(os.getenv("ALGORITHM_TIMEOUT_SECONDS", "600"))
    session_secret: str = os.getenv("SESSION_SECRET", "change-this-secret-in-production")
    session_hours: int = int(os.getenv("SESSION_HOURS", "12"))
    factory_code: str = os.getenv("FACTORY_CODE", "FACTORY01")
    cors_origins: tuple[str, ...] = tuple(
        item.strip() for item in os.getenv("CORS_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080").split(",") if item.strip()
    )


settings = Settings()
