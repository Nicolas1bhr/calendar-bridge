# Contributing

Thanks for helping. Calendar Bridge changes real people's calendars without supervision, so the
bar is correctness first.

## Principles

1. **Preserve user data and provider semantics.** When something cannot be synchronized safely or
   faithfully, keep the original, report the exact limitation, and never guess. An error is never
   evidence of a deletion.
2. **As simple as it can be while fully doing its job.** Prefer the smallest design that covers
   every real case. Do not cut corners on safety, validation or error messages to get there.
3. **Private by default.** Logs, alerts and errors carry identifiers and outcomes only: never
   event titles, descriptions, attendees, tokens or passwords.

## Development setup

Python 3.12 or newer.

```bash
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install --require-hashes --no-deps -r requirements-dev.lock
python -m pip install --no-deps -e .
```

## Checks

```bash
ruff check src tests && ruff format --check src tests && mypy
pytest                                    # unit, property, integration (real CalDAV server), recovery
HYPOTHESIS_PROFILE=deep pytest tests/property
```

The integration tests run a real CalDAV server (Radicale) and a stateful fake of the Google
Calendar API, and simulate crashes between a remote write and the local commit. Live tests
against real accounts are opt-in; see [docs/acceptance.md](docs/acceptance.md).

## Pull requests

- Add or adjust a test that fails without your change.
- Update the docs and add a line to `CHANGELOG.md` under *Unreleased* for anything a user would
  notice.
- Keep the scope to one change; explain the failure scenario it fixes or the case it adds.

## Dependencies

Dependencies are pinned with hashes. To change them, edit `pyproject.toml` and regenerate both
lock files with [uv](https://docs.astral.sh/uv/), only from releases at least two weeks old:

```bash
uv pip compile pyproject.toml --universal --generate-hashes --no-header \
  --exclude-newer "$(date -u -d '14 days ago' +%Y-%m-%d)" -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --generate-hashes --no-header \
  --exclude-newer "$(date -u -d '14 days ago' +%Y-%m-%d)" -o requirements-dev.lock
```

## License of contributions

Calendar Bridge is licensed under the GNU GPL, version 3 or later. By submitting a contribution, you
agree that it is distributed under the same license.

## Reporting bugs

Use the issue form. The output of `calendar-bridge doctor` and `calendar-bridge status --no-titles`
helps most. Remove anything private before posting, and report security issues privately as
described in [SECURITY.md](SECURITY.md).
