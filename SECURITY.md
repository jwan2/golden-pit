# Security

Please report vulnerabilities privately via GitHub's "Report a vulnerability" (Security tab) rather than a public issue.

Deployment notes:
- Set `GOLDEN_PIT_ADMIN_TOKEN` and serve over HTTPS. Without a token, write actions are restricted to localhost.
- AI keys are read from environment variables only and never written to disk or logs.
