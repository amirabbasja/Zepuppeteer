# Zepuppeteer

Zepuppeteer runs Python workflows across persistent Brave profiles using the
visible screen. It starts the installed Brave executable as a normal OS process
and uses screenshots, Tesseract OCR, image matching, and real mouse/keyboard
input. Playwright, WebDriver, browser debugging connections, DOM queries and
injected automation scripts are no longer used.

## Quick start

Requires Python 3.10+, installed Brave, and a **Linux X11 desktop** running as a
normal user. Keep the browser fully visible on the primary monitor. Wayland and
`--headless` are unsupported. Use a dedicated desktop session while tasks run;
manual mouse/keyboard activity can interrupt them.

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-eng xclip python3-venv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

mkdir -p profiles/account-1/src profiles/account-2/src
cp examples/profile.toml profiles/account-1/profile.toml
cp examples/profile.toml profiles/account-2/profile.toml

python -m zepuppeteer run --group demo --workflow demo --dry-run
python -m zepuppeteer run --group demo --workflow demo
python -m zepuppeteer history
```

Alternatively run `uv sync --locked` and prefix commands with `uv run`.
`uv.lock` includes the screen dependencies. No automation browser or driver needs
to be installed. The local demo uses a real HTML form and verifies its visible
result after typing and clicking; it needs no website account or network.

Brave can still be installed automatically from its official Ubuntu/Debian APT
repository, using sudo. `--no-install` disables this and `--executable PATH`
selects an installed executable. Desktop and OCR prerequisites are checked before
installation. Preview, discovery, history, and `vpnCheck` do not require a desktop.

## How buttons are located

The controller identifies the browser window through the launched process group,
brings it to the foreground, and captures its visible pixels. OCR returns labels
and their bounding rectangles. Icon templates use OpenCV matching. A click uses
the rectangle center translated to desktop coordinates. A second capture must
confirm the same position before input. Duplicate matches fail instead of choosing
an arbitrary button. Losing focus, moving the window after capture, or closing
Brave stops input. PyAutoGUI's corner fail-safe remains enabled: move the pointer
to a primary-screen corner to interrupt automation, or use Ctrl+C in the terminal.

Ordinary text controls need no fixed coordinates. Icon-only controls and precise
notebook states need templates configured for your site's current appearance.
Search regions are fractions of the **browser window**, including its toolbar:
`[left, top, width, height]`, each from 0 to 1. Regions restrict recognition; they
are not positions at which the code blindly clicks.

Add these tables to the relevant `profiles/<account>/profile.toml`:

```toml
[settings.screen.targets.google_account]
image = "icons/google-account.png"
region = [0.7, 0.08, 0.3, 0.25]
confidence = 0.9

[settings.screen.targets.colab_file]
text = "File"
region = [0.0, 0.08, 0.35, 0.25]

[settings.screen.targets.colab_runtime]
region = [0.5, 0.08, 0.5, 0.3]

