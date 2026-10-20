# Troubleshooting

Start with `calendar-bridge doctor` (read-only end-to-end checks) and `calendar-bridge status`.
With Docker, prefix commands with `docker compose exec calendar-bridge` while the service runs, or
`docker compose run --rm` when it is stopped.

Every message below is what the bridge prints or alerts; none of them ever contains event details.

## During setup

| Message | Cause and fix |
| --- | --- |
| `no encryption key configured` | Create it once: `docker compose run --rm keygen`. |
| `the encryption key file ... is a directory` | Docker creates an empty directory when the key file is missing (for example after `docker compose up` before `keygen`). `docker compose run --rm keygen` replaces it. |
| `the encryption key file ... is not readable by this process` | The service runs as uid 10001. Recreate the key with `keygen` on a fresh machine, or `sudo chown 10001:10001 secrets/calendar_bridge_key && sudo chmod 400 secrets/calendar_bridge_key`. |
| `this is a 'Web application' OAuth client` / `not an OAuth client file` | Create a client of type **Desktop app** ([setup](setup.md#1-google-create-your-oauth-client), step 7) and use its JSON. |
| `the Google Calendar API is not enabled in your Google Cloud project` | Open the link in the message (or [enable it here](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)) and run `setup` again. |
| Browser: *Access blocked: Calendar Bridge has not completed the Google verification process* | The app is still in *Testing* and your account is not a test user. Publish it (setup step 1.6). |
| Browser: *Google hasn't verified this app* | Expected for your own app: **Advanced → Go to Calendar Bridge**. |
| `access was not allowed on Google's consent page` | You cancelled; start again and choose Continue. |
| `Google did not grant every permission` | A box was unticked on the consent page. Start again and leave all of them ticked. |
| `that address belongs to an earlier attempt` / `Google refused the code` | Use the address from the current attempt, within a few minutes. |
| `Google did not return a refresh token` | Remove Calendar Bridge at <https://myaccount.google.com/permissions>, then run `setup` again. |
| `this is a different Google account (...) than the one already configured` | You signed in with another account. Sign in with the right one, or pass `--allow-account-change` to `auth google` if you really mean to switch. |
| `that is not an app-specific password, so nothing was sent to Apple` | Create one at <https://account.apple.com> (setup step 2). Your normal password is never accepted. |
| `iCloud rejected the Apple ID or app-specific password` | Check the Apple Account e-mail. App-specific passwords stop working when the Apple Account password changes: create a new one. |
| `port 8765 on 127.0.0.1 is in use by another program` | Only with `--listen`. Leave out `--listen` and paste the address instead, or set `CB_GOOGLE_OAUTH_PORT`. |
| `unknown setting CB_... (did you mean ...?)` | A misspelled variable in `.env`. Fix the name; valid names are in `.env.example`. |
| `... is not a whole number` / `is not true or false` | A setting has an invalid value in `.env` or `calendar-bridge.toml`. |
| `another synchronization is running; try again shortly` | The service is in the middle of a cycle. Try again in a minute. |

## While running

| Symptom or alert | Cause and fix |
| --- | --- |
| `Google access needs attention: Google refused the stored authorization (invalid_grant)` | Most often the Google app was left in *Testing* (authorizations expire after 7 days), access was removed at myaccount.google.com, or the password changed with "sign out everywhere". Publish the app if needed, then `setup`. Nothing is lost meanwhile. |
| `Google access needs attention: ... deleted_client` | The OAuth client was deleted in Google Cloud. Create a new Desktop client, run `setup`, answer *no* to "Use the same Google OAuth client as last time?" and give the new file. |
| `iCloud access needs attention` | The app-specific password was revoked or the Apple Account password changed. Create a new app-specific password and run `setup`. |
| `... access needs attention: stored credentials cannot be used: ...` | The encryption key changed or is missing. Put the original key back; if it is lost, run `setup` again (synchronization state is kept). |
| `No fully successful synchronization for N minutes` | A provider is unreachable or failing. `status` shows the read errors; `docker compose logs calendar-bridge` shows details. Nothing is lost: changes are caught up once both sides answer again. |
| `Deletions suspended for '...'` | Many deletions were requested at once. Check them with `items list --state pending`, then `safety release --mapping <id>` if they were intended ([operations](operations.md#safety-holds)). |
| `N unresolved conflict(s)` | The same detail changed differently on both sides. `conflicts list`, `conflicts show <id>`, then `conflicts resolve <id> --keep google` or `--keep icloud`. |
| `N event(s) could not be synchronized` | `items list --state failed` and `items show <id>` explain why. Failed events are retried automatically once a day, or now with `items retry <id>`. |
| An event is not copied at all | It may be older than the history window (365 days by default), or something the other side cannot hold: `items list --state unsupported` lists those with the reason ([semantics](semantics.md)). |
| A copied event is at the wrong hour | The original has no time zone (rare, some imported events). It is placed in your Google account's time zone as it was when your first calendar pair started. To use another zone, set `CB_DEFAULT_TIMEZONE` before setting up calendar pairs: changing it later re-times such events in Google, and their cancelled or moved occurrences may need to be redone there. |
| Container marked *unhealthy* | The service has not completed a cycle recently. `docker compose logs --tail 100 calendar-bridge`. |
| `unexpected error (...)` | A bug. Run the command again with `--log-level DEBUG` and [report it](https://github.com/Nicolas1bhr/calendar-bridge/issues) without private details. |

## Starting over

Your events live in Google and iCloud, not in the bridge. To start from scratch without losing
anything: `docker compose down -v` (removes the bridge's database), then `docker compose run --rm
calendar-bridge setup` again. The first sync pairs the existing copies instead of duplicating them.
