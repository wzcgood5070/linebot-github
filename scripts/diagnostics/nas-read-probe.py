"""Read and verify one approved NAS file from an isolated test container."""

import asyncio
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from app.nas import SynologyBrowserNas


FOLDER = "TEST_MEETING_FOLDER"
FILENAME = "test.pdf"
SOURCE_PROFILE = Path("/app/data/dsm-browser-profile")


async def verify_pdf():
    if not SOURCE_PROFILE.is_dir():
        raise SystemExit("NAS TEST: PROFILE_NOT_FOUND")
    with TemporaryDirectory() as scratch:
        profile = Path(scratch) / "dsm-profile"
        shutil.copytree(
            SOURCE_PROFILE, profile,
            ignore=shutil.ignore_patterns("Singleton*", "DevToolsActivePort", "Cache", "Code Cache", "GPUCache"),
        )
        os.environ["BROWSER_PROFILE_PATH"] = str(profile)
        os.environ["SYNOLOGY_PARENT_PATH"] = "/YOUR_SHARED_FOLDER/linebot"
        os.environ["SYNOLOGY_SUBMISSION_MOUNT"] = ""
        os.environ["SYNOLOGY_FILE_STATION_BROWSER_ONLY"] = "true"
        os.environ["BROWSER_HEADLESS"] = "true"
        os.environ["BROWSER_CHANNEL"] = ""
        nas = SynologyBrowserNas()

        print("NAS TEST: OPENING_AND_DOWNLOADING", flush=True)
        async with nas.open_submissions(FOLDER, download=True, names={FILENAME}) as paths:
            if len(paths) != 1:
                raise SystemExit("NAS TEST: FILE_NOT_DOWNLOADED")
            with paths[0].open("rb") as pdf:
                if b"%PDF-" not in pdf.read(1024):
                    raise SystemExit("NAS TEST: NOT_A_PDF")
            print(f"NAS TEST: PASS ({paths[0].stat().st_size} bytes)")


if __name__ == "__main__":
    try:
        asyncio.run(verify_pdf())
    except Exception as error:
        message = str(error)
        detail = message if message.startswith("DSM File Station ") else type(error).__name__
        raise SystemExit(f"NAS TEST: FAILED ({detail})") from None