[settings.screen.targets.colab_saved]
text = "All changes saved"
region = [0.0, 0.08, 1.0, 0.25]
```

Create an image template by cropping a screenshot tightly around the intended
icon. Save the PNG beside the account configuration, e.g.
`profiles/<account>/icons/google-account.png`. Template paths resolve relative to
`profile.toml`. Use the same browser zoom, desktop scaling, and theme as the
capture. Change the example regions for your own layout. An unconfigured or
unrecognized icon produces an actionable error and failure evidence.

Available target overrides include:

| Target | Purpose |
| --- | --- |
| `google_account` | Google account avatar button; image required when it has no readable label |
| `google_active_account` | Region containing the account popup's active email at the top |
| `colab_toolbar`, `colab_runtime` | Regions containing the menu bar and runtime toolbar status |
| `colab_runtime_connected`, `colab_runtime_disconnected`, `colab_runtime_connecting`, `colab_runtime_restarting` | Optional image/text targets for runtime state |
| `colab_connection_options` | Additional connection options icon |
| `colab_file`, `colab_save`, `colab_insert`, `colab_run_all`, `colab_connect_hosted` | Optional text/image overrides for menu controls |
| `colab_saved` | Visible saved-state label or image used after saving |
| `colab_first_cell_editor` | A distinctive anchor that focuses the first cell's editor |
| `colab_new_cell`, `colab_delete_cell` | Inserted cell and its delete icon, for keepAlive tasks 3/4 |
| `colab_last_cell_anchor` | Distinctive visible content identifying the final code cell |
| `colab_notebook_idle` | Visible toolbar state proving no execution is already underway |
| `colab_last_cell_running` | Executing indicator in a tight region around the final code cell |
| `colab_last_cell_output` | Region containing only that cell's visible output |
| `colab_resources` | Region containing only the open runtime resources panel |
| `kaggle_account` | Kaggle account avatar/menu button |
| `kaggle_profile` | Your Profile entry in the account menu |
| `kaggle_toolbar`, `kaggle_runtime` | Regions containing the notebook toolbar and session status |
| `kaggle_session_options`, `kaggle_session_status` | Optional session menu icon and menu status region |
| `kaggle_runtime_connected`, `kaggle_runtime_disconnected`, `kaggle_runtime_connecting` | Optional image/text targets for session state |

OCR reads visible English UI. It cannot recover hidden/off-screen DOM text,
HTTP response codes, or inaccessible cell IDs. Output reads and quota reads are
restricted to configured regions. Adapt the regions or templates when a website
changes. Colab and Kaggle account workflows still require a manual initial login.

## Browser lifecycle and server setup

Profiles run one at a time. Normal launch arguments select the user-data root,
internal profile, and a new blank window. There are no automation/debugging flags
or modifications to `navigator.webdriver`. The normal desktop password store and
keyring are preserved. Close Brave using the selected user-data root before
starting; live profiles are never edited or attached to.

The existing blank-startup preference preparation is retained: the selected
profile's startup preference becomes New Tab and pinned startup tabs are cleared,
while cookies and logins remain. The original Preferences file is backed up once
as `Preferences.before-zepuppeteer-startup`. On Linux the browser inherits the
desktop session environment and uses installed system GTK schemas when present.

For a server, provide an X11 desktop (or a dedicated Xvfb display), its clipboard
backend, and Tesseract. Simply setting DISPLAY does not create a display. Rebuild
the Python environment and install Brave on the target. Transfer ignored profile
configuration, browser data, VPN files, and `.env` separately from Git. Named
Brave profiles resolve in the server account's own Brave configuration directory.
Cookies may need a new manual login if the desktop keyring is unavailable or
machine-specific. Preserve Local State with transferred internal profile data,
and never copy browser data over a running or unrelated profile.

## Architecture

`brave.py` discovers Brave and constructs native launch commands. `screen.py`
owns the OS window, screenshots, recognition, clipboard input, and browser
process. Website adapters use these screen operations. Tasks return verified
`TaskResult` values; the runner owns sequential profiles, retries, timeouts,
cleanup, SQLite history, and failure evidence. VPN tools and profile transfer
utilities remain separate from the screen controller.

## Profiles and browser data

The recommended managed layout matches the import/export utilities:

```text
profiles/
  account-1/
    profile.toml               # Optional metadata
    src/                       # Brave user-data directory
      Local State              # Created by Brave or imported
      Default/                 # Or Profile N for an imported profile
  account-2/
    src/
