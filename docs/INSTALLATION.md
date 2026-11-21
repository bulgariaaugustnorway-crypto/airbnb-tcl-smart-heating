# Installation and configuration

## Local development

Use Python 3.12. Create a virtual environment, activate it, and install `requirements-dev.lock` as shown in the README. Run `python run_demo.py` for the no-network walkthrough. Dependencies are downloaded from PyPI during installation; tests and the demo need no live services after installation.

The supported development command is `python -m pytest -q`, from the repository root. Do not run the tests inside a production checkout. Test fixtures use temporary directories and mocks; the test harness clears external integration environment variables, disables AWS metadata lookup, and blocks network access and subprocesses.

## Private settings

Copy `.env.example` to `.env` only if you need private local settings. `.env` is ignored by Git, but you should still inspect staged changes before committing. Process environment variables take precedence over `.env`. Set `HEATING_LOAD_ENV=false` to prevent loading that file. Set `HEATING_RUNTIME_DIR` to an isolated directory if you want runtime files outside the checkout.

| Setting | Public default | Purpose |
| --- | --- | --- |
| `TCL_DRY_RUN` | `true` | Prevent device writes from scheduling decisions |
| `TCL_READ_ONLY` | `true` | Prevent TCL shadow mutations |
| `TCL_LIVING_ROOM_ID`, `TCL_BEDROOM_ID` | synthetic aliases | Explicitly selected devices |
| `TCL_USERNAME`, `TCL_PASSWORD` | empty | Optional TCL account |
| `AIRBNB_ICAL_URL` | empty | Private iCalendar feed; never put it in Git |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Optional alert destination |
| `NOTIFICATION_WEBHOOK_URL` | empty | Optional webhook; may itself contain a secret |
| `S3_BUCKET`, `SSM_PARAMETER_PREFIX` | empty | Optional cloud persistence and secret source |
| `DASHBOARD_PASSWORD_HASH` | empty | PBKDF2-SHA256 password hash |
| `SESSION_SECRET_KEY` | empty | Independent random signing secret, minimum 32 characters |
| `MONITORING_ENABLED` | `false` | Private occupancy and room telemetry |
| `HEAT_DURING_BLOCKED_DATES` | `false` | Whether host blocks count as occupancy |
| `WEATHER_LATITUDE`, `WEATHER_LONGITUDE` | empty | Weather lookup enabled only when configured |

Other schedule and policy settings are documented in `src/config.py`. Many schedules are code-level settings rather than environment variables. Confirm the timezone, check-in/check-out times, safety temperatures, and room mapping for your own property.

## Dashboard secrets

Generate a signing secret locally:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Generate a supported password hash without putting the password in shell history:

```sh
python -c "from getpass import getpass; from src.dashboard.auth import hash_password; print(hash_password(getpass('Dashboard password: ')))"
```

Store the results privately. The dashboard returns HTTP 503 when its signing secret is absent or too short. A missing or unsupported password hash prevents login. Keep the dashboard behind HTTPS and trusted access controls. Installation does not start an HTTP server.

## Optional cloud and hardware integration

No automated cloud deployment is supplied. `lambda_handler.lambda_handler` is the automation adapter; `lambda_handler.dashboard_handler` is the dashboard adapter. Provisioning and securing any hosting environment is a separate task. `infra/build_artifact.py` can create a source-only review ZIP, but that ZIP does not vendor dependencies and is not a complete deployment package.

For your own future integration, use a separate development account and equipment you control. Keep both safety flags enabled, use short-lived operator credentials, select device IDs explicitly, scope permissions, and inspect the actual plan and logs before permitting writes. Never reuse the original production deployment configuration. No commands in this document change AWS resources.

Read-only and dry-run still permit reads, notifications, and persistence when those adapters are configured. A no-network requirement must use the offline demo or an external network boundary.
