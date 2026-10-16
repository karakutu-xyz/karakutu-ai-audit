# Security policy

## Reporting a vulnerability

Report vulnerabilities **privately**. Do not open a public issue, pull request or discussion.

- Use GitHub's private vulnerability reporting ("Security" tab → "Report a vulnerability")
  on this repository.
- Include the affected version or commit, a description of the impact, and the smallest
  reproduction you have. Do not include real wallet keyfiles, tokens or archive data.

We aim to acknowledge a report within 5 working days and to agree a disclosure date with you
once a fix is available. Please give us a reasonable window before disclosing publicly.

## Supported versions

Only the latest release (currently 1.0.x) receives security fixes.

## Scope

In scope, with the reasons they matter here:

- **The Arweave wallet.** The service authenticates to the Karakutu archive with an RSA-4096
  wallet keyfile (`KARAKUTU_WALLET_PATH` or `KARAKUTU_WALLET_JSON`). The deployed wallet can
  write audit tags and post discussions. Anything that leaks key material (logs, error bodies,
  image layers, the repository) or lets an outsider act with the wallet's JWT is in scope.
- **API authentication.** `POST /api/v1/articles/{article_id}/audit` and the job endpoints
  need `Authorization: Bearer <API_AUTH_TOKEN>` (at least 32 characters, constant-time compare);
  with the token unset they answer 404 and no worker runs. A way around the token, a timing leak,
  or a way to force a write while `ARCHIVE_WRITES_ENABLED=false` is in scope.
- **Server-side request forgery.** The audit pipeline fetches URLs taken from archive records
  and caller-supplied content. Any way to make the service connect to loopback, private,
  link-local or cloud-metadata addresses (including through redirects or DNS) is in scope.
- Leaks of raw exception text, internal hostnames or archive payloads through API responses.
- The container image and `docker-compose.yml` defaults (running as root, published ports,
  secrets baked into layers).

Out of scope:

- Audit **verdict quality** (a wrong verdict is a bug or a calibration issue, not a security
  issue — open a normal issue).
- Findings that require an already-compromised host, a malicious operator with the wallet,
  or a deployment that ignores the documented defaults (for example exposing the API without
  `API_AUTH_TOKEN` or a reverse proxy).
- Third-party services (the Karakutu archive, SearXNG, search engines, LM Studio) — report
  those to their maintainers.

## Handling secrets as a contributor

- Never commit a wallet keyfile, `.env` file or token. `.gitignore` and the CI secret scan
  (gitleaks plus a filename check) are a safety net, not a substitute.
- If a key is ever committed, treat it as compromised: rotate it at the archive first, then
  clean the history.
