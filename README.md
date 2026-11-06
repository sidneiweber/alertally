# Alertally

A web dashboard that brings alerts from several monitoring sources into one place.
Your team sees what is open, acknowledges alerts ("I'm on it") and follows reports
on the most frequent ones.

Supported sources:

- **GCP Cloud Monitoring** (`POST /gcp`)
- **Prometheus Alertmanager** (`POST /alertmanager`) — see [docs/alertmanager.md](docs/alertmanager.md)
- **AWS CloudWatch via SNS** (`POST /aws`) — see [docs/aws.md](docs/aws.md)

Built with Flask and MongoDB. Google sign-in, with the UI in English, Portuguese and
Spanish (see [docs/i18n.md](docs/i18n.md)).

## Features

- List of open alerts, sorted by severity. The alert counter refreshes every minute.
- **ACK**: shows who is handling an alert. Admins can remove other people's ACKs.
- **Close alert**: admins only. Alerts also close on their own when the source reports them as resolved.
- **Reports** (`/reports`): most frequent alerts and daily counts over the last 1, 7, 15, 30 or 60 days.
- Access control by email domain, email list or admin list.

## Running locally

You only need Docker with Compose.

```bash
docker compose -f docker-compose.dev.yaml up --build
```

- App at <http://localhost:5000>, **without Google sign-in** (fixed user `local@example.com`, admin).
- MongoDB at `localhost:27017`.
- The code is mounted into the container, so saving a file reloads the app.

A `.env` file is optional in dev mode. Use one to test webhooks with
authentication (`ALERTMANAGER_TOKEN`, `AWS_SNS_*`, `GCP_WEBHOOK_*`).

> **Fedora / SELinux:** the volume uses `.:/app:z` so the container can read the
> project folder. Without `:z`, the app fails with `Error: Could not import 'app'`.

### Sending a test alert

```bash
curl -X POST http://localhost:5000/alertmanager \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "4",
    "status": "firing",
    "alerts": [{
      "status": "firing",
      "fingerprint": "test-1",
      "labels": {"alertname": "DiskFull", "severity": "critical"},
      "annotations": {"summary": "Disk at 95%"}
    }]
  }'
```

### Without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pybabel compile -d translations
DISABLE_GOOGLE_LOGIN=true flask --app app run --debug
```

Requires MongoDB at `127.0.0.1:27017` (or set `MONGO_URL` / `MONGO_PORT`).

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q tests
```

Tests use `mongomock`, so no running MongoDB is needed. CI
(`.github/workflows/ci.yml`) runs the tests, compiles the translations and builds
the Docker image.

## Production

```bash
cp .env.example .env    # then fill it in
docker compose up -d --build
```

The app runs with gunicorn on port 5000. Put a reverse proxy with HTTPS in front
of it and set `SESSION_COOKIE_SECURE=true`.

### Configuration

Everything comes from environment variables (the `.env` file). See
[.env.example](.env.example) for the full list with comments.

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | Yes | Signs the session cookie. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `CLIENT_ID` / `CLIENT_SECRET` | Yes | Google OAuth. Redirect URI: `https://<your-host>/login/google/authorized`. |
| `ALLOWED_EMAIL_DOMAINS` | One of the three | Domains allowed to sign in (e.g. `example.com`). |
| `ALLOWED_EMAILS` | One of the three | Specific emails allowed to sign in. |
| `ADMIN_EMAILS` | One of the three | Admins: always allowed in, can close alerts and remove other people's ACKs. |
| `TIMEZONE` | No | Time zone for displayed times (default `UTC`). |
| `SESSION_COOKIE_SECURE` | No | `true` when served only over HTTPS. |
| `MONGO_URL` / `MONGO_PORT` | No | Where MongoDB is (Compose already points to `mongo`). |
| `GCP_WEBHOOK_USER` / `GCP_WEBHOOK_PASSWORD` | No | Basic auth for the `/gcp` webhook. |
| `ALERTMANAGER_TOKEN` | No | Bearer token required on `/alertmanager`. |
| `AWS_SNS_USER` / `AWS_SNS_PASSWORD` / `AWS_SNS_TOPIC_ARNS` | No | Basic auth and accepted topics for `/aws`. |
| `DISABLE_GOOGLE_LOGIN` | No | Local development only. **Never use in production.** |

Webhooks skip Google sign-in. Without their credentials, anyone who can reach the
URL can create alerts, so set them in production.

## Project layout

```
app.py            # creates the Flask app, registers blueprints, CSRF and i18n
config.py         # configuration read from the environment
docs/             # integration guides (Alertmanager, AWS) and i18n
routes/
  alerts.py       # alert list, ACK, close and webhooks (/gcp, /alertmanager, /aws)
  auth.py         # Google sign-in and logout
  main.py         # home page, reports and language switch
utils/            # Mongo access, session and i18n helpers
templates/        # HTML pages (Jinja)
static/           # CSS, JS and images
translations/     # pt_BR and es translations
tests/            # pytest + mongomock tests
```

## License

[MIT](LICENSE)
