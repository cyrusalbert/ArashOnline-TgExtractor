# V2Ray Telegram Scraper

Reads Telegram channels, extracts **valid** V2Ray links, tests every one through a real
proxy core, and publishes two lists: one sorted by **ping**, one by **download speed**.

```
channels.txt ─► scrape ─► extract + validate ─► dedupe ─► test ─► rename ─► output
                (t.me/s     vmess vless trojan    remark-    ping + geo    🇩🇪 DE | 85ms |    sorted_by_ping.txt
                 or          ss hy2 tuic          independent (phase 1)    12.4Mbps | #001    sorted_by_speed.txt
                 Telethon)                                    speed
                                                              (phase 2)
```

Two ways to run it, sharing the exact same code (`src/pipeline.py`):

| | GitHub Actions | Local PC |
|---|---|---|
| Trigger | cron every 3 h + manual button | you / Task Scheduler / cron |
| Entry point | `python -m src.pipeline` | `run_local.py` (`.bat` / `.sh`) |
| Config profile | `ci` | `local` |
| Ping/speed measured from | **the GitHub runner** (datacenter) | **your own ISP** |

> **Important:** results from the two modes are not comparable. A config that is fast from a
> GitHub datacenter can be slow or blocked from your home connection (and vice versa). If the
> lists are for *your own* use, the **local** results are the ones that reflect your real ping and speed.

---

## Repository layout

```
├── .github/workflows/run.yml   scheduled + manual workflow (commits results back)
├── channels.txt                channels to read, one per line
├── config.yaml                 every tunable setting (+ "ci" / "local" profiles)
├── requirements.txt
├── run_local.py                local entry point (adds --push)
├── run_local.bat / run_local.sh  create a venv, install deps, run run_local.py
├── scripts/telethon_session.py one-off helper to create a Telethon session (optional mode)
├── src/
│   ├── pipeline.py             shared core: runs all stages (used by both workflows)
│   ├── config.py               config.yaml + profile + env overrides
│   ├── scraper.py              t.me/s web preview, optional Telethon
│   ├── parser.py               extraction, strict validation, dedupe
│   ├── core_installer.py       downloads the right sing-box binary for your OS/CPU
│   ├── singbox.py              link -> sing-box outbound, core process manager
│   ├── tester.py               ping / geo / speed with worker pools
│   ├── geo.py                  exit-country lookup through the proxy
│   ├── renamer.py              remark rewriting that keeps links valid
│   ├── output.py               ranking, atomic file writing, optional git push
│   ├── models.py, logger.py
└── tests/test_core.py          offline unit tests
```

Generated files (committed by the workflow): `raw_links.txt`, `sorted_by_ping.txt`,
`sorted_by_speed.txt`, `sub_ping_base64.txt`, `sub_speed_base64.txt`, `results.json`.

---

## 1. GitHub Actions setup

1. Create a repository and push these files to it.
2. **Settings → Actions → General → Workflow permissions → "Read and write permissions"**
   (the workflow must be able to commit the result files).
3. Edit `channels.txt` - one channel per line (`name`, `@name`, or `https://t.me/name`).
   The channel needs its public web preview: open `https://t.me/s/<name>` in a browser to check.
4. **Actions tab → "Collect, test and publish V2Ray configs" → Run workflow.**
   After that it runs by itself every 3 hours (edit the `cron:` line in `run.yml` to change it).
   The manual form lets you override *max configs* and *messages per channel* for a single run.

Your subscription URLs will be:

```
https://raw.githubusercontent.com/<user>/<repo>/<branch>/sorted_by_ping.txt#TgExtractor-LowPing
```
```
https://raw.githubusercontent.com/<user>/<repo>/<branch>/sorted_by_speed.txt#TgExtractor-HighSpeed
```
```
https://raw.githubusercontent.com/<user>/<repo>/<branch>/sub_ping_base64.txt#TgExtractor-LowPing
```
```
https://raw.githubusercontent.com/<user>/<repo>/<branch>/sub_speed_base64.txt#TgExtractor-HighSpeed
```

