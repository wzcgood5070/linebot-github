from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Settings:
    line_channel_secret: str = field(default_factory=lambda: os.getenv("LINE_CHANNEL_SECRET", "dev-secret"))
    line_channel_access_token: str = field(default_factory=lambda: os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""))
    timezone_name: str = field(default_factory=lambda: os.getenv("APP_TIMEZONE", "Asia/Taipei"))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "./data/meetings.db"))
    poll_seconds: int = field(default_factory=lambda: int(os.getenv("POLL_SECONDS", "10")))
    upload_lead_minutes: int = field(default_factory=lambda: int(os.getenv("UPLOAD_LEAD_MINUTES", "10")))
    nas_driver: str = field(default_factory=lambda: os.getenv("NAS_DRIVER", "local"))
    orchestrator: str = field(default_factory=lambda: os.getenv("ORCHESTRATOR", "python"))
    internal_meeting_jobs: bool = field(default_factory=lambda: os.getenv("INTERNAL_MEETING_JOBS", "").lower() == "true")
    submission_checks_enabled: bool = field(default_factory=lambda: os.getenv("MEETING_SUBMISSION_CHECKS_ENABLED", "true").lower() == "true")
    n8n_intake_url: str = field(
        default_factory=lambda: os.getenv("N8N_INTAKE_URL", "http://n8n:5678/webhook/line-meeting-intake-v3")
    )
    core_api_key: str = field(default_factory=lambda: os.getenv("CORE_API_KEY", "dev-core-key"))

    @property
    def timezone(self) -> tzinfo:
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            if self.timezone_name == "Asia/Taipei":
                return timezone(timedelta(hours=8), name="Asia/Taipei")
            raise
