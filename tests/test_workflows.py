import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from zepuppeteer.models import NeedsAttention, Profile, Step, TaskResult, Workflow
from zepuppeteer.runner import run_workflow
from zepuppeteer.state import StateStore, read_history
from zepuppeteer.workflows import load_workflow, resolve_tasks, workflow_path


class WorkflowConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-config-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "workflow.toml"

    def load(self, text):
        self.path.write_text(text)
        return load_workflow(self.path)

    def test_demo_resolves_and_step_ids_support_repeated_tasks(self):
        workflow = load_workflow("config/workflows/demo.toml")
        self.assertEqual(set(resolve_tasks(workflow)), {"demo.fill_form", "demo.verify_result"})
        workflow = self.load('[[steps]]\nid="first"\ntask="demo.fill_form"\n'
                             '[[steps]]\nid="second"\ntask="demo.fill_form"\ninput_from="first"\n')
        self.assertEqual(workflow.steps[1].input_from, "first")
        self.assertEqual(workflow_path("demo", "config"), Path("config/workflows/demo.toml"))
        self.assertEqual(workflow_path(str(self.path), "unused"), self.path)

    def test_invalid_plans_are_rejected_before_a_browser_can_launch(self):
        cases = [
            ('name="empty"', "at least one"),
            ('policy="continue"\n[[steps]]\ntask="demo.fill_form"', "Unknown workflow"),
            ('on_profile_failure="ignore"\n[[steps]]\ntask="demo.fill_form"', "on_profile_failure"),
            ('[[steps]]\ntask="../escape"', "dotted Python"),
            ('[[steps]]\ntask="demo.fill_form"\ninput_from="future"', "earlier step"),
            ('[[steps]]\ntask="demo.fill_form"\n[[steps]]\ntask="demo.fill_form"', "duplicate"),
            ('[[steps]]\ntask="demo.fill_form"\nretries=1', "retry_safe"),
            ('[[steps]]\ntask="demo.fill_form"\nretries=true', "nonnegative integer"),
            ('[[steps]]\ntask="demo.fill_form"\nretry_safe="yes"', "boolean"),
            ('[[steps]]\ntask="demo.fill_form"\ntimeout_seconds=0', "positive"),
            ('[[steps]]\ntask="demo.fill_form"\ntimeout_seconds=inf', "finite"),
            ('[[steps]]\ntask="demo.fill_form"\nretry_delay_seconds=-1', "nonnegative"),
            ('[[steps]]\ntask="demo.fill_form"\nparam={x=1}', "Unknown step"),
            ('[[steps]]\ntask="demo.fill_form"\nparams=[]', "params"),
            ('[[steps]]\ntask="demo.fill_form"\n[steps.params]\ndate=2026-01-01', "JSON-compatible"),
        ]
        for source, message in cases:
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, message):
                self.load(source)

    def test_task_contract_is_validated(self):
        workflow = Workflow("test", (Step("one", "demo.fill_form"),))
        for task in [lambda ctx: TaskResult(), AsyncMock()]:
            # AsyncMock accepts one argument; a plain synchronous function must fail.
            with patch("zepuppeteer.workflows.importlib.import_module", return_value=SimpleNamespace(run=task)):
                if isinstance(task, AsyncMock):
                    self.assertIs(resolve_tasks(workflow)["demo.fill_form"], task)
                else:
                    with self.assertRaisesRegex(ValueError, "async def run"):
                        resolve_tasks(workflow)
        async def invalid(first, second):
            pass
        with patch("zepuppeteer.workflows.importlib.import_module", return_value=SimpleNamespace(run=invalid)):
            with self.assertRaisesRegex(ValueError, "one TaskContext"):
                resolve_tasks(workflow)


class WorkflowRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.notifier = AsyncMock(return_value=True)
        notifier_patch = patch("zepuppeteer.runner.notify_attention", self.notifier)
        notifier_patch.start()
        self.addCleanup(notifier_patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-runner-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = StateStore(self.root / "runs.sqlite")
        self.addCleanup(self.state.connection.close)
        self.profiles = [Profile(name, self.root / name) for name in ["one", "two"]]
        self.contexts = [self.context(), self.context()]
        self.launch = AsyncMock(side_effect=self.contexts)
        self.playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=self.launch))

    def context(self):
        return SimpleNamespace(close=AsyncMock(), add_init_script=AsyncMock(), pages=[],
                               tracing=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()))

    async def execute(self, steps, tasks, *, policy="continue", profiles=None):
        return await run_workflow(self.playwright, self.profiles if profiles is None else profiles,
                                  "/brave", Workflow("test", tuple(steps), policy), tasks,
                                  self.state, self.root / "artifacts", headless=True)

    def details(self, run_id=None):
        run_id = run_id or read_history(self.state.path)[0]["id"]
        return read_history(self.state.path, run_id=run_id)

    async def test_colab_vpn_connects_before_browser_and_closes_after_browser(self):
        events = []
        connection = SimpleNamespace(proxy={"server": "http://127.0.0.1:1234",
                                             "username": "test", "password": "password"})
        async def run_on_vpn(operation):
            return await operation
        connection.run = run_on_vpn

        @asynccontextmanager
        async def connect(profile):
            events.append("vpn connected")
            try:
                yield connection
            finally:
                self.contexts[0].close.assert_awaited_once()
                events.append("vpn closed")

        async def launch(**options):
            self.assertEqual(events, ["vpn connected"])
            self.assertEqual(options["proxy"], connection.proxy)
            events.append("browser launched")
            return self.contexts[0]

        async def task(ctx):
            self.assertIs(ctx.vpn, connection)
            events.append("task")
            return TaskResult()

        self.launch.side_effect = launch
        with patch("zepuppeteer.runner.colab_uses_vpn", return_value=True), \
                patch("zepuppeteer.runner.connect_vpn", connect):
            result = await self.execute([Step("open", "colab.open_notebook")],
                                        {"colab.open_notebook": task}, profiles=self.profiles[:1])
        self.assertEqual(result.status, "success")
        self.assertEqual(events, ["vpn connected", "browser launched", "task", "vpn closed"])

    async def test_vpn_failure_notifies_without_launching_browser_or_task(self):
        @asynccontextmanager
        async def connect(profile):
            raise NeedsAttention("VPN has no heartbeat")
            yield

        task = AsyncMock()
        with patch("zepuppeteer.runner.colab_uses_vpn", return_value=True), \
                patch("zepuppeteer.runner.connect_vpn", connect):
            result = await self.execute([Step("open", "colab.open_notebook")],
                                        {"colab.open_notebook": task}, profiles=self.profiles[:1])
        self.assertEqual(result.failed, 1)
        self.launch.assert_not_awaited()
        task.assert_not_awaited()
        self.notifier.assert_awaited_once_with(self.profiles[0], "colab", "VPN has no heartbeat")

    async def test_vpn_disabled_does_not_connect_or_configure_proxy(self):
        task = AsyncMock(return_value=TaskResult())
        with patch("zepuppeteer.runner.colab_uses_vpn", return_value=False), \
                patch("zepuppeteer.runner.connect_vpn") as connect:
            result = await self.execute([Step("open", "colab.open_notebook")],
                                        {"colab.open_notebook": task}, profiles=self.profiles[:1])
        self.assertEqual(result.status, "success")
        connect.assert_not_called()
        self.assertNotIn("proxy", self.launch.call_args.kwargs)
        self.assertIsNone(task.call_args.args[0].vpn)

    async def test_pipeline_passes_outputs_and_opens_each_profile_only_once(self):
        seen = []
        async def first(ctx):
            if ctx.profile.name == "two":
                self.contexts[0].close.assert_awaited_once()
            seen.append((ctx.profile.name, ctx.step_id))
            self.assertEqual(ctx.params, {"prefix": "hello"})
            ctx.params["prefix"] = "mutated"
            self.assertTrue(ctx.artifact_dir.is_dir())
            return TaskResult(outputs={"value": ctx.profile.name})
        async def second(ctx):
            seen.append((ctx.profile.name, ctx.step_id))
            self.assertEqual(ctx.inputs, {"value": ctx.profile.name})
            return TaskResult(outputs={"verified": True})
        result = await self.execute([Step("first", "first", {"prefix": "hello"}),
                                     Step("second", "second", input_from="first")],
                                    {"first": first, "second": second})
        self.assertEqual(seen, [("one", "first"), ("one", "second"), ("two", "first"), ("two", "second")])
        self.assertEqual((result.status, result.succeeded, result.failed), ("success", 2, 0))
        details = self.details(result.run_id)
        self.assertTrue(all(item["status"] == "success" for item in details["steps"]))
        self.assertEqual(details["steps"][1]["outputs"], {"verified": True})
        self.launch.assert_awaited()
        self.assertEqual(self.launch.await_count, 2)
        for context in self.contexts:
            context.close.assert_awaited_once()

    async def test_failure_skips_remaining_steps_and_continues_next_profile(self):
        async def first(ctx):
            if ctx.profile.name == "one":
                raise RuntimeError("website failure")
            return TaskResult()
        second = AsyncMock(return_value=TaskResult())
        result = await self.execute([Step("first", "first"), Step("second", "second")],
                                    {"first": first, "second": second})
        self.assertEqual((result.status, result.succeeded, result.failed), ("failed", 1, 1))
        details = self.details(result.run_id)
        self.assertEqual([s["status"] for s in details["steps"]], ["failed", "skipped", "success", "success"])
        first = details["steps"][0]
        evidence = json.loads((Path(first["artifact_dir"]) / "failure.json").read_text())
        self.assertIn("website failure", evidence["error"])
        self.contexts[0].tracing.stop.assert_awaited_once_with(path=str(Path(first["artifact_dir"]) / "trace.zip"))
        second.assert_awaited_once()

    async def test_stop_policy_records_unstarted_profiles_as_skipped(self):
        task = AsyncMock(side_effect=RuntimeError("failed"))
        result = await self.execute([Step("one", "task")], {"task": task}, policy="stop")
        self.assertEqual((result.failed, result.skipped), (1, 1))
        self.launch.assert_awaited_once()
        self.assertEqual([p["status"] for p in self.details()["profiles"]], ["failed", "skipped"])

    async def test_retry_has_separate_artifacts_and_persists_attempt_count(self):
        calls = []
        async def task(ctx):
            calls.append(ctx.artifact_dir)
            if len(calls) == 1:
                raise RuntimeError("temporary")
            return TaskResult(outputs={"ok": True})
        result = await self.execute([Step("one", "task", retries=1, retry_safe=True, retry_delay_seconds=0)],
                                    {"task": task}, profiles=self.profiles[:1])
        self.assertEqual(result.status, "success")
        self.assertNotEqual(calls[0], calls[1])
        step, = self.details()["steps"]
        self.assertEqual(step["attempts"], 2)
        self.assertIsNone(step["error"])
        self.assertTrue((calls[0] / "failure.json").is_file())

    async def test_manual_attention_is_never_retried(self):
        task = AsyncMock(side_effect=NeedsAttention("MFA required"))
        result = await self.execute([Step("one", "task", retries=3, retry_safe=True)],
                                    {"task": task}, profiles=self.profiles[:1])
        self.assertEqual(result.failed, 1)
        task.assert_awaited_once()

    async def test_manual_attention_sends_one_profile_platform_description_alert(self):
        task = AsyncMock(side_effect=NeedsAttention("Runtime disconnected"))
        result = await self.execute(
            [Step("open", "colab.open_notebook", retries=3, retry_safe=True)],
            {"colab.open_notebook": task}, profiles=self.profiles[:1])
        self.assertEqual(result.failed, 1)
        self.notifier.assert_awaited_once_with(self.profiles[0], "colab", "Runtime disconnected")
        self.assertIn("Runtime disconnected", self.details()["steps"][0]["error"])

    async def test_ordinary_failure_does_not_send_attention_alert(self):
        task = AsyncMock(side_effect=RuntimeError("Unexpected website error"))
        await self.execute([Step("open", "colab.open_notebook")],
                           {"colab.open_notebook": task}, profiles=self.profiles[:1])
        self.notifier.assert_not_awaited()

    async def test_retry_exhaustion_preserves_original_failure_when_evidence_fails(self):
        self.contexts[0].tracing.start.side_effect = RuntimeError("trace unavailable")
        task = AsyncMock(side_effect=RuntimeError("website failed"))
        summary = await self.execute([Step("one", "task", retries=1, retry_safe=True, retry_delay_seconds=0)],
                                      {"task": task}, profiles=self.profiles[:1])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(task.await_count, 2)
        step, = self.details()["steps"]
        self.assertEqual(step["attempts"], 2)
        self.assertIn("website failed", step["error"])
        self.contexts[0].tracing.stop.assert_not_awaited()

    async def test_timeout_cancels_the_task_and_records_failure(self):
        ended = asyncio.Event()
        async def task(ctx):
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()
        result = await self.execute([Step("one", "task", timeout_seconds=0.01)],
                                    {"task": task}, profiles=self.profiles[:1])
        self.assertEqual(result.failed, 1)
        self.assertTrue(ended.is_set())
        self.assertIn("exceeded", self.details()["steps"][0]["error"])
        self.contexts[0].close.assert_awaited_once()

    async def test_cancellation_finishes_history_and_closes_the_active_browser(self):
        entered = asyncio.Event()
        async def task(ctx):
            entered.set()
            await asyncio.Event().wait()
        operation = asyncio.create_task(self.execute([Step("one", "task")], {"task": task}))
        await entered.wait()
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        details = self.details()
        self.assertEqual(details["status"], "cancelled")
        self.assertEqual([p["status"] for p in details["profiles"]], ["cancelled", "skipped"])
        self.assertEqual([s["status"] for s in details["steps"]], ["cancelled", "skipped"])
        self.contexts[0].close.assert_awaited_once()

    async def test_cancellation_during_cleanup_still_waits_for_browser_close(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
        self.contexts[0].close.side_effect = close
        operation = asyncio.create_task(self.execute([Step("one", "task")],
            {"task": AsyncMock(return_value=TaskResult())}, profiles=self.profiles[:1]))
        await entered.wait()
        operation.cancel()
        await asyncio.sleep(0)
        self.assertFalse(operation.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertEqual(self.details()["profiles"][0]["status"], "cancelled")
        self.contexts[0].close.assert_awaited_once()

    async def test_skip_only_skips_steps_that_require_the_skipped_output(self):
        tasks = {"skip": AsyncMock(return_value=TaskResult(status="skipped", message="Nothing to export")),
                 "dependent": AsyncMock(), "independent": AsyncMock(return_value=TaskResult())}
        result = await self.execute([Step("skip", "skip"), Step("dep", "dependent", input_from="skip"),
                                     Step("other", "independent")], tasks, profiles=self.profiles[:1])
        self.assertEqual(result.status, "success")
        tasks["dependent"].assert_not_awaited()
        tasks["independent"].assert_awaited_once()
        self.assertEqual([s["status"] for s in self.details()["steps"]], ["skipped", "skipped", "success"])

    async def test_invalid_results_are_not_recorded_as_success(self):
        for result in [None, TaskResult(status="failed"), TaskResult(outputs={"path": Path("bad")}),
                       TaskResult(outputs={"value": float("nan")})]:
            with self.subTest(result=result):
                self.launch.side_effect = [self.context()]
                summary = await self.execute([Step("one", "task")],
                    {"task": AsyncMock(return_value=result)}, profiles=self.profiles[:1])
                self.assertEqual(summary.failed, 1)
                self.assertEqual(self.details(summary.run_id)["steps"][0]["status"], "failed")

    async def test_launch_setup_and_cleanup_failures_are_recorded_and_do_not_leak(self):
        self.launch.side_effect = [RuntimeError("busy profile"), self.contexts[1]]
        summary = await self.execute([Step("one", "task")], {"task": AsyncMock(return_value=TaskResult())})
        self.assertEqual((summary.failed, summary.succeeded), (1, 1))
        self.assertEqual(self.details(summary.run_id)["steps"][0]["status"], "skipped")
        self.launch.side_effect = [self.contexts[0]]
        self.contexts[0].add_init_script.side_effect = RuntimeError("setup failed")
        await self.execute([Step("one", "task")], {"task": AsyncMock()}, profiles=self.profiles[:1])
        self.contexts[0].close.assert_awaited_once()
        self.contexts[0].add_init_script.side_effect = None
        self.contexts[0].close.side_effect = RuntimeError("close failed")
        self.launch.side_effect = [self.contexts[0]]
        summary = await self.execute([Step("one", "task")],
            {"task": AsyncMock(return_value=TaskResult())}, profiles=self.profiles[:1])
        self.assertEqual(summary.failed, 1)
        self.assertIn("Browser cleanup", self.details(summary.run_id)["profiles"][0]["error"])