Time limits: the job has a 60-minute hard stop, and the `ci` profile caps the test stage with
`testing.max_runtime_minutes: 35` and `testing.max_configs: 400`. When the budget runs out the
remaining configs are skipped and everything tested so far is still published.

### Optional: Telethon instead of the web preview (needs secrets)

The default web mode needs no login but only sees channels with a public preview and only the
newest ~100-300 messages. To read anything your Telegram account can see:

1. Get an `api_id` / `api_hash` at <https://my.telegram.org> → *API development tools*.
2. `pip install telethon`, then `python scripts/telethon_session.py` and log in. It prints a **session string**.
3. Add three repository secrets (**Settings → Secrets and variables → Actions**):
   `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`.

When `TELEGRAM_SESSION` exists the workflow installs Telethon and switches to `scraper.mode: both`
(web first, Telethon on top). The session string logs in **as your account**: treat it like a
password and prefer a spare account. Credentials are only ever read from the environment - they
are never written to files or logs.

---

## 2. Local PC

Requires Python 3.9+ (3.11+ recommended). The launcher creates `.venv`, installs the requirements,
and on first run downloads the sing-box binary for your OS/CPU into `bin/` (git-ignored).

**Windows**
```bat
run_local.bat
run_local.bat --push
```
**Linux / macOS**
```bash
chmod +x run_local.sh      # once
./run_local.sh
./run_local.sh --push
```
Or directly: `python run_local.py [options]`.

| Option | Effect |
|---|---|
| `--push` | after the run, `git add` + commit + push the result files (needs a configured remote and cached credentials / SSH key) |
| `--max-configs N` | test at most N configs |
| `--messages N` | read N messages per channel |
| `--no-speed` / `--no-geo` | skip that test |
| `--scrape-only` | only produce `raw_links.txt` |
| `--update-core` | re-download sing-box |
| `-v` | debug logging |

Local secrets (Telethon) go into a git-ignored `.env` file in the repo root:

```
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=abcdef...
TELEGRAM_SESSION=1BQA...
```

**If `t.me` is blocked on your network**, set `scraper.proxy` in `config.yaml` (an `http://` proxy works
out of the box; `socks5h://` needs `pip install "requests[socks]"`), or use Telethon through a system VPN.
Note this proxy is only for *reading Telegram* - configs are always tested over your direct connection.

### Scheduling the local run

**Windows Task Scheduler** - run once in a Command Prompt (adjust the path):

```bat
schtasks /Create /TN "V2Ray Scraper" /SC HOURLY /MO 3 /ST 00:10 ^
  /TR "\"C:\path\to\repo\run_local.bat\" --push"
```
or use the GUI: *Create Basic Task* → trigger *Daily*, repeat every 3 hours → action
*Start a program* → `C:\path\to\repo\run_local.bat`, arguments `--push`. (The script changes into its
own folder, so "Start in" is optional.)

**Linux / macOS cron** - `crontab -e`:

```cron
10 */3 * * * /path/to/repo/run_local.sh --push >> /path/to/repo/run.log 2>&1
```
For `--push` to work unattended, git must be able to authenticate without a prompt
(SSH key, or a credential helper / personal access token).

---

## 3. Configuration (`config.yaml`)

Everything is commented inside the file. The most useful keys:

| Key | Default | Meaning |
|---|---|---|
| `scraper.mode` | `web` | `web`, `telethon` or `both` |
| `scraper.messages_per_channel` | 100 | newest N messages per channel (profiles override) |
| `testing.max_configs` | 600 | cap per run, sampled round-robin across channels |
| `testing.concurrency` | 40 | parallel ping/geo tests (one core process each) |
| `testing.speed_concurrency` | 3 | parallel speed tests - keep low, they share your bandwidth |
| `testing.max_runtime_minutes` | 0 | time budget for the test stage (0 = none) |
| `testing.ping.*` | | URL (`generate_204`), attempts, timeout, `max_ping_ms` |
| `testing.speed.*` | | download URLs (Cloudflare first), `timeout`, `top_n`, `min_mbps` |
| `rename.template` | `{flag} {cc} \| {ping}ms \| {speed}Mbps \| #{rank:03d}` | remark format |
| `output.dir` | `.` | where files are written (repo root) |
| `output.top_n` | 0 | keep only the best N per list |