```

After adding an account's information to `profiles/<id>/profile.toml`, initialize
its Brave profile and open it for manual login:

```bash
python -m zepuppeteer createProfile acc-5
# Preview paths without creating files or launching Brave:
python -m zepuppeteer createProfile acc-5 --dry-run
```

The command reads the existing TOML and preserves its contents. With
`profile.brave_browser_displayName`, it creates that named profile in the
installed Brave browser's user-data root if missing, or reopens the matching
profile if it already exists. Other Brave profiles and settings are preserved.
Without a display-name setting, it initializes or reopens the account's local
`src/` data. Existing flat browser data also keeps its original location.

Log in to the required websites in the browser, complete any MFA, then close the
browser to finish. Subsequent workflows using `--profile acc-5` reuse that browser
data. Websites can still expire sessions or require login again.

You can also pass a new account name. In that case, the command creates its folder,
a default `profile.toml` with enabled metadata and empty settings, and `src/` for
persistent Brave data. Add account-specific workflow settings to the generated
TOML before running workflows.

Use `--profiles-dir PATH`, `--executable PATH`, or `--no-install` as needed.
`create-profile` is an alias. This task requires a desktop display and a non-hidden
profile directory name. Existing account configuration and saved logins are
retained. Browser data stays in place if setup is interrupted or browser startup
fails. Run the same setup command again to finish logging in:

```bash
python -m zepuppeteer createProfile acc-5
```

For Python callers, `await createProfile("acc-5", profiles_dir="./profiles")`
from `zepuppeteer.tasks.createProfile` returns a `TaskResult` with the profile ID,
manifest path, and browser data directory after the browser closes. Profile
creation is a standalone setup task, performed before running workflows.

To open an existing account for manual work, run:

```bash
python -m zepuppeteer openProfile acc-5
# Preview the selected browser profile without opening Brave:
python -m zepuppeteer openProfile acc-5 --dry-run
```

`openProfile` opens only the selected account in visible Brave and waits without
a timeout until you close the browser manually. Cookies and saved logins are
retained. It uses the existing `profile.toml`, including
`profile.brave_browser_displayName` or `profile_directory`, and allows disabled
accounts for manual work. Close any Brave instance using the same browser data
before running it. The command requires a desktop display.

`open-profile` is an alias. Use `--profiles-dir PATH`, `--executable PATH`, or
`--no-install` as needed. It requires an existing account folder and does not
create profile metadata or register missing named Brave profiles; use
`createProfile` for initial setup. For Python callers,
`await openProfile("acc-5", profiles_dir="./profiles")` from
`zepuppeteer.tasks.openProfile` returns a `TaskResult` after manual closure.

An empty `src/` is initialized by Brave on first execution. A profile metadata
file can also designate a new managed profile before `src/` exists. Folder names
are stable identifiers: `account-1` remains the profile's ID even if its browser
display name changes.

An optional `profile.toml` uses this schema:

```toml
enabled = true
tags = ["reports", "team-a"]

# Set this only when selecting an existing internal browser profile.
# profile_directory = "Profile 2"

[settings]
timezone = "Asia/Tehran"

[settings.portal_a]
base_url = "https://example.com"
username_env = "PORTAL_A_USERNAME"
password_env = "PORTAL_A_PASSWORD"
```

Tasks access the settings through `ctx.profile.settings`. Environment variable
names are ordinary configuration: a task must resolve them explicitly, for
example with `os.environ[settings["password_env"]]`. The framework does not log in
to websites automatically. Listing profiles and printing a dry-run plan omit
settings contents. Other metadata is left untouched.

To open a named profile from the installed Brave browser on Ubuntu, set:

```toml
[profile]
brave_browser_displayName = "AI-acc5"
```

The runner matches that exact display name in
`~/.config/BraveSoftware/Brave-Browser/Local State` and launches the corresponding
internal directory from that user-data root. With this setting, `--profile acc-5`
uses the installed named profile, including its existing sessions; `acc-5/src`
is not used. Missing or duplicate display names are errors, so an unrelated or
new profile is never silently selected. Close Brave before running automation
against its user-data root. Do not combine this setting with `profile_directory`.

When `src/` contains a single `Default` or `Profile N` folder, discovery selects
that internal browser profile automatically. This includes imported profiles that
use names such as `Profile 2`. If several internal profiles exist, set
`profile_directory` explicitly; a configured internal directory must already
exist. Ambiguous flat-plus-src layouts and symlinked browser roots are rejected.

The folder ID and Brave's displayed name are different identifiers: for example,
`--profile acc-5` with the setting above can select the installed `Profile 2`,
whose displayed name is `AI-acc5`. `Profile 2` is Brave's internal directory,
not account number 2. Without a display-name setting, the managed local data
described above remains the default.
Use `brave://version` to check the active **Profile Path**, or Brave Settings →
**Profile name and icon** to check the displayed name. The launcher preserves
Brave's desktop keyring so imported cookies and saved sessions remain readable;
The native browser launch uses Brave's normal password store and keychain.

