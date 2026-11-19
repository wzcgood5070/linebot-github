from __future__ import annotations

import asyncio
from datetime import datetime

from dotenv import load_dotenv

from app.config import Settings
from app.nas import build_nas


async def main() -> None:
    load_dotenv()
    settings = Settings()
    if settings.nas_driver != "synology_browser":
        raise SystemExit("錯誤：.env 的 NAS_DRIVER 必須設為 synology_browser")

    starts_at = datetime.now(settings.timezone)
    title = "NAS自動化測試"
    url = await build_nas(settings.nas_driver).create_upload_folder(title, starts_at)
    print("NAS 測試成功")
    print(f"檔案請求網址：{url}")


if __name__ == "__main__":
    asyncio.run(main())
