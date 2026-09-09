# ICS Merger

A local, single-user service that merges remote ICS feeds, Google Calendar, and Microsoft 365
calendars. It serves a browser calendar, a deterministic ICS feed, and a settings UI.

## WARNING

This is pure AI slop that chatgpt threw together. I've done some tests to make sure it works for me. Not meant for others to user.

## Quick Start

Docker Compose is the supported runtime. Create the local configuration and storage directories:

```bash
cp config.example.yaml config.yaml
mkdir -p data secrets
chmod 700 data secrets
export ICS_MERGER_UID="$(id -u)"
export ICS_MERGER_GID="$(id -g)"
docker compose up --build -d
```

Open `http://localhost:8000` for the calendar or `http://localhost:8000/settings` to edit sources
and connect providers. Stop the service with `docker compose down`; OAuth tokens remain in `data/`.

Compose binds only to `127.0.0.1:8000`, runs as your numeric UID/GID, drops all capabilities, and
uses a read-only root filesystem. It mounts `config.yaml` read/write, `secrets/` read-only, and
`data/` read/write.

After changing `server.host` or `server.port` in the settings UI, restart the container:

```bash
docker compose restart
```

## Configuration

Configuration is strict YAML; unknown keys fail validation. See `config.example.yaml` for the full
schema.

- `server`: bind `host`, bind `port`, and externally visible `public_base_url` used for OAuth
  callbacks.
- `calendar`: provider `future_horizon_days`, from 1 through 366, and `include_free_time`.
- `remote_ics`: named feed `calendars`, private-network policy, response limit, and
   connect/read/write/pool timeout seconds.
- `env_file`: credential file required when Google or Microsoft is enabled.
- `storage`: owner-only plaintext JSON `token_file_path`.
- `google`: named `calendars`.
- `microsoft`: `tenant` and named `calendars`.

Every calendar needs a unique `name`. Remote entries use `url`; provider entries use `calendar_id`.
`ttl_seconds` controls background refresh and defaults to `3600`. Renaming a calendar changes its
generated event IDs.

Omit the entire `google` or `microsoft` section to disable that provider. When either section is
present, `env_file` is required. The selected file is loaded directly without reading or modifying
the process environment. Google uses `ICS_MERGER_GOOGLE_CLIENT_ID` and
`ICS_MERGER_GOOGLE_CLIENT_SECRET`; Microsoft uses `ICS_MERGER_MICROSOFT_CLIENT_ID` and
`ICS_MERGER_MICROSOFT_CLIENT_SECRET`.

Relative paths resolve from the directory containing `config.yaml`. Keep credentials in the
ignored, owner-only env file:

```bash
mkdir -p secrets
chmod 700 secrets
umask 077
cp secrets/providers.env.example secrets/providers.env
chmod 600 secrets/providers.env
```

Add only the variables for enabled providers. The file must be regular, non-symlinked, UTF-8,
no larger than 64 KiB, and mode `0600`. Never commit `config.yaml`, `secrets/`, `data/`, or private
feed URLs.

## Google Cloud Setup

1. Create or select a Google Cloud project and enable the Google Calendar API.
2. Configure the OAuth consent screen. Add the single user as a test user if the app remains in
   testing, and choose the appropriate internal or external audience.
3. Under **APIs & Services > Credentials**, create an **OAuth client ID** with application type
   **Web application**.
4. Register the exact authorized redirect URI
   `http://localhost:8000/api/auth/google/callback`. If `server.public_base_url` changes, register
   the exact resulting `{public_base_url}/api/auth/google/callback` value instead.
5. Add the client ID and secret to `secrets/providers.env`, enable Google in `/settings`, and sign
   in there.

The app requests only read-only calendar access and offline refresh access. OAuth uses authorization
code flow with PKCE S256 and one-time CSRF state.

## Microsoft Entra Setup

1. In the Microsoft Entra admin center, open **App registrations > New registration**. Select the
   supported account type that matches `microsoft.tenant`.
2. Under **Authentication**, add a **Web** redirect URI of
   `http://localhost:8000/api/auth/microsoft/callback`. If `server.public_base_url` changes,
   register the exact resulting `{public_base_url}/api/auth/microsoft/callback` value instead. Do
   not configure it as an SPA redirect and do not enable implicit grants.
3. Under **Certificates & secrets**, create a client secret and put the client ID and secret in the
   variables named above.
4. Under **API permissions**, add Microsoft Graph delegated `Calendars.Read` and `offline_access`.
   Grant consent if required by tenant policy.
5. Add the credentials to `secrets/providers.env`, enable Microsoft in `/settings`, and sign in
   there.

Microsoft integration uses Graph v1.0 `calendarView` for Microsoft 365 / Exchange Online.
On-premises Exchange and EWS are not supported.

## Endpoints

- `/` - seven-day browser calendar
- `/settings` - configuration and provider connections
- `/api/calendars/merged.ics` - merged calendar feed
- `/api/calendars/merged.json` - browser event data
- `/api/health` - health check
- `/api/docs` - OpenAPI documentation

Merged endpoints accept `source_label=none|title|description`. Sources refresh independently in the
background; requests use the latest snapshot and never wait for provider I/O. Failures are isolated
while another source succeeds and reported in `X-ICS-Merger-Source-Failures`.

Transparent source events are removed. Overlapping and adjacent busy or tentative events are
coalesced. When `calendar.include_free_time` is `true`, bounded gaps become transparent `Free Time`
events. Provider attendees and organizers are intentionally omitted. Out-of-office events are
anonymized and transparent.

Remote URLs are untrusted. Private, loopback, link-local, and other non-global destinations are
blocked unless `remote_ics.allow_private_networks` is `true`; use that opt-in only for trusted
self-hosted feeds. Redirects are rejected, response size and timeouts are bounded, and URLs or
payloads are not logged.

## Security and Limitations

The merged feed itself is unauthenticated. The default loopback binding is the current access model;
do not expose this service or feed beyond a trusted local boundary. Provider login endpoints are
also intended for that local user. Use HTTPS for any non-local `server.public_base_url`.

Provider tokens are plaintext JSON in `data/tokens.json`, protected by file permissions rather than
application encryption. OAuth transactions do not survive restarts. There is no multi-user,
multi-account, database, configurable filtering, or cross-provider deduplication support.

## Checks

```bash
docker run --rm -v "$PWD:/workspace" -w /workspace python:3.12-slim \
   sh -c "pip install -q -e '.[dev]' && pytest -q && ruff check . && mypy src"
docker compose config --quiet
docker compose build
```

