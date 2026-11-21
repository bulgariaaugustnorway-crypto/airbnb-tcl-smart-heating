# Public edition scope

The public edition is built from an explicit source allowlist and starts a new local Git history. It does not clone or retain the original `.git` directory, remotes, author identities, branches, tags, or hooks.

Included: application source, unit and mocked integration tests, synthetic calendars, an offline demo, a source-only artifact builder, safe configuration examples, and new public documentation.

Excluded: production environment files, credentials, original runtime calendar/cache/state, reservation and telemetry exports, logs, response captures, deployment configuration, live stack templates, operations/emergency scripts, secret-upload scripts, live equipment verification scripts, original build artifacts, bytecode, virtual environments, and historical private documentation.

Physical device identifiers and deployment-specific fixture identifiers are replaced. Calendar examples use new synthetic UIDs and generic summaries; they contain no original guest names, descriptions, URLs, or contact details. Test tokens and AWS credentials are conspicuous test-only strings. Public provider service URLs and shared TCL application protocol constants remain where needed by the unofficial adapter.

Security changes include synthetic device defaults, an unconfigured signing secret, removal of password-derived/fixed signing-key fallbacks and plaintext dashboard login, whole-calendar-URL redaction, sanitized TCL HTTP failures, trusted gateway IP selection, unconfigured weather coordinates, monitoring off by default, and blocked-date heating off by default.

Tests for omitted production secret-upload and live-delivery scripts are excluded with those scripts. Existing runtime regression tests remain, with explicit synthetic settings where safer defaults differ. New tests cover release packaging, configuration, authentication and log redaction. No original tests are executed against the production project.

No cloud resources, GitHub repository, remote, release tag, or publication are created by these files. Review the local release audit separately before approving any future publication.