Any value can be overridden without editing the file: `V2S__<section>__<key>=value`, e.g.
`V2S__testing__max_configs=200` or `V2S__testing__speed__enabled=false`.
Profiles (`profiles.ci`, `profiles.local`) are overlaid on the base values; pick one with `--profile`
or `V2S_PROFILE`.

If you change `output.dir`, also update the file list in the *Commit updated files* step of `run.yml`.

---

## 4. How each stage works

1. **Scrape** - `t.me/s/<channel>` is fetched page by page (`?before=<id>`) until N messages are collected;
   retries with back-off, polite delays, rate-limit handling. One failing channel never stops the rest.
2. **Extract + validate** - links are found in plain text, HTML-escaped text (`&amp;`), `<code>`/`<pre>`
   blocks, anchors, and even glued together without spaces. Each candidate is strictly parsed:
   vmess base64-JSON, UUIDs, ports 1-65535, hosts must be public domains/IPs (no localhost / private ranges),
   Shadowsocks ciphers, REALITY keys, and so on. Anything malformed is dropped and counted in the log.
   All valid links go to `raw_links.txt`.
3. **Dedupe** - key = protocol + host + port + credentials + key transport params (network, security,
   SNI, host header, path, service name, flow, REALITY key, obfs, plugin). Remarks are ignored.
4. **Test** - each config is run in its **own sing-box process** as a local proxy, so a bad config can
   only fail its own test.
   - *Phase 1 (high concurrency):* ping = average of `attempts` requests to `generate_204` after an
     un-timed warm-up (a non-204 answer counts as failure); dead configs are dropped. Then the exit
     country is looked up through the proxy (ip-api, with fallbacks).
   - *Phase 2 (low concurrency):* download of a fixed-size file (Cloudflare's speed endpoint by default),
     in Mbps, for the `top_n` lowest-ping survivors. Timing starts at the first received byte; a download
     cut off by the timeout still yields a figure.
5. **Rename** - `🇩🇪 DE | 85ms | 12.4Mbps | #001`. vmess: JSON re-encoded; all other schemes: fragment
   replaced and percent-encoded. `#001` is the position **in the list the link is written to**, so the same
   server can be `#001` in one file and `#017` in the other.
6. **Output** - atomic writes. If *nothing* works (e.g. the runner has no network) the previous files are
   kept and the run exits non-zero, so a bad run cannot wipe a good list.

### Why sing-box instead of Xray-core?

sing-box handles all six protocols (vmess, vless, trojan, shadowsocks, **hysteria2, tuic**) in one
binary; Xray-core has no TUIC. Links using transports sing-box lacks (mKCP, QUIC, XHTTP/SplitHTTP, VLESS
post-quantum encryption, exotic SS plugins) are still *valid* and stay in `raw_links.txt`, but are reported as
`unsupported` and not tested. Requires sing-box ≥ 1.11.

---

## Troubleshooting

* **"No messages could be fetched"** - the channel has no web preview, the name is wrong, or `t.me` is
  blocked from where you run (see `scraper.proxy`, or use Telethon).
* **Everything is `dead` locally** - check that your ping URL is reachable directly, and that no firewall
  blocks the downloaded `bin/sing-box`. Run with `-v` to see core error messages.
* **Many `core-error`s** - usually an outdated sing-box (`--update-core`) or links with unusual options.
* **Repo history grows fast** - the workflow commits every 3 hours. Increase the cron interval, or
  periodically squash history if the size matters to you.
* **Tests:** `python -m unittest discover -s tests -v` (offline, no sing-box required).

## Responsible use

Only collect and publish configs you are entitled to share, and respect the terms of the channels you
read. Free public configs are operated by strangers: they can log or tamper with traffic. Use them for
low-sensitivity browsing only, never for banking or other credentials.
