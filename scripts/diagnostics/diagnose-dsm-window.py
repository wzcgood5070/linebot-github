import asyncio
import json
import os
import shutil
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
# Load the current workspace module supplied beside this diagnostic.
sys.path.insert(0, '/tmp/nas-source')
from nas import SynologyBrowserNas


async def diagnose():
    with TemporaryDirectory() as directory:
        profile = Path(directory) / 'profile'
        shutil.copytree(
            '/app/data/dsm-browser-profile', profile,
            ignore=shutil.ignore_patterns('Singleton*', 'DevToolsActivePort', 'Cache', 'Code Cache', 'GPUCache'),
        )
        os.environ['BROWSER_PROFILE_PATH'] = str(profile)
        nas = SynologyBrowserNas()
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(profile), headless=True, viewport={'width': 1440, 'height': 960},
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                failures = []
                script_errors = []

                def failed_request(request):
                    address = urlsplit(request.url)
                    failures.append({
                        'host': address.hostname, 'path': address.path,
                        'type': request.resource_type, 'failure': request.failure,
                    })

                def failed_response(response):
                    if response.status >= 400:
                        address = urlsplit(response.url)
                        failures.append({'host': address.hostname, 'path': address.path, 'status': response.status})

                page.on('requestfailed', failed_request)
                page.on('response', failed_response)
                page.on('pageerror', lambda error: script_errors.append(error.name))
                try:
                    await nas._ensure_login(page)
                except Exception:
                    print('DSM RESOURCE FAILURES: ' + json.dumps(failures[-15:]), flush=True)
                    print('DSM SCRIPT ERRORS: ' + json.dumps(script_errors[-10:]), flush=True)
                    output = Path('/tmp/diagnostics')
                    if output.is_dir():
                        await page.screenshot(path=str(output / 'dsm-login-state.png'))
                        print('DSM LOGIN: screenshot saved as dsm-login-state.png', flush=True)
                    state = await page.evaluate('''() => ({
                        ready: document.readyState,
                        textLength: document.body ? document.body.innerText.length : 0,
                        visibleInputs: Array.from(document.querySelectorAll('input')).filter(e=>e.getBoundingClientRect().width>0).map(e=>({
                            type:e.type, name:e.name, placeholder:e.placeholder
                        })),
                        desktopCount: document.querySelectorAll('.sds-desktop-shortcut').length,
                        frames: document.querySelectorAll('iframe').length
                    })''')
                    print('DSM LOGIN STATE: ' + json.dumps(state), flush=True)
                    return
                for phase in ('before', 'after'):
                    if phase == 'after':
                        restored = await nas._restore_file_station(page)
                        print('RESTORE_BUTTON_FOUND:', restored, flush=True)
                        if not restored:
                            shortcut = page.locator('.sds-desktop-shortcut li.launch-icon').filter(has_text='File Station').first
                            await shortcut.click(timeout=15_000)
                            print('FILE STATION: desktop shortcut clicked once', flush=True)
                        try:
                            await page.locator('.syno-sds-fs-win').first.wait_for(state='visible', timeout=60_000)
                            print('FILE STATION: window loaded', flush=True)
                            await nas._open_folder_in_file_station(page, 'YOUR_SHARED_FOLDER', top_level=True)
                            await nas._open_folder_in_file_station(page, 'linebot', top_level=False)
                            print('FILE STATION: meeting parent folder opened', flush=True)
                        except Exception:
                            print('FILE STATION: window or meeting parent folder did not become available', flush=True)
                        print('DSM RESOURCE FAILURES: ' + json.dumps(failures[-15:]), flush=True)
                        print('DSM SCRIPT ERRORS: ' + json.dumps(script_errors[-10:]), flush=True)
                        output = Path('/tmp/diagnostics')
                        if output.is_dir():
                            await page.screenshot(path=str(output / 'dsm-file-station-state.png'))
                    state = await page.evaluate('''() => ({
                        windows: Array.from(document.querySelectorAll('.syno-sds-fs-win')).map(e => {
                            const r = e.getBoundingClientRect();
                            const s = getComputedStyle(e);
                            return {classes:e.className, display:s.display, visibility:s.visibility,
                                x:r.x,y:r.y,width:r.width,height:r.height};
                        }),
                        taskButtons: Array.from(document.querySelectorAll('#sds-taskbar .taskbar-button-wrapper')).map(e => ({
                            app:e.getAttribute('syno-id'), classes:e.className
                        })),
                        frames: document.querySelectorAll('iframe').length,
                        fileStationLabels: Array.from(document.querySelectorAll('.x-window-header-text'))
                            .filter(e=>e.textContent.trim()==='File Station').length,
                        sharedFolderVisible: Array.from(document.querySelectorAll('a.x-tree-node-anchor'))
                            .some(e=>e.textContent.trim()==='YOUR_SHARED_FOLDER' && e.getBoundingClientRect().width>0)
                    })''')
                    print('DSM WINDOW ' + phase + ': ' + json.dumps(state), flush=True)
            finally:
                await context.close()


asyncio.run(diagnose())
