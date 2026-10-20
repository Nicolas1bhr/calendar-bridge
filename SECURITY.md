# Security policy

Calendar Bridge holds the keys to two calendars: who you meet, where and when. Reports about
anything that could expose that data, or make the bridge change or delete events it should not,
are taken seriously and handled first.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this repository.
Do not open a public issue for a vulnerability.

Include what you found, how to reproduce it and what an attacker (or a bug) could do with it.
Never include real tokens, passwords, encryption keys or calendar contents; describe them instead.

You can expect an acknowledgement within a week. Fixes are released as a new version, with a
changelog entry that credits you unless you prefer otherwise.

## What counts

- Disclosure of credentials, the encryption key or calendar content (including through logs,
  alerts, status files or error messages).
- Any way to make the bridge write, overwrite or delete events outside its mapped calendars, or
  delete events without real deletion evidence.
- Weaknesses in the container image, its dependencies or the release pipeline.

Data-loss bugs that are not security issues in the strict sense (for example a sync rule that
loses a field) are just as welcome: open a regular issue, or report them privately if they could
be triggered on purpose.

## Supported versions

Only the latest release receives fixes. The security model is described in
[docs/security.md](docs/security.md).
