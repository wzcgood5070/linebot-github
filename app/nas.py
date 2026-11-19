from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, quote, unquote_plus, urljoin, urlsplit


def files_in_folder(root: Path, folder_name: str) -> list[Path]:
    folder = root / folder_name
    if folder.is_symlink():
        raise OSError("會議資料夾不可為符號連結")
    return sorted(path for path in folder.iterdir() if path.is_file() and not path.is_symlink())


def local_file_versions(paths: list[Path]) -> dict[str, str]:
    versions = {}
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        versions[path.name] = f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"
    return versions


def safe_name(title: str, starts_at: datetime) -> str:
    # User-selected convention: one shared meeting folder per calendar date.
    return starts_at.strftime("%m%d")


class LocalNas:
    async def create_upload_folder(self, title: str, starts_at: datetime, *, folder_name: str | None = None) -> str:
        name = folder_name or safe_name(title, starts_at)
        root = Path(os.getenv("LOCAL_NAS_ROOT", "./data/nas"))
        (root / name).mkdir(parents=True, exist_ok=True)
        return f"{os.getenv('LOCAL_PUBLIC_BASE_URL', 'http://localhost:8000/uploads').rstrip('/')}/{quote(name)}"

    def submission_files(self, folder_name: str) -> list[Path]:
        root = Path(os.getenv("LOCAL_NAS_ROOT", "./data/nas"))
        return files_in_folder(root, folder_name)

    @asynccontextmanager
    async def open_submissions(self, folder_name: str, *, download: bool = False, names: set[str] | None = None):
        yield [path for path in self.submission_files(folder_name) if names is None or path.name in names]

    async def submission_versions(self, folder_name: str) -> dict[str, str]:
        return local_file_versions(self.submission_files(folder_name))


