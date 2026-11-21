# Contributing

Use Python 3.12 and a dedicated virtual environment. Install `requirements-dev.lock` and run the verification commands in `SECURITY.md` before proposing a change. Update the direct requirements files and regenerate the lock in a clean environment when changing dependencies. Keep behavior changes covered by tests that exercise useful outcomes, especially guest override preservation, stale calendars, authentication, and device write safeguards.

Tests must use mocks or in-memory data and run without AWS, TCL, Airbnb, weather, or Telegram access. The harness rejects network access and subprocess execution, including attempted access hidden by application exception handlers. Never add a live equipment test to the default test collection.

Use invented data and `.invalid` domains for new network fixtures. Keep credentials and private runtime data out of changes, issue reports, screenshots, and build artifacts. Test-only secret strings should be conspicuously synthetic. Explain any scanner finding and its disposition; do not silently add broad exclusions.

Public APIs and firmware behavior can change. Describe limitations honestly and do not label mock-based tests as live hardware verification. Keep third-party copyright notices intact.

The maintainers acknowledge assistance from ChatGPT and Google Antigravity. AI-assisted changes receive the same human review and verification as other contributions.