Two older layouts remain supported:

- **Flat isolated:** each immediate folder directly contains Brave's `Local State`
  and `Default`/`Profile N`, or is empty. Adding metadata to an initialized flat
  profile keeps its existing data location.
- **Native Brave root:** a root containing `Local State` is recognized automatically;
  its `Default` and `Profile N` folders become separate selectable profiles sharing
  that root. Pass the root, not an individual internal profile folder.

```bash
python -m zepuppeteer profiles --profiles-dir "$HOME/.config/BraveSoftware/Brave-Browser"
```

Use `--layout isolated` or `--layout native` to override root detection. Hidden and
symlinked profile folders are ignored. Discovery uses natural order, so
`account2` precedes `account10`. Close Brave instances using the selected data
before execution. The runner does not delete lock files or terminate unrelated
browsers. Native profiles share a data root and always execute sequentially.

## Groups and profile selection

`config/groups.toml` contains explicit profile directory names:

```toml
[groups]
demo = ["account-1", "account-2"]
reports = ["account-1", "account-3"]
```

The built-in group `all` selects all enabled profiles and does not require a group
configuration file. It is the default and its name is reserved. Named groups may
overlap. Unknown group names and missing group members fail before execution;
disabled members are excluded. Group declaration order does not override natural
profile discovery order.

```bash
python -m zepuppeteer profiles
python -m zepuppeteer profiles --group demo
python -m zepuppeteer profiles --include-disabled
python -m zepuppeteer run --workflow demo --group demo --profile account-1
python -m zepuppeteer run --workflow demo --tag reports --tag team-a
```

`--profile` and `--tag` are repeatable. Explicit names, group membership, and tags
are intersected; a profile must have every requested tag. An empty selection
produces an error instead of silently doing nothing. Use `--config-dir PATH` to
select another directory containing `groups.toml` and `workflows/`.

## Workflow configuration

A workflow name resolves to `config/workflows/<name>.toml`. You can also pass an
explicit TOML path with `--workflow ./somewhere/workflow.toml`.

```toml
name = "demo"
on_profile_failure = "continue"

[[steps]]
id = "submit"
task = "demo.fill_form"
timeout_seconds = 30
retries = 0

[steps.params]
prefix = "Hello "

[[steps]]
id = "verify"
task = "demo.verify_result"
input_from = "submit"
timeout_seconds = 10
```

| Field | Meaning | Default |
| --- | --- | --- |
| `name` | Workflow identifier | TOML filename stem |
| `on_profile_failure` | `continue` with the next profile, or `stop` the run | `continue` |
| `steps[].task` | Module below `zepuppeteer.tasks`, such as `demo.fill_form` | Required |
| `steps[].id` | Unique step identifier; required to distinguish repeated uses of one task | Task name |
| `steps[].params` | JSON-compatible task parameters | Empty table |
| `steps[].input_from` | Earlier step ID whose outputs become this step's inputs | No inputs |
| `steps[].timeout_seconds` | Positive timeout for each task attempt | `120` |
| `steps[].retries` | Additional attempts after the first failure | `0` |
| `steps[].retry_safe` | Explicit declaration that repeating this task is safe | `false` |
| `steps[].retry_delay_seconds` | Nonnegative delay between attempts | `1` |

