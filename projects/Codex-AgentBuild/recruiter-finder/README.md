# Recruiter Finder

Find personal LinkedIn hiring/referral posts, verify their public text and author metadata, and alert only for recent relevant roles in configured locations. LinkedIn Jobs pages, company job listings, and external job boards never qualify.

## Commands

From this directory:

```sh
cp config.example.json config.json
.venv/bin/recruiter setup       # guided private credentials + authorized live tests
.venv/bin/recruiter start       # activate after setup confirms phone receipt
.venv/bin/recruiter stop
.venv/bin/recruiter scan        # three live API queries; alerts if enabled
.venv/bin/recruiter status      # service, search diagnostics, counts, usage
.venv/bin/recruiter matches
.venv/bin/recruiter logs
.venv/bin/recruiter test-sms    # asks before sending a real test
.venv/bin/recruiter dashboard  # read-only localhost dashboard
.venv/bin/python -m pytest -q
```

You can also open `Setup.command`. Setup stores credentials with hidden prompts in Git-ignored `.env` (mode 0600). Never paste secrets into chat. It opens SerpApi's API-key dashboard and Twilio Console, lets you choose Apple Messages instead, and never purchases numbers or upgrades accounts. Rerun setup to change provider.

## Discovery and verification

Three search requests per scan use the same SerpApi account. Current configuration combines Google and DuckDuckGo searches, including a date-filtered query. The previous Google `site:` requests returned unrelated job-board links during live testing, and short Google date filters also excluded observed LinkedIn results. The working Google search therefore uses `inurl:` without that date filter. **Neither engine's displayed date establishes freshness.** Coverage depends on search indexing and is not comprehensive.

Only original LinkedIn `/posts/` or `/feed/update/urn:li:activity:` URLs enter the candidate store. Regional LinkedIn hosts normalize to `www.linkedin.com`; activity IDs deduplicate results. IDs supply a preliminary derived date, allowing old candidates to be rejected cheaply. Google Jobs cards and LinkedIn Jobs URLs are ignored.

For eligible-age candidates, the app performs an ordinary unauthenticated request to the public post URL and reads `SocialMediaPosting` JSON-LD. It requires matching post identity, full `articleBody`, a timezone-qualified `datePublished`, and an author identified as `Person` with a LinkedIn `/in/` profile. Organization-authored posts are excluded. Unavailable pages, access challenges, login redirects, or missing evidence go to review; no sign-in, cookies, authenticated scraping, or access-control bypass is used. Public fetches are capped at ten per scan; unavailable metadata is not retried for twelve hours. This verifies what LinkedIn's public metadata says about the profile, not a person's real-world identity.

Alert classification uses the post body rather than surrounding comments, linked Jobs cards, or a truncated search excerpt. It checks publication within 72 hours, role/hiring relevance, salary preference, and location. Company and other facts are omitted unless reliably extracted; notifications include the verified poster, post excerpt, age, score, and original URL. Missing salary does not disqualify a role.

Location is mandatory: only roles matching configured locations can qualify. Unknown or conflicting arrangements go to review without alerts. Rules are conservative heuristics, not perfect natural-language understanding; mixed-role posts and implicit geographic restrictions can remain ambiguous. Generic first-person phrases are insufficient without public author/text verification in the production configuration.

Copy `config.example.json` to `config.json` before setup. The personal
configuration is Git-ignored; edit it for roles, skills, company preferences,
salary preference, threshold, queries, per-query `engine`/`date_filter`, API
budget, notification cap, and digest. Configure notification destinations
locally and keep credentials in the Git-ignored `.env`. Default monthly ceiling
is 200 API attempts. Failed requests, manual scans, and diagnostics count
conservatively. This limit covers this app only, not your provider account's
other clients or dollar charges. Three requests twice daily consume up to 186
attempts in a 31-day month before retries/diagnostics.

## Notifications

The selected provider is visible in `status`. Apple Messages needs a signed-in iMessage account and macOS Automation permission for the invoking process. Use System Settings > Privacy & Security > Automation if a request was denied. Self-addressed iMessages can sync silently; verify receipt on the locked iPhone. AppleScript submission alone cannot prove delivery. Test background operation separately from Terminal operation.

Twilio needs Account SID/Auth Token from Console > Account Dashboard and an SMS-capable sender from Phone Numbers > Manage > Active Numbers. Trial recipients require verification. Complete applicable US A2P 10DLC registration or toll-free verification in Console. See [Twilio trial setup](https://www.twilio.com/docs/usage/tutorials/how-to-use-your-free-trial-account). Provider acceptance/queued status is not delivery; setup asks you to confirm receipt before authorizing unattended alerts.

Up to five distinct posts per notification pass are sent. Optional digest combines those posts. There is no monthly SMS cap, and long alerts can span multiple billable segments. Confirmed submissions never resend. Explicit rate-limit rejection retries after fifteen minutes, up to five attempts. Ambiguous failures become `uncertain`; interrupted sends remain `sending` for manual provider-history review rather than duplicate sends. Exactly-once delivery across SQLite and a provider cannot be guaranteed. Twilio delivery status is reconciled on background ticks.

## Scheduling and persistence

The LaunchAgent `~/Library/LaunchAgents/local.recruiter-finder.plist` invokes the isolated virtualenv every five minutes. It calculates 08:00/17:00 due slots in America/New_York, including daylight saving time. These ticks are not new search scans. SQLite remembers completed slots. Wake catch-up scans once for the latest overdue slot; failed scans retry after thirty minutes within the API cap. Pending notifications and Twilio status can be processed between scans. VS Code need not stay open. The agent operates while logged in and awake, including with the screen locked. Logout/shutdown stops it.

Sleep prevents normal scheduled execution. In macOS Energy or Battery > Options, enable the available setting to prevent automatic sleep on power adapter with the display off. Keep the Mac plugged in; lid closure can still suspend a laptop. A temporary `caffeinate -i` prevents idle sleep until Ctrl-C. No permanent power settings are changed. Grant Documents access to background Python if macOS requests it.

`state/finder.sqlite` stores candidates, public evidence, rejection status, usage, and scan slots. `status` includes per-query result counts, accepted LinkedIn URL counts, search IDs, and warnings. These distinguish unavailable discovery from posts rejected for age/location/person evidence. Sanitized errors omit exception text that could contain secrets. Application logs rotate at 500KB with three backups. Preserve the database to preserve deduplication. A process lock serializes searches and sends.

## Validation

Unit tests mock external search, public-post HTTP, notification providers, and launchctl. They verify public author/body/date extraction, company exclusion, age/location/relevance rules, duplicates, quotas, retry behavior, private credentials, and scheduler configuration. They do not prove live phone delivery or Automation permissions.

Diagnostic evidence is stored locally under ignored `state/`. Live testing confirmed that Google `inurl:` returns actual LinkedIn posts and that a public post exposes author/body/date metadata. No qualifying live end-to-end phone alert has yet been verified. Do not treat a loaded scheduler as evidence that every stage has passed.

Installed dependency versions are recorded in `requirements.lock.txt`. For a fresh install, create a Python 3.11+ virtualenv and run `.venv/bin/python -m pip install -e '.[test]'`.
