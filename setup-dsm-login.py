from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright


async def main() -> None:
    load_dotenv()
    base_url = os.environ["SYNOLOGY_BASE_URL"]
    profile = os.getenv("BROWSER_PROFILE_PATH", "./data/dsm-browser-profile")
    browser_channel = os.getenv("BROWSER_CHANNEL", "chrome").strip()
    Path(profile).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        launch_options = {
            "headless": False,
            "ignore_https_errors": True,
            "viewport": {"width": 1440, "height": 960},
        }
        if browser_channel:
            launch_options["channel"] = browser_channel
        context = await playwright.chromium.launch_persistent_context(profile, **launch_options)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(base_url, wait_until="domcontentloaded", timeout=60_000)
        print("請在開啟的 Chrome 中手動登入 DSM。")
        print("看到 DSM 桌面與 File Station 後，回到此 PowerShell 按 Enter。")
        await asyncio.to_thread(input)
        await page.wait_for_timeout(2_000)
        if await page.get_by_text("File Station", exact=True).count():
            print("DSM 登入狀態已保存。")
        else:
            print("警告：程式尚未看到 File Station；請確認真的已進入 DSM 桌面。")
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