Unknown workflow and step options are rejected to catch spelling mistakes.
Dependencies must point to earlier steps; execution follows the listed order.
Only the declared source's outputs are passed as inputs. Multiple steps may use
the same task with different IDs and parameters.

A retry declaration must include `retry_safe = true`. For example, a read-only
report lookup could use `retries = 2` and `retry_delay_seconds = 5`. For operations
that submit, publish, purchase, or upload, the task must verify the previous
outcome before repeating the action. The runner cannot infer whether a website
committed an operation before a connection failed. A timeout cancels the task's
Python coroutine; it cannot undo an action already accepted by a website.

## Google Colab task

Start from `examples/colab_profile.toml` for the full account and screen-target
configuration, then capture its icon PNGs from your own desktop.

Configure the account in its `profile.toml`:

```toml
[profile.platforms.colab]
login_username = "account@example.com"
notebook_url = "https://colab.research.google.com/drive/YOUR_NOTEBOOK_ID"
notebook_name = "your-notebook.ipynb"
auto_connect = true
use_vpn = false
job_start_timeout_seconds = 300
```

The task opens the notebook through the address bar, waits for its visible Runtime
menu and loaded OS window title, opens the configured Google account avatar, and
waits for the top email in the popup. The name must match exactly without ` - Colab`.
An unavailable account icon, unknown account, wrong title, or unrecognized runtime
requires attention. Existing runtime status is read only from its toolbar region.

When disconnected and `auto_connect=true`, the configured connection-options icon
opens the menu and OCR locates Connect to a hosted runtime. The task waits up to
300 seconds for connected indicators. An in-progress connection is waited for
without submitting another request. Following a new connection it clicks Run all
once and checks a configured final-cell executing indicator. Configure the
`colab_notebook_idle`, `colab_last_cell_anchor`, and `colab_last_cell_running`
targets, each with a tight region, before starting work. The controller requires
the idle signal and final-cell anchor before clicking and rechecks the final-cell
anchor afterward. Missing targets prevent Run all. It verifies startup rather
than eventual job completion.

An already connected runtime follows keepAlive task 5 without restarting the
job. That action requires `colab_first_cell_editor` and `colab_saved`. It copies
the focused editor through the clipboard, preserves source through the
`# Status update` comment, replaces the following status comment, re-reads the
editor to verify the change, and saves through File > Save. A missing marker
prevents edits. No `exit()` terminates the workflow runner.

The helper actions can save, open resources, insert/delete a scratch cell, or
update the status comment. Configure their targets from the table above before
using them. Visible output is OCR text; scrolling to the end does not read hidden
iframes or off-screen cell output. Both tasks save a verified screenshot and
JSON metadata before closing the tab. Outputs include the notebook title/URL,
verified account/runtime state, and `screenshot_path`/`metadata_path`. The runner
then closes Brave. Failure leaves the tab available for evidence collection.

With `use_vpn=true`, configure `[profile.vpn]` as below and install `xray` and
`curl`. The runner connects before Brave launches. WireGuard and V2Ray both use
a userspace Xray HTTP proxy without modifying host routes or DNS. A short-lived
loopback relay adds upstream authentication for the normal browser; credentials
are never passed in browser arguments. Heartbeats use the authenticated upstream
before launch, before Run all, and every 15 seconds during task execution. A
failed heartbeat cancels work. The browser closes before the relay and VPN.
QUIC and non-proxied WebRTC UDP are disabled while using that proxy.

## Kaggle task

Use `examples/kaggle_profile.toml` for account settings and screen targets.

```toml
[profile.platforms.kaggle]
login_username = "your-kaggle-username"
notebook_url = "https://www.kaggle.com/code/your-kaggle-username/your-notebook/edit"
notebook_name = "Your Notebook"
```

