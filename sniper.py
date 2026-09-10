#!/usr/bin/env python3
"""Hetzner Cloud sniper: waits until a server type is available and creates it once.

Stdlib only. Configuration via environment variables (see .env.example).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.hetzner.cloud/v1"


def env_list(name, default):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def pick_location(available, preferred):
    """Return the first preferred location that is available, else None."""
    return next((loc for loc in preferred if loc in available), None)


class Sniper:
    def __init__(self):
        self.token = os.environ["HCLOUD_TOKEN"]
        self.server_type = os.environ.get("HC_TYPE", "cx33")
        self.locations = env_list("HC_LOCATIONS", "fsn1,nbg1,hel1")
        self.name = os.environ.get("HC_NAME", "cx33-node")
        self.image = os.environ.get("HC_IMAGE", "debian-13")
        self.ssh_keys = env_list("HC_SSH_KEYS", "")
        self.interval = int(os.environ.get("HC_INTERVAL", "45"))
        self.slack_webhook = os.environ.get("SLACK_WEBHOOK")

    # --- HTTP -------------------------------------------------------------
    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body else None
        r = urllib.request.Request(
            f"{API}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(r, timeout=20) as resp:
            return json.load(resp)

    def notify(self, text):
        print(text, flush=True)
        if not self.slack_webhook:
            return
        try:
            r = urllib.request.Request(
                self.slack_webhook, data=json.dumps({"text": text}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(r, timeout=10).read()
        except Exception as e:  # never let notify kill the sniper
            print("slack notify failed:", e, flush=True)

    # --- Hetzner ----------------------------------------------------------
    def available_locations(self):
        """Location names where the configured type can currently be ordered.

        Source of truth is server_type.locations[].available; the old
        /datacenters endpoint is removed after 2026-10-01 (HTTP 410).
        """
        q = urllib.parse.quote(self.server_type)
        types = self.req("GET", f"/server_types?name={q}")["server_types"]
        if not types:
            sys.exit(f"unknown server type {self.server_type}")
        return [loc["name"] for loc in types[0]["locations"] if loc["available"]]

    def create(self, location):
        body = {
            "name": self.name, "server_type": self.server_type, "image": self.image,
            "location": location, "ssh_keys": self.ssh_keys,
            "public_net": {"enable_ipv4": True, "enable_ipv6": True},
        }
        return self.req("POST", "/servers", body)

    # --- loop -------------------------------------------------------------
    def run(self):
        self.notify(f":hourglass: sniper started for `{self.server_type}` "
                    f"in {self.locations}, interval {self.interval}s")
        fails = 0
        while True:
            try:
                avail = self.available_locations()
                hit = pick_location(avail, self.locations)
                print(time.strftime("%H:%M:%S"), "available in:", avail or "-", flush=True)
                if hit:
                    res = self.create(hit)
                    srv = res["server"]
                    ip = srv["public_net"]["ipv4"]["ip"]
                    msg = (f":white_check_mark: *CREATED* `{srv['name']}` ({self.server_type}) "
                           f"in `{hit}` — id {srv['id']}, ipv4 `{ip}`")
                    if not self.ssh_keys:
                        msg += f"\nroot password: `{res.get('root_password')}`"
                    self.notify(msg)
                    return 0
                fails = 0  # only a fully clean pass clears the error counter
            except urllib.error.HTTPError as e:
                try:
                    err = json.load(e).get("error", {})
                except Exception:
                    err = {}
                if err.get("code") == "resource_unavailable":
                    print("race lost, retrying", flush=True)
                elif e.code == 429:
                    print("rate limited, backing off", flush=True)
                    time.sleep(300)
                else:
                    fails += 1
                    print("HTTP", e.code, err, flush=True)
                    if fails == 5:  # persistent error: likely bad config, tell once
                        self.notify(f":x: sniper: 5 consecutive HTTP {e.code}: "
                                    f"{err.get('message')}")
            except Exception as e:  # network blip etc.
                print("ERR", e, flush=True)
            time.sleep(self.interval)


if __name__ == "__main__":
    sys.exit(Sniper().run())