class SynologyBrowserNas:
    """Drive DSM through a dedicated Chrome profile when only QuickConnect is available."""

    def __init__(self):
        self.base = os.environ["SYNOLOGY_BASE_URL"].rstrip("/")
        self.user = os.environ["SYNOLOGY_USERNAME"]
        self.password = os.environ["SYNOLOGY_PASSWORD"]
        self.parent = os.getenv("SYNOLOGY_PARENT_PATH", "/line-meetings").strip("/")
        self.profile = os.getenv("BROWSER_PROFILE_PATH", "./data/dsm-browser-profile")
        self.browser_lock = asyncio.Lock()
        self.headless = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
        self.browser_channel = os.getenv("BROWSER_CHANNEL", "chrome").strip()
        self.public_base = os.getenv("SYNOLOGY_PUBLIC_BASE_URL", "https://YOUR_QUICKCONNECT_ID.quickconnect.to").rstrip("/")

    def _find_file_request_url(self, value) -> str:
        if isinstance(value, str):
            if "/sharing/" in value:
                return value
            return ""
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"sharing_link", "sharing_link_id", "link_id"} and isinstance(item, str):
                    token = item.rsplit("/", 1)[-1]
                    if re.fullmatch(r"[A-Za-z0-9_-]{6,}", token):
                        return f"{self.public_base}/sharing/{token}"
                found = self._find_file_request_url(item)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._find_file_request_url(item)
                if found:
                    return found
        return ""

    def submission_files(self, folder_name: str) -> list[Path]:
        mounted_root = os.getenv("SYNOLOGY_SUBMISSION_MOUNT", "").strip()
        if not mounted_root:
            raise OSError("尚未設定可讀取的 NAS 資料夾掛載路徑")
        return files_in_folder(Path(mounted_root), folder_name)

    @asynccontextmanager
    async def open_submissions(self, folder_name: str, *, download: bool = False,
                               names: set[str] | None = None, versions: bool = False):
        if os.getenv("SYNOLOGY_SUBMISSION_MOUNT", "").strip():
            paths = self.submission_files(folder_name)
            yield local_file_versions(paths) if versions else [path for path in paths if names is None or path.name in names]
            return

        from playwright.async_api import async_playwright

        async with self.browser_lock, async_playwright() as playwright:
            options = {"headless": self.headless, "viewport": {"width": 1440, "height": 960}}
            if self.browser_channel:
                options["channel"] = self.browser_channel
            context = await playwright.chromium.launch_persistent_context(self.profile, **options)
            api_session = None
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await self._ensure_login(page)
                authorization = {}

                def capture_file_station_request(request):
                    parts = urlsplit(request.url)
                    if "/webapi/" not in parts.path:
                        return
                    params = {**parse_qs(parts.query), **parse_qs(request.post_data or "")}
                    if not params.get("api", [""])[0].startswith("SYNO.FileStation."):
                        return
                    token = params.get("SynoToken", [None])[0] or request.headers.get("x-syno-token")
                    if token:
                        authorization.update({"SynoToken": token})

                page.on("request", capture_file_station_request)
                existing_pages = set(context.pages)
                shortcut = page.locator(".sds-desktop-shortcut li.launch-icon").filter(has_text="File Station").first
                window = page.locator(".syno-sds-fs-win:visible").last
                if not await window.count() and not await self._restore_file_station(page):
                    await shortcut.click(timeout=15_000)
                try:
                    await window.wait_for(state="visible", timeout=60_000)
                except Exception:
                    await self._restore_file_station(page)
                page = next((candidate for candidate in context.pages if candidate not in existing_pages), page)
                folder = f"/{self.parent}/{folder_name}"
                if os.getenv("SYNOLOGY_FILE_STATION_BROWSER_ONLY", "").lower() == "true":
                    async with self._browser_submissions(page, folder, names=names, download=download, versions=versions) as result:
                        yield result
                    return
                api_root = urljoin(page.url, "/webapi/")
                info = await context.request.get(urljoin(api_root, "query.cgi"), params={
                    "api": "SYNO.API.Info", "version": 1, "method": "query",
                    "query": "SYNO.API.Auth,SYNO.FileStation.List,SYNO.FileStation.Download",
                })
                definitions = self._api_data(await info.json())
                list_api = "SYNO.FileStation.List"
                list_endpoint = urljoin(api_root, definitions[list_api]["path"])
                list_arguments = {
                    "api": list_api, "version": 2, "method": "list", "folder_path": folder,
                    "additional": "size,time",
                }
                cookies = await context.cookies([page.url])
                sid = next((cookie["value"] for cookie in cookies if cookie["name"] == "id"), None)
                if sid:
                    authorization["_sid"] = sid
                response = await context.request.post(list_endpoint, form={**list_arguments, **authorization}, max_redirects=0)
                listing = await response.json()
                if listing.get("error", {}).get("code") in {106, 119}:
                    auth = definitions["SYNO.API.Auth"]
                    auth_endpoint = urljoin(api_root, auth["path"])
                    auth_arguments = {"api": "SYNO.API.Auth", "version": min(6, auth["maxVersion"]), "session": "FileStation"}
                    login = await context.request.post(auth_endpoint, form={
                        **auth_arguments, "method": "login", "account": self.user, "passwd": self.password,
                        "format": "sid", "enable_syno_token": "yes",
                    }, max_redirects=0)
                    account = await login.json()
                    if account.get("success"):
                        account = self._api_data(account)
                        authorization = {"_sid": account["sid"]}
                        if account.get("synotoken"):
                            authorization["SynoToken"] = account["synotoken"]
                        api_session = (auth_endpoint, {**auth_arguments, **authorization, "method": "logout"})
                        response = await context.request.post(list_endpoint, form={**list_arguments, **authorization}, max_redirects=0)
                        listing = await response.json()
                if listing.get("error", {}).get("code") in {106, 119}:
                    async with self._browser_submissions(page, folder, names=names, download=download, versions=versions) as result:
                        yield result
                    return
                entries = self._api_data(listing)["files"]
                files = self._direct_files(entries, folder)
                if versions:
                    yield {entry['name']: self._file_version(entry) for entry in files}
                    return
                if names is not None:
                    files = [entry for entry in files if entry['name'] in names]
                if not download:
                    yield [Path(entry['name']) for entry in files]
                    return

                download_api = "SYNO.FileStation.Download"
                with TemporaryDirectory() as directory:
                    paths = []
                    for entry in files:
                        response = await context.request.post(urljoin(api_root, definitions[download_api]["path"]), form={
                            "api": download_api, "version": 2, "method": "download",
                            "mode": "download", "path": json.dumps([entry['path']], ensure_ascii=False),
                            **authorization,
                        }, max_redirects=0)
                        if not response.ok or "attachment" not in response.headers.get("content-disposition", "").lower():
                            raise OSError("DSM 無法下載會議檔案")
                        path = Path(directory) / entry['name']
                        path.write_bytes(await response.body())
                        paths.append(path)
                    yield paths
            finally:
                if api_session is not None:
                    endpoint, form = api_session
                    with suppress(Exception):
                        await context.request.post(endpoint, form=form, max_redirects=0, timeout=5_000)
                await context.close()

    @asynccontextmanager
    async def _browser_submissions(self, page, folder: str, *, names, download: bool, versions: bool):
        listed = asyncio.Event()
        entries = []

        async def capture_listing(response):
            request = response.request
            request_text = unquote_plus(f"{request.url} {request.post_data or ''}")
            if "/webapi/" not in request.url or "SYNO.FileStation.List" not in request_text:
                return
            try:
                payload = await response.json()
            except Exception:
                return
            match = self._browser_listing(payload, folder, request_text)
            if match is not None:
                entries[:] = match
                listed.set()

        page.on("response", capture_listing)
        try:
            for index, segment in enumerate(folder.strip("/").split("/")):
                try:
                    await self._open_folder_in_file_station(page, segment, top_level=index == 0)
                except Exception:
                    diagnostic = os.getenv("SYNOLOGY_PROBE_SCREENSHOT")
                    if diagnostic:
                        with suppress(Exception):
                            await page.screenshot(path=diagnostic, timeout=5_000)
                    raise OSError(f"DSM File Station 無法開啟資料夾「{segment}」") from None
            if names and not versions:
                for filename in sorted(names):
                    item = self._file_station_window(page).get_by_text(filename, exact=True).filter(visible=True).last
                    try:
                        await item.wait_for(state="visible", timeout=10_000)
                    except Exception:
                        raise OSError(f"DSM File Station 資料夾已開啟，但未顯示「{filename}」") from None
                entries[:] = [{"name": name, "path": f"{folder}/{name}", "isdir": False} for name in sorted(names)]
            else:
                try:
                    await asyncio.wait_for(listed.wait(), timeout=15)
                except TimeoutError:
                    raise OSError("DSM File Station 資料夾已開啟，但未擷取到完整檔案清單") from None
        finally:
            page.remove_listener("response", capture_listing)

        files = self._direct_files(entries, folder)
        if names is not None:
            files = [entry for entry in files if entry['name'] in names]
        if versions:
            yield {entry['name']: self._file_version(entry) for entry in files}
        elif not download:
            yield [Path(entry['name']) for entry in files]
        else:
            with TemporaryDirectory() as directory:
                paths = []
                for entry in files:
                    filename = entry['name']
                    item = self._file_station_window(page).get_by_text(filename, exact=True).filter(visible=True).last
                    try:
                        await item.click(button="right", timeout=10_000)
                        async with page.expect_download(timeout=30_000) as transfer:
                            await self._click_text(page, ["下載", "Download"], timeout=8_000)
                        path = Path(directory) / filename
                        await (await transfer.value).save_as(path)
                        paths.append(path)
                    except Exception:
                        raise OSError(f"DSM File Station 找到「{filename}」，但下載未完成") from None
                yield paths

    async def _open_folder_in_file_station(self, page, segment: str, *, top_level: bool):
        window = self._file_station_window(page)
        entry = window.get_by_text(segment, exact=True).filter(visible=True).first
        await entry.wait_for(state="visible", timeout=60_000)
        for attempt in range(3):
            window = self._file_station_window(page)
            node = window.locator("a.x-tree-node-anchor").get_by_text(segment, exact=True).filter(visible=True).first
            if await node.count():
                await node.click(timeout=4_000)
                await self._wait_folder_navigation(page, segment)
                return
            entry = window.locator(".x-grid3-col-filename, [role='option']").get_by_text(segment, exact=True).filter(visible=True).last
            if await entry.count():
                await entry.dblclick(timeout=4_000)
                await self._wait_folder_navigation(page, segment)
                return
            if attempt < 2:
                if top_level and attempt == 0:
                    await self._restore_file_station(page)
                await page.wait_for_timeout(2_000)
        raise RuntimeError("找不到指定的 File Station 資料夾")

    async def _wait_folder_navigation(self, page, segment: str):
        # File Station updates the tree selection before its file grid.
        # Wait for the grid's accessible path before creating anything.
        await page.wait_for_function(
            """segment => Array.from(document.querySelectorAll('.syno-sds-fs-win.active-win a.x-grid3-focus[aria-label]'))
                .some(e => e.getAttribute('aria-label').split('/').filter(Boolean).pop() === segment)""",
            arg=segment, timeout=60_000,
        )

    @staticmethod
    def _file_station_window(page):
        return page.locator(".syno-sds-fs-win.active-win:visible, .syno-sds-fs-win:visible:not(.deactive-win)").last

    async def _restore_file_station(self, page) -> bool:
        button = page.locator(
            "#sds-taskbar .taskbar-button-wrapper.launched[syno-id='SYNO.SDS.App.FileStation3.Instance'] .taskbar-button"
        ).filter(visible=True).last
        if not await button.count():
            return False
        await button.click(timeout=4_000)
        return True

    @staticmethod
    def _browser_listing(payload, folder: str, request_text: str) -> list[dict] | None:
        if isinstance(payload, dict):
            entries = payload.get("files")
            if isinstance(entries, list):
                if entries and all(entry.get("path", "").startswith(f"{folder}/") for entry in entries):
                    return entries
                if not entries and (
                    folder in request_text or json.dumps(folder, ensure_ascii=True)[1:-1] in request_text
                ):
                    return []
            children = payload.values()
        elif isinstance(payload, list):
            children = payload
        else:
            return None
        for child in children:
            found = SynologyBrowserNas._browser_listing(child, folder, request_text)
            if found is not None:
                return found
        return None

    async def submission_versions(self, folder_name: str) -> dict[str, str]:
        async with self.open_submissions(folder_name, versions=True) as versions:
            return versions

    @staticmethod
    def _file_version(entry: dict) -> str:
        additional = entry.get('additional', {})
        clocks = additional.get('time', {})
        return f"{additional.get('size', '')}:{clocks.get('mtime', '')}:{clocks.get('ctime', '')}"

    @staticmethod
    def _api_data(response: dict) -> dict:
        if not response.get("success"):
            code = response.get("error", {}).get("code", "未知")
            raise OSError(f"DSM File Station API 失敗（代碼 {code}）")
        return response["data"]

    @staticmethod
    def _direct_files(entries: list[dict], folder: str) -> list[dict]:
        return [entry for entry in entries if (
            not entry['isdir'] and entry['name'] not in {"", ".", ".."}
            and not any(character in entry['name'] for character in "/\\:")
            and entry['path'] == f"{folder}/{entry['name']}"
        )]

    @staticmethod
    async def _click_text(page, patterns: list[str], *, double: bool = False, timeout: int = 8_000):
        for pattern in patterns:
            locator = page.get_by_text(pattern, exact=True).filter(visible=True).last
            try:
                if double:
                    await locator.dblclick(timeout=timeout)
                else:
                    await locator.click(timeout=timeout)
                return
            except Exception:
                continue
        raise RuntimeError(f"DSM browser could not find any of: {patterns}")

    async def _ensure_login(self, page):
        await page.goto(self.base, wait_until="domcontentloaded", timeout=60_000)
        desktop = page.locator(".sds-desktop-shortcut").get_by_text("File Station", exact=True)
        user_input = page.locator('input[name="account"]:visible, input[name="username"]:visible, input[type="text"]:visible').first
        await desktop.or_(user_input).first.wait_for(state="visible", timeout=90_000)
        if await desktop.is_visible():
            return

        password_input = page.locator('input[name="passwd"], input[name="password"], input[type="password"]').first
        if await user_input.count() and await user_input.is_visible():
            await user_input.fill(self.user)
            await user_input.press("Enter")
            await page.wait_for_timeout(1_500)

        password_input = page.locator('input[name="passwd"], input[name="password"], input[type="password"]').first
        await password_input.wait_for(state="visible", timeout=60_000)
        if await password_input.count() and await password_input.is_visible():
            await password_input.fill(self.password)
            await password_input.press("Enter")

        try:
            await desktop.wait_for(state="visible", timeout=90_000)
        except Exception as exc:
            raise RuntimeError(
                "DSM login is waiting for verification. Complete login in the opened Chrome window, then run the test again."
            ) from exc

    async def create_upload_folder(self, title: str, starts_at: datetime, *, folder_name: str | None = None) -> str:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed; run: py -V:3.12 -m pip install -r requirements.txt") from exc

        name = folder_name or safe_name(title, starts_at)
        Path(self.profile).mkdir(parents=True, exist_ok=True)
        async with self.browser_lock, async_playwright() as playwright:
            launch_options = {
                "headless": self.headless,
                "ignore_https_errors": True,
                "viewport": {"width": 1440, "height": 960},
            }
            # The container image ships Playwright Chromium. Windows deployments
            # can keep using the locally installed Google Chrome channel.
            if self.browser_channel:
                launch_options["channel"] = self.browser_channel
            context = await playwright.chromium.launch_persistent_context(
                self.profile,
                **launch_options,
            )
            await context.grant_permissions(["clipboard-read", "clipboard-write"])
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await self._ensure_login(page)
                if not await self._restore_file_station(page):
                    shortcut = page.locator(".sds-desktop-shortcut li.launch-icon").filter(has_text="File Station").first
                    await shortcut.click(timeout=15_000)
                try:
                    await self._file_station_window(page).wait_for(state="visible", timeout=60_000)
                except Exception:
                    await self._restore_file_station(page)
                    await self._file_station_window(page).wait_for(state="visible", timeout=60_000)

                for index, segment in enumerate(part for part in self.parent.split("/") if part):
                    await self._open_folder_in_file_station(page, segment, top_level=index == 0)

                window = self._file_station_window(page)
                grid = window.locator('.x-grid3-body')
                folder = grid.get_by_text(name, exact=True).filter(visible=True).last
                if not await folder.count():
                    await self._click_text(page, ["建立資料夾", "新增資料夾", "Create folder"], timeout=10_000)
                    field = page.locator('input[type="text"]:visible').last
                    await field.fill(name)
                    await self._click_text(page, ["確定", "OK", "建立", "Create"])
                    await page.wait_for_timeout(2_000)

                folder = grid.get_by_text(name, exact=True).filter(visible=True).last
                await folder.wait_for(state="visible", timeout=60_000)
                await folder.click(button="right", timeout=15_000)
                await self._click_text(page, ["建立檔案請求", "Create file request"], timeout=10_000)

                api_results: list[object] = []

                async def capture_response(response):
                    if "entry.cgi" not in response.url:
                        return
                    try:
                        api_results.append(await response.json())
                    except Exception:
                        pass

                page.on("response", capture_response)
                await self._click_text(page, ["儲存", "Save"], timeout=10_000)
                await page.wait_for_timeout(2_000)

                values = await page.locator("input").evaluate_all("els => els.map(e => e.value)")
                hrefs = await page.locator("a").evaluate_all("els => els.map(e => e.href)")
                try:
                    clipboard_text = await page.evaluate("navigator.clipboard.readText()")
                except Exception:
                    clipboard_text = ""
                url = self._find_file_request_url([*values, *hrefs, clipboard_text, *api_results])
                if not url:
                    raise RuntimeError("DSM created the request but the browser could not read its /sharing/ URL")
                return url
            except Exception:
                log_dir = Path("./work/logs")
                log_dir.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(log_dir / "dsm-browser-error.png"), full_page=True)
                raise
            finally:
                await context.close()


def build_nas(driver: str):
    if driver == "synology_browser":
        return SynologyBrowserNas()
    if driver == "local":
        return LocalNas()
    raise ValueError(f"Unsupported NAS_DRIVER: {driver!r}; use 'local' or 'synology_browser'")
