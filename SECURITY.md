# Security policy

## Scope

This is an unofficial automation project. The public edition defaults to simulated devices, dry-run and read-only device control, and unconfigured external integrations. Tests use mocks; no live cloud or physical safety certification is implied.

## Reporting a vulnerability

Do not include tokens, credentials, calendar URLs, guest details, device identifiers, or deployment metadata in public issues. After publication, use GitHub's **Report a vulnerability** feature if the maintainer has enabled private vulnerability reporting. Otherwise, ask for a private contact channel without disclosing the vulnerability or sensitive data publicly. No private reporting channel is provisioned by this source release.

Maintainers should enable private vulnerability reporting and repository secret scanning before accepting security reports. Fixes will target the current main branch; there is no promised response time or supported legacy release branch.

## Secrets and guest privacy

- Keep TCL credentials, temporary AWS credentials, Telegram tokens, webhook URLs, session keys, and password hashes in a private secret store.
- Treat the complete calendar URL as a password, including its path and query string.
- Never commit `.env`, account IDs, physical device IDs, personal contact details, reservation exports, room histories, cache/state, logs, or deployment artifacts.
- Runtime state is intentionally useful to the scheduler and can contain raw reservation identifiers and summaries. Truncated identifiers in some log messages are not anonymization. Delete or minimize guest details before sharing diagnostics.
- Limit access and retention for any storage, logs, notifications, and dashboard. Use TLS and scoped permissions for deployments you provision.

## Safeguards

Keep `TCL_DRY_RUN=true` and `TCL_READ_ONLY=true` until you have independently reviewed the behavior for your equipment. These flags govern device writes, not all network traffic. The offline demo and test harness enforce a separate no-network boundary.

There is no default dashboard signing key. Configure a random `SESSION_SECRET_KEY` of at least 32 characters and a supported PBKDF2-SHA256 password hash. Plaintext password fallback is disabled. Local login throttling is not a substitute for distributed rate limiting or perimeter access control.

The calendar parser supports common exports, not every recurrence or timezone feature of RFC 5545. An unreliable calendar, provider outage, software defect, or device malfunction can affect heating. Keep manual control and independent building protections available.

## If something is exposed

Privately revoke or rotate the affected credential or feed link, invalidate dashboard sessions by changing the signing secret, review access logs, and assess guest-data exposure. Removing a file from Git does not remove old commits, forks, releases, caches, or downloaded copies. These actions require the account owner's authorization and are not performed by the release-preparation tools.

## Verification

```sh
python tools/scan_public.py
python -m pytest -q
python -m bandit -r src lambda_handler.py -ll
python -m pip_audit -r requirements.txt
python -m pip_audit -r requirements-dev.lock
```

Dependency audits contact a public advisory service and need internet access. Static scanning and known-vulnerability databases cannot prove the absence of secrets or vulnerabilities. Inspect all staged files and the complete fresh Git history before publication.
