# Airbnb TCL Smart Heating

Calendar-driven heating automation for TCL air conditioners, with a Python scheduling engine, independent room controls, guest override handling, and an owner dashboard.

This community project is an **unofficial integration**. It is not affiliated with Airbnb, TCL, Telegram, Amazon Web Services, OpenAI, or Google. The public edition starts with synthetic devices, dry-run and read-only safeguards, and no credentials or private calendars.

## What it does

- Parses iCalendar feeds and distinguishes reservations, imported channel bookings, and blocked dates.
- Plans pre-arrival heating, occupied day and night schedules, and checkout transitions.
- Handles consecutive stays, missing calendars, cancellation grace periods, manual overrides, and device isolation.
- Provides mock device control, optional TCL Cloud adapters, Telegram/webhook notifications, and local or S3 persistence.
- Includes a password-protected dashboard with signed sessions, CSRF protection, and rate limiting.

The calendar parser supports common exported feed patterns; it is not a complete RFC 5545 implementation. A calendar is an occupancy hint, not proof that a person is present.

## Quick start: offline simulation

Use Python **3.12** and Git. Clone this repository after it has been published, or open the prepared local copy:

```sh
git clone https://github.com/bulgariaaugustnorway-crypto/airbnb-tcl-smart-heating.git
cd airbnb-tcl-smart-heating
python -m venv .venv
```

Activate the environment:

```sh
# macOS / Linux
source .venv/bin/activate
```

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

Then install and run:

```sh
python -m pip install -r requirements-dev.lock
python run_demo.py
python -m pytest -q
python tools/scan_public.py
```

Dependency installation needs internet access. `requirements-dev.lock` pins the complete reviewed development environment; `requirements-dev.txt` lists its direct dependencies. The demo uses a synthetic calendar, mock devices, temporary storage, and a process-level network/subprocess guard. It ignores existing cloud credentials and local `.env` files. Tests also block unmocked network access and subprocesses.

## Configuration

For local experiments, copy `.env.example` to `.env` and keep both safety flags enabled:

```dotenv
TCL_DRY_RUN=true
TCL_READ_ONLY=true
```

The application loads `.env` relative to this repository unless `HEATING_LOAD_ENV=false`. It never loads the original production project's configuration. Real calendar URLs, TCL account credentials, AWS credentials, Telegram tokens, device IDs, and dashboard secrets belong in a private secret store or ignored local configuration.

**Dry-run and read-only prevent device writes; they do not prevent authentication, calendar downloads, notifications, telemetry writes, or other network traffic in the optional cloud adapters.** Use the offline demo for a no-network walkthrough. Review [installation and configuration](docs/INSTALLATION.md) and [security guidance](SECURITY.md) before supplying any real credentials.

## Architecture and examples

- [Architecture and trust boundaries](docs/ARCHITECTURE.md)
- [Installation and configuration](docs/INSTALLATION.md)
- [Examples and calendar fixtures](examples/README.md)
- [Security and vulnerability reporting](SECURITY.md)
- [Public edition changes](docs/PUBLIC_EDITION.md)
- [Contributing and verification](CONTRIBUTING.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

The Lambda entrypoints remain available as adapters. Infrastructure templates, automatic deployment commands, production operations scripts, and live equipment tests are deliberately absent from this edition. No hosted dashboard or cloud deployment is created by installation.

## Operational limits

Hardware behavior and cloud APIs vary by firmware, account region, and provider changes. The public candidate is tested with mocks; it has not been tested against live equipment as part of release preparation. Configure the property's timezone and schedules deliberately: the current scheduler uses `Europe/Sofia`. Weather lookup is disabled until coordinates are explicitly configured. Monitoring and heating during blocked dates are off by default.

This software is not an independent frost-protection or life-safety system. Keep manufacturer protections and a way to control heating manually. Runtime calendars, histories, logs, and state contain private occupancy information and must remain private.

## Acknowledgments

Developed and refined with assistance from **ChatGPT** and **Google Antigravity**. Human maintainers remain responsible for code review, security decisions, and operation.

The TCL Cloud adapter credits András Nemes's [ha-tcl-home-unofficial-integration](https://github.com/nemesa/ha-tcl-home-unofficial-integration). Its MIT notice is preserved in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT; see [LICENSE](LICENSE). Third-party components retain their own licenses and notices.