Configure `kaggle_account` for the account menu icon. The task opens the existing
editor, waits for visible Save Version text, follows the account menu's Your
Profile link, waits for navigation, and verifies the signed-in handle from the
address bar. It then returns to the notebook and checks the native window title
and visible session status. If the
status is inside a session menu, configure `kaggle_session_options` and
`kaggle_session_status`; it opens that menu to read the state and closes it with
Escape. Runtime states can also use the optional image targets in the table above.
Profiles without this platform table are skipped. The task does not log in,
start a session, execute cells, or save a notebook version. The browser tests
execute both site tasks against local notebook fixtures using real mouse clicks,
keyboard navigation, and OCR. They do not use signed-in live
service accounts.

## VPN connectivity task

`vpnCheck` accepts a profile directory name and tests an HTTPS request through
that profile's VPN without opening Brave:

```bash
python -m zepuppeteer vpnCheck acc-5
python -m zepuppeteer vpnCheck acc-5 --profiles-dir ./profiles --timeout 20
```

Add one of these configurations to `profiles/<account>/profile.toml`:

```toml
[profile.vpn]
vpn_protocol = "wireguard"
wireguard_file = "tunnel.conf"
```

```toml
[profile.vpn]
vpn_protocol = "v2ray"
v2ray_link = "vless://YOUR_ID@vpn.example.com:443?security=tls&type=ws&path=%2Fvpn"
```

The same keys directly under `[profile]` or at the top level are also accepted.
`wireguard_file` can be absolute or relative to the containing `profile.toml`.
The command exits with an error for missing tools, invalid settings, or failed
connectivity; it never installs software. Both protocols need `curl`. V2Ray
needs `xray` on PATH and supports VLESS, VMess, Trojan, and SIP002 Shadowsocks
share links. Supported transports are TCP/RAW, WebSocket, gRPC, HTTPUpgrade, and
XHTTP, with none/TLS/REALITY security as appropriate. Unsupported link formats,
TCP header obfuscation, Shadowsocks plugins, and links disabling TLS verification
are rejected. Xray validates the generated configuration before starting.

WireGuard needs Linux network namespaces, kernel WireGuard support, `wg`
(wireguard-tools), and `ip` (iproute2). A normal user also needs `sudo` access.
Terminal runs reuse cached authorization or show sudo's native password prompt
before creating any network resources. You can also run `sudo -v` in the same
terminal before retrying the command. Runs without a terminal never prompt and
need sudo authorization configured in advance. All subsequent privileged
commands use `sudo -n`.
Keys and peer endpoints are validated before sudo or network setup. Endpoint
hostnames are resolved using the host's DNS and passed to `wg` as numeric IPs;
DNS errors identify that prerequisite directly. If `wg setconf` fails, its
diagnostic is shown with WireGuard keys redacted.

The stripped WireGuard configuration is staged as a root-owned mode-600 file in
`/etc/wireguard`, which is allowed by Ubuntu's `wg` AppArmor policy. The task uses
the standard `mkdir`, `install`, and `rm` utilities for this. It removes the file
immediately after loading, and also attempts removal on failure or cancellation.

The task creates a temporary WireGuard interface, moves it into a disposable
namespace, and routes only the probe IP there. It leaves host routes and DNS
unchanged, ignores wg-quick shell hooks, and does not reuse existing VPN
interfaces. Configured ListenPort and FwMark are omitted to avoid host conflicts.
See WireGuard's [network namespace documentation](https://www.wireguard.com/netns/)
for the interface isolation mechanism.

The default probe is `https://1.1.1.1/cdn-cgi/trace`. Override it with `--url`;
WireGuard requires a numeric IP host covered by the configuration's AllowedIPs
and a matching IPv4/IPv6 interface address. This checks internet access through
the tunnel, so a private-network VPN may need its own reachable HTTPS endpoint.
The result prints total HTTPS request latency in milliseconds. WireGuard also
attempts three ICMP pings when `ping` is installed; blocked/missing ICMP does not
fail an otherwise successful HTTPS check. V2Ray reports HTTPS latency because
the SOCKS proxy does not carry ICMP ping.

