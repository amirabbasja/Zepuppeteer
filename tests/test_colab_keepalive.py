"""KeepAlive actions operate on local fixtures without touching real notebooks."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call, patch

from zepuppeteer.brave import find_brave, launch_options
from zepuppeteer.models import NeedsAttention, Profile
from zepuppeteer.tasks.colab.open_notebook import keepAlive


TASK = "zepuppeteer.tasks.colab.open_notebook"
BRAVE = find_brave()


class KeepAliveValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_selection_does_not_touch_page(self):
        for kwargs in ({}, {"task_number": True}, {"task_number": 6},
                       {"task_number": 1, "random": True}, {"random": "true"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await keepAlive(None, **kwargs)


@unittest.skipUnless(BRAVE and importlib.util.find_spec("playwright"), "Requires Brave and Playwright")
class KeepAliveBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_actions_and_random_selection(self):
        from playwright.async_api import async_playwright

        with tempfile.TemporaryDirectory(prefix="zepuppeteer-keepalive-") as root:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch_persistent_context(
                    **launch_options(BRAVE, Profile("keepalive-test", Path(root) / "browser"), True))
                try:
                    page = await browser.new_page()
                    for task in (1, 2, 3, 4):
                        await page.set_content('''
                            <div role="menubar">
                              <button onclick="document.querySelector('#file').hidden=false">File</button>
                              <button onclick="document.querySelector('#insert').hidden=false">Insert</button>
                            </div>
                            <div id="file" hidden>
                              <button role="menuitem" onclick="window.saved=true; window.actions.push('save')">Save</button>
                              <button role="menuitem" onclick="throw Error('Wrong Save')">Save a copy</button>
                            </div>
                            <button aria-label="Additional connection options"
                              onclick="document.querySelector('#resources').hidden=false">Options</button>
                            <button id="resources" hidden role="menuitem"
                              onclick="window.viewed=true">View resources</button>
                            <div id="insert" hidden>
                              <button role="menuitem" onclick="insertCell()">Code cell</button>
                            </div>
                            <div class="cell code" id="existing"><textarea>original code</textarea></div>
                            <script>
                              window.saved=false; window.viewed=false;
                              window.deletedContents=null; window.runCalls=0; window.actions=[];
                              function deleteCell(button) {
                                const cell=button.parentElement;
                                window.deletedContents=cell.querySelector('textarea').value;
                                window.actions.push('delete');
                                cell.remove();
                              }
                              function insertCell() {
                                const cell = document.createElement('div');
                                cell.id='new-cell'; cell.className='cell code';
                                cell.innerHTML='<textarea></textarea><button aria-label="Run cell" '
                                  + 'onclick="window.runCalls++">Run</button><button aria-label="Delete cell" '
                                  + 'onclick="deleteCell(this)">Trash</button>';
                                document.querySelector('#existing').before(cell);
                              }
                            </script>
                        ''')
                        sleep = AsyncMock()
                        with patch(f"{TASK}.asyncio", SimpleNamespace(sleep=sleep)), \
                             patch(f"{TASK}.random_module.randint", return_value=0):
                            if task == 4:
                                with patch(f"{TASK}.random_module.choice", return_value=4):
                                    result = await keepAlive(page, random=True)
                            else:
                                result = await keepAlive(page, task_number=task)
                        self.assertEqual(result, task)
                        self.assertEqual(await page.locator('#existing textarea').input_value(), "original code")
                        self.assertEqual(await page.locator('#new-cell').count(), 0)
                        self.assertEqual(await page.evaluate('window.saved'), task in (1, 4))
                        self.assertEqual(await page.evaluate('window.viewed'), task == 2)
                        expected_pauses = {
                            1: [2, 10], 2: [2, 10],
                            3: [2, 2, 1, 10], 4: [2, 1, 1, 2, 10],
                        }
                        self.assertEqual(sleep.await_args_list, [call(seconds) for seconds in expected_pauses[task]])
                        self.assertEqual(await page.evaluate('window.runCalls'), 0)
                        if task == 4:
                            self.assertEqual(await page.evaluate('window.deletedContents'),
                                             'print("status update")')
                            self.assertEqual(await page.evaluate('window.actions'), ['delete', 'save'])
                finally:
                    await browser.close()

    async def test_task_four_types_when_monaco_overlay_intercepts_clicks(self):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

        with tempfile.TemporaryDirectory(prefix="zepuppeteer-monaco-focus-") as root:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch_persistent_context(
                    **launch_options(BRAVE, Profile("monaco-focus-test", Path(root) / "browser"), True))
                try:
                    page = await browser.new_page()
                    await page.set_content('''
                        <style>
                          .monaco-editor { position: relative; width: 300px; height: 60px; }
                          .monaco-editor textarea { position: absolute; inset: 0; width: 300px;
                            height: 60px; padding: 0; border: 0; }
                          .monaco-scrollable-element { position: absolute; inset: 0; z-index: 1; }
                          .view-line { width: 100%; height: 100%; background: white; }
                        </style>
                        <div role="menubar">
                          <button onclick="document.querySelector('#file').hidden=false">File</button>
                          <button onclick="document.querySelector('#insert').hidden=false">Insert</button>
                        </div>
                        <div id="file" hidden>
                          <button role="menuitem" onclick="window.saved=true;
                            window.actions.push('save')">Save</button>
                        </div>
                        <div id="insert" hidden>
                          <button role="menuitem" onclick="insertCell()">Code cell</button>
                        </div>
                        <div class="cell code" id="existing"><textarea>original code</textarea></div>
                        <script>
                          window.saved=false; window.deletedContents=null; window.deletedOverlayPresent=false;
                          window.runCalls=0; window.actions=[];
                          function deleteCell(button) {
                            const cell=button.parentElement;
                            window.deletedContents=cell.querySelector('textarea').value;
                            window.deletedOverlayPresent=Boolean(cell.querySelector('.view-line'));
                            window.actions.push('delete');
                            cell.remove();
                          }
                          function insertCell() {
                            const cell=document.createElement('div');
                            cell.id='new-cell'; cell.className='cell code';
                            cell.innerHTML='<div class="monaco-editor"><textarea aria-label="Editor content"></textarea>'
                              + '<div class="monaco-scrollable-element"><div class="view-line">Editor text layer</div>'
                              + '</div></div><button aria-label="Run cell" onclick="window.runCalls++">Run</button>'
                              + '<button aria-label="Delete cell" onclick="deleteCell(this)">Trash</button>';
                            document.querySelector('#existing').before(cell);
                            document.querySelector('#insert').hidden=true;
                          }
                        </script>
                    ''')
                    # Prove the fixture reproduces the reported pointer interception.
                    await page.evaluate('insertCell()')
                    with self.assertRaises(PlaywrightTimeoutError):
                        await page.locator('#new-cell textarea').click(timeout=250)
                    await page.locator('#new-cell').evaluate('cell => cell.remove()')

                    sleep = AsyncMock()
                    with patch(f"{TASK}.asyncio", SimpleNamespace(sleep=sleep)), \
                         patch(f"{TASK}.random_module.randint", return_value=0):
                        result = await keepAlive(page, task_number=4)
                    self.assertEqual(result, 4)
                    self.assertEqual(await page.evaluate('window.deletedContents'),
                                     'print("status update")')
                    self.assertTrue(await page.evaluate('window.saved'))
                    self.assertEqual(await page.evaluate('window.actions'), ['delete', 'save'])
                    self.assertEqual(await page.evaluate('window.runCalls'), 0)
                    self.assertEqual(await page.locator('#new-cell').count(), 0)
                    self.assertEqual(await page.locator('#existing textarea').input_value(), "original code")
                    self.assertTrue(await page.evaluate('window.deletedOverlayPresent'))
                    self.assertEqual(sleep.await_args_list, [call(2), call(1), call(1), call(2), call(10)])
                finally:
                    await browser.close()

    async def test_status_update_preserves_marker_and_other_cells(self):
        from playwright.async_api import async_playwright

        with tempfile.TemporaryDirectory(prefix="zepuppeteer-status-") as root:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch_persistent_context(
                    **launch_options(BRAVE, Profile("status-test", Path(root) / "browser"), True))
                try:
                    page = await browser.new_page()
                    for editor_kind in ("textarea", "contenteditable", "monaco"):
                        with self.subTest(editor=editor_kind):
                            first_line = 'print("keep this")'
                            if editor_kind == "textarea":
                                first_line = 'print("' + 'long wrapped prefix ' * 60 + '")'
                            marker_line = {
                                "textarea": '#  STatus   update',
                                "contenteditable": '#\u00a0Status\u00a0update\u00a0Comments',
                                "monaco": '# Status update Comments',
                            }[editor_kind]
                            prefix = first_line + '\n' + marker_line + '\n'
                            source = prefix + 'old status\nremove this too'
                            await self.status_fixture(page, source, editor_kind=editor_kind)
                            sleep = AsyncMock()
                            with patch(f"{TASK}.asyncio", SimpleNamespace(sleep=sleep)), \
                                 patch(f"{TASK}.random_module.randint", side_effect=lambda a, b:
                                       0 if (a, b) == (60, 180) else a) as randint, \
                                 patch(f"{TASK}.random_module.choice", return_value=5) as choice:
                                if editor_kind == "textarea":
                                    result = await keepAlive(page, random=True)
                                    self.assertIn(5, choice.call_args.args[0])
                                else:
                                    result = await keepAlive(page, task_number=5)
                            self.assertEqual(result, 5)
                            editor = page.locator('#first textarea, #first [contenteditable="true"]')
                            output = await editor.inner_text() if editor_kind == "contenteditable" else await editor.input_value()
                            self.assertTrue(output.startswith(prefix), output)
                            status = output[len(prefix):].rstrip("\n")
                            match = re.fullmatch(
                                r"# Time: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \+0330)"
                                r" \| CNN training: running good \| percent done: (\d+)%"
                                r" \| epoches done : (\d+)", status)
                            self.assertIsNotNone(match, output)
                            timestamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S %z")
                            self.assertLess(abs((datetime.now(timezone.utc) - timestamp).total_seconds()), 10)
                            self.assertIn(int(match[2]), range(101))
                            self.assertIn(int(match[3]), range(1000, 10001))
                            self.assertEqual(await page.locator('#second textarea').input_value(), "untouched second cell")
                            self.assertTrue(await page.evaluate('window.saved'))
                            self.assertEqual(await page.evaluate('window.savedText'), output)
                            inputs = await page.evaluate('window.statusInputs')
                            self.assertEqual(inputs[-len(status):], list(status))
                            self.assertEqual(await page.evaluate('window.savedInputCount'), len(inputs))
                            self.assertEqual(randint.call_args_list.count(call(60, 180)), len(status))
                            self.assertEqual(sleep.await_args_list, [call(1), call(2), call(2), call(10)])
                            if editor_kind == "monaco":
                                self.assertGreater(await page.evaluate('window.modelReads'), 0)

                    for source in ('print("no marker")', 'print("# Status update")\nkeep this',
                                   '# Status updater\nkeep this'):
                        with self.subTest(missing_marker=source):
                            await self.status_fixture(page, source)
                            with patch(f"{TASK}.asyncio", SimpleNamespace(sleep=AsyncMock())):
                                with self.assertRaises(NeedsAttention):
                                    await keepAlive(page, task_number=5)
                            self.assertEqual(await page.locator('#first textarea').input_value(), source)
                            self.assertFalse(await page.evaluate('window.saved'))

                    # Monaco's textarea is only a partial input buffer. Without
                    # its full model, the helper must leave the notebook alone.
                    source = '# Status update\npartial editor input'
                    await self.status_fixture(page, source)
                    await page.locator('#first textarea').evaluate('''input => {
                        const editor = document.createElement('div');
                        editor.className = 'monaco-editor';
                        input.replaceWith(editor);
                        editor.append(input);
                    }''')
                    with patch(f"{TASK}.asyncio", SimpleNamespace(sleep=AsyncMock())):
                        with self.assertRaises(NeedsAttention):
                            await keepAlive(page, task_number=5)
                    self.assertEqual(await page.locator('#first textarea').input_value(), source)
                    self.assertFalse(await page.evaluate('window.saved'))

                    await page.set_content('<p>No cells</p>')
                    with self.assertRaises(NeedsAttention):
                        await keepAlive(page, task_number=5)
                finally:
                    await browser.close()

    async def status_fixture(self, page, source, *, editor_kind="textarea"):
        editor = '<textarea style="width: 100px; height: 80px"></textarea>'
        if editor_kind == "contenteditable":
            editor = '<div contenteditable="true" style="white-space: pre-wrap"></div>'
        elif editor_kind == "monaco":
            editor = f'<div class="monaco-editor">{editor}</div>'
        await page.set_content(f'''
            <div role="menubar">
              <button onclick="document.querySelector('#file').hidden=false">File</button>
            </div>
            <div id="file" hidden>
              <button role="menuitem" onclick="saveStatus()">Save</button>
            </div>
            <div class="cell code" id="first">{editor}</div>
            <div class="cell code" id="second"><textarea>untouched second cell</textarea></div>
            <script>
              window.saved=false; window.monaco=undefined; window.modelReads=0;
              window.statusInputs=[]; window.savedText=null; window.savedInputCount=null;
              function saveStatus() {{
                const editor=document.querySelector('#first textarea, #first [contenteditable="true"]');
                window.savedText=editor.tagName === 'TEXTAREA' ? editor.value : editor.innerText;
                window.savedInputCount=window.statusInputs.length;
                window.saved=true;
              }}
            </script>
        ''')
        await page.locator('#first textarea, #first [contenteditable="true"]').fill(source)
        await page.locator('#first textarea, #first [contenteditable="true"]').evaluate('''editor => {
            editor.addEventListener('input', event => window.statusInputs.push(event.data));
        }''')
        if editor_kind == "monaco":
            await page.evaluate('''() => {
                const editor = document.querySelector('#first .monaco-editor');
                window.monaco = {editor: {getEditors: () => [{
                    getDomNode: () => editor,
                    getModel: () => ({getValue: () => {
                        window.modelReads++;
                        return editor.querySelector('textarea').value;
                    }})
                }]}};
            }''')
