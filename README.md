# hcloud-sniper

Polls the Hetzner Cloud API until a server type (e.g. `cx33`) becomes available
in one of your preferred locations, creates it **once**, and notifies you via Slack.

Stdlib-only Python, no dependencies.

## How it works

1. `GET /v1/server_types?name=<type>` — the type lists `locations[].available`.
2. First preferred location with the type available → `POST /v1/servers`.
3. `412 resource_unavailable` (lost the race) → retry; `429` → back off `HC_BACKOFF`.
4. On success: Slack message with id/IP, process exits 0.

## Polling budget

The rate limit is a bucket of 3600 requests that refills at 1 per second, per
project. That makes 1 s the hard floor for polling, with zero headroom left for
anything else using the same project.

| `HC_INTERVAL` | req/h | share of the budget |
| --- | --- | --- |
| 1 s | 3600 | 100% |
| 5 s (default) | 720 | 20% |
| 45 s | 80 | 2% |

One poll is one request, and the loop watches `RateLimit-Remaining`: below
`HC_RESERVE` it drops to 30 s polls until the bucket refills. A `429` sleeps
`HC_BACKOFF` seconds, not until `RateLimit-Reset` — that header marks *full*
recovery and can be an hour out, while a few seconds already buys new requests.

`GET /v1/datacenters` and `datacenter.server_types.available` are deprecated and
return `410 Gone` after 2026-10-01, hence the per-location fields on the server type.

## Setup

```bash
cp .env.example /etc/hcloud-sniper.env && chmod 600 /etc/hcloud-sniper.env
# fill in HCLOUD_TOKEN, SLACK_WEBHOOK, HC_SSH_KEYS

sudo mkdir -p /opt/hcloud-sniper && sudo cp sniper.py /opt/hcloud-sniper/
sudo cp systemd/hcloud-sniper.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hcloud-sniper
journalctl -u hcloud-sniper -f
```

Dry run first with an always-available type: `HC_TYPE=cx22 python3 sniper.py`, then delete the server.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Requires Python 3.11+ (tests use `unittest.TestCase.enterContext`).

## Notes

- Billing starts at creation; the service uses `Restart=on-failure`, so a successful
  exit (0) is never restarted.
- Without `HC_SSH_KEYS` the root password is posted to Slack — use a private channel or set keys.