Xray runs as a temporary process with an authenticated SOCKS listener on
127.0.0.1 and no direct outbound fallback. Its temporary configuration is private
and removed on exit. Temporary processes, interfaces, and namespaces are cleaned
up on success, failure, timeout, or cancellation. Credentials are not printed.
The generated configuration uses the official
[Xray transports](https://xtls.github.io/en/config/transport.html) and
[config validation command](https://github.com/XTLS/Xray-docs-next/blob/main/docs/en/document/command.md).

For Python callers, use `await vpnCheck("acc-5", profiles_dir="./profiles")`
from `zepuppeteer.tasks.vpnCheck`. It returns a `TaskResult` with `profile`,
`vpn_protocol`, `connected`, `latency_ms`, and optional `ping_ms`. Existing
browser workflows can include `task = "vpnCheck"`; the task reads the current
profile's manifest and accepts `timeout` and `url` in `[steps.params]`.

## Writing tasks and website adapters

Workflow modules define `async def run(ctx: TaskContext) -> TaskResult`. Use the
screen API exposed by `ctx.browser`:

```python
from zepuppeteer.models import TaskResult

async def run(ctx):
    page = await ctx.browser.new_page()
    await page.goto("https://example.com")
    await page.click_text("Name", region=(0, 0.1, 1, 0.7))
    await page.fill_text(ctx.profile.name)
    await page.click_target("submit", text="Apply")
    await page.wait_for_text("Saved")
    await page.screenshot(path=str(ctx.artifact_dir / "saved.png"))
    await page.close()
    return TaskResult(outputs={"saved": True})
```

`fill_text(text)` verifies a focused field through Select All and Copy after pasting.
Other operations include `type_text(text)` for insertion, `read_text(region)`, `words(region)`,
`wait_for_target(name)`, `hotkey("ctrl", "s")`, `press("enter")`, `scroll(-5)`,
`current_url()` (copies the address bar), and `title()` (native window title).
Ordinary single-line ASCII input uses real keystrokes; multiline/Unicode input
uses the clipboard. Clipboard text is restored after reads and pastes. Tasks must await a visible
result after an action before reporting success. A click alone is not proof.
Only one automation tab should be operated at a time. Do not change tabs or use
the desktop while an automated task is active.

Legacy files still define `async def run(context, profile)`. Run the converted
example with:

```bash
python -m zepuppeteer --profiles-dir ./profiles --task ./examples/demo_task.py
```

`context` is now a `ScreenBrowser`; existing custom Playwright tasks must be
ported to the operations above. The runner API is now
`run_profiles(profiles, executable, task=None)` or
`run_workflow(profiles, executable, workflow, tasks, state, ...)` with no driver
argument. `--headless` produces an explicit error.

## Failures, history, and artifacts

Workflow execution records run/profile/step status and JSON outputs in SQLite.
Declared retry-safe steps can retry within their configured limits;
`NeedsAttention` is not retried. Ctrl+C cancels the active work and closes the
browser before advancing. Browser launch and cleanup failures are recorded.

On failure the runner attempts window screenshots and `failure.json`. The
`trace.zip` filename is retained for compatibility, but now contains start/end
PNG screenshots and metadata. It is a screen evidence archive, not a Playwright
trace and cannot be opened in the Playwright trace viewer. `--no-trace` disables
these archives. Screenshots can contain private visible account/notebook data;
`artifacts/` stays ignored by Git.

`NeedsAttention` uses the existing best-effort Telegram notification when
configured via `.env` (`telegram_bot_token`, `telegram_chat_id`) or the overriding
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` environment variables. Credentials
are not included in error messages.

## Profile transfer and password utilities

The existing scripts remain available:

```bash
python scripts/password_enc.py
python scripts/password_dec.py

python scripts/import_brave_profile.py "Work account" account-1 --dry-run
python scripts/import_brave_profile.py "Work account" account-1
python scripts/export_brave_profile.py "Work account" account-1
```

Password tools prompt for their inputs and use the AES-GCM helpers in
`src/utils.py`. Import/export accepts the Brave display name followed by the
repository profile folder name. Profile paths are relative to this repository,
regardless of the shell's current directory. The destination layout is
`profiles/<name>/src`, and `profile.toml` is left untouched.

**Transfers move data by default.** After copying and verifying SHA-256 checksums,
they update `Local State` and remove the source folder. Close Brave instances
using either location. `--copy` preserves the source, `--replace` backs up an
existing matching destination, and `--dry-run` previews changes. Use
`--brave-dir PATH` to override Ubuntu's Brave root or `--profiles-dir PATH` to use
another repository profile root.

Replaced folders and metadata snapshots are retained for recovery. Transfer
errors during the commit attempt to restore both sides; the operation is not a
filesystem-wide atomic transaction. Other profiles and destination global
settings are preserved. Encrypted cookies and saved logins still depend on the
original system keyring; transfer does not decrypt or migrate keyring secrets.

## Validation and development

```bash
python -m unittest discover -s tests -v

# Opt in only on a disposable X11 desktop: tests move the mouse and type.
ZE_SCREEN_TESTS=1 python -m unittest discover -s tests -p "test_*screen_browser.py" -v
```

Unit coverage includes OCR coordinate conversion, template matching, duplicate
matches, stale positions, focus checks, normal process launch, profile storage,
workflow history, retries/cancellation, screen site actions, and VPN handling.
The integration tests use disposable profiles and local pages for form typing,
clicking, persistence, and failure screenshots. They never use real accounts.

Implementation references: [PyAutoGUI](https://pyautogui.readthedocs.io/en/latest/),
[MSS](https://python-mss.readthedocs.io/latest/), and
[pytesseract](https://github.com/madmaze/pytesseract).

## Command examples

Run these commands from the repository root after installing the project.
Replace `account-1` and `RUN_ID` with your profile directory name and run ID;
Colab, Kaggle, and VPN commands require the configuration described above.

```bash
# Show available commands and general usage.
python -m zepuppeteer --help

# Show all options for running a workflow.
python -m zepuppeteer run --help

# List enabled profiles as JSON without opening Brave.
python -m zepuppeteer profiles

# List every discovered profile, including disabled profiles.
python -m zepuppeteer profiles --include-disabled

# Create or reopen a profile for manual website login.
python -m zepuppeteer createProfile account-1

# Open an existing profile until you close its browser window.
python -m zepuppeteer openProfile account-1

# Check a profile's VPN connectivity without opening Brave.
python -m zepuppeteer vpnCheck account-1

# Preview the demo group's workflow without installing or launching Brave.
python -m zepuppeteer run --group demo --workflow demo --dry-run

# Run the local demo workflow on the demo group in a visible browser window.
python -m zepuppeteer run --group demo --workflow demo

# Run the configured Colab workflow for one enabled profile.
python -m zepuppeteer run --workflow colab --profile account-1

# Run the configured Kaggle workflow for one enabled profile.
python -m zepuppeteer run --workflow kaggle --profile account-1

# Show the ten most recent workflow runs.
python -m zepuppeteer history --limit 10

# Show profile and step details for a specific workflow run.
python -m zepuppeteer history --run-id RUN_ID

# Run the original single-file demo task on enabled profiles in visible windows.
python -m zepuppeteer --profiles-dir ./profiles --task ./examples/demo_task.py

# Preview importing a Brave profile without moving its data.
python scripts/import_brave_profile.py "Work account" account-1 --dry-run

# Run the automated test suite.
python -m unittest discover -s tests -v
```
