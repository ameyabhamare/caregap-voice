"""Live demo mode helpers (Phase 11; docs/live-demo.md). Run on the owner's computer.

    python -m caregap.campaign.live tunnel < cloudflared-logs   # make live-up
    python -m caregap.campaign.live call --customer sunrise     # make live-call
    python -m caregap.campaign.live recording --campaign 12     # make live-recording

``tunnel`` finds the quick tunnel's URL in cloudflared's logs, writes the webhook URL to
.env (LIVE_WEBHOOK_URL) and prints the URL to give the Twilio number for inbound calls.

``call`` creates a one-member live campaign and lets n8n run it. The script waits for a
person: it is shown here and the call is placed only if you approve it. The call goes to
LIVE_DEMO_NUMBER (the core service enforces it), recorded on Twilio.

``recording`` downloads the Twilio recording of a live campaign's call to docs/demo/.
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg

from caregap.campaign.consent import seed_consent
from caregap.config import Settings, get_settings
from caregap.customers import load_all
from caregap.telephony.twilio import basic_auth

TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
WEBHOOK_PATH = "/webhook/twilio-events"


def tunnel_url(logs: str) -> str | None:
    """The last quick-tunnel URL cloudflared printed."""
    found = TUNNEL_URL.findall(logs)
    return found[-1] if found else None


def set_env(path: Path, key: str, value: str) -> None:
    """Set ``key=value`` in a .env file, replacing an existing line or appending one."""
    lines = path.read_text().splitlines() if path.exists() else []
    out = [line for line in lines if not line.startswith(f"{key}=")]
    out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")


def download_recording(
    http: httpx.Client, settings: Settings, call_sid: str, dest: Path, *, attempts: int = 10,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:  # fmt: skip
    """Twilio finishes a recording a few seconds after the call; wait for it."""
    base = f"{settings.twilio_api_base_url.rstrip('/')}/2010-04-01/Accounts/" \
           f"{settings.twilio_account_sid}"  # fmt: skip
    auth = {"Authorization": basic_auth(settings.twilio_account_sid,
                                        settings.twilio_auth_token.get_secret_value())}  # fmt: skip
    for _ in range(attempts):
        r = http.get(f"{base}/Recordings.json", params={"CallSid": call_sid}, headers=auth)
        r.raise_for_status()
        recordings = r.json().get("recordings", [])
        if recordings:
            media = http.get(f"{base}/Recordings/{recordings[0]['sid']}.mp3", headers=auth)
            media.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(media.content)
            return dest
        sleep(3)
    raise SystemExit(f"[live] no recording for call {call_sid} yet; try again in a minute")


def _tunnel(_: argparse.Namespace) -> int:
    url = tunnel_url(sys.stdin.read())
    if url is None:
        print("[live] no trycloudflare.com URL in the tunnel logs yet", file=sys.stderr)
        return 1
    set_env(Path(".env"), "LIVE_WEBHOOK_URL", url + WEBHOOK_PATH)
    print(f"[live] LIVE_WEBHOOK_URL={url}{WEBHOOK_PATH} (written to .env)")
    for customer in sorted(load_all(get_settings().customers_dir)):
        hook = f"{url}{WEBHOOK_PATH}?event=inbound&customer={customer}"
        print(f"[live] inbound calls, {customer}: set the Twilio number's 'A call comes in' "
              f"webhook (HTTP POST) to {hook}")  # fmt: skip
    return 0


def _call(args: argparse.Namespace) -> int:
    settings = get_settings()
    http = httpx.Client(timeout=60)
    health = _check(http.get(f"{args.core}/health"))
    if not health.get("live"):
        raise SystemExit("[live] the core service is not in live mode (LIVE_MODE=true, "
                         "then make live-up)")  # fmt: skip
    seed_consent(settings.database_url)
    summary = _check(http.post(f"{args.core}/campaigns", json={
        "customer": args.customer, "name": f"live {datetime.now(UTC):%Y-%m-%d %H:%M}",
        "size": 1, "measurement_year": args.year,
    }))  # fmt: skip
    cid = summary["id"]
    print(f"[live] campaign {cid}: {summary['states']}; n8n prepares the script")
    deadline = time.monotonic() + args.timeout_minutes * 60
    decided: set[int] = set()
    while time.monotonic() < deadline:
        for review in _check(http.get(f"{args.core}/reviews")):
            if review["id"] in decided or review.get("member_id") not in _members(settings, cid):
                continue
            decided.add(review["id"])
            print(f"\n[live] script {review['id']} ({review['status']}):\n"
                  + textwrap.fill(review["script"], 88, initial_indent="  ",
                                  subsequent_indent="  "))  # fmt: skip
            ok = input(f"[live] call {settings.live_demo_number} with this message? [y/N] ")
            decision = "approve" if ok.strip().lower() == "y" else "reject"
            _check(http.post(f"{args.core}/reviews/{review['id']}", json={
                "decision": decision, "reviewer": args.reviewer, "note": "live demo"}))  # fmt: skip
        summary = _check(http.get(f"{args.core}/campaigns/{cid}"))
        if summary["done"]:
            print(f"[live] campaign {cid} done: {summary['states']}, calls {summary['calls']}")
            print(f"[live] recording: make live-recording CAMPAIGN={cid}")
            return 0
        http.post(f"{args.n8n}/webhook/campaign-tick")
        time.sleep(5)
    print("[live] timed out; n8n keeps running the campaign on its schedule")
    return 1


def _members(settings: Settings, campaign_id: int) -> set[str]:
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute(
            "SELECT member_id FROM campaign_members WHERE campaign_id = %s", (campaign_id,)
        ).fetchall()
    return {r[0] for r in rows}


def _recording(args: argparse.Namespace) -> int:
    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            "SELECT a.call_sid FROM call_attempts a JOIN campaigns k ON k.id = a.campaign_id"
            " WHERE k.id = %s AND k.live AND a.call_sid IS NOT NULL ORDER BY a.id DESC LIMIT 1",
            (args.campaign,),
        ).fetchone()
    if row is None:
        raise SystemExit(f"[live] campaign {args.campaign} has no live call")
    dest = Path(args.out or f"docs/demo/live-call-campaign-{args.campaign}.mp3")
    path = download_recording(httpx.Client(timeout=60), settings, row[0], dest)
    print(f"[live] recording saved to {path} (synthetic script; Twilio's trial notice first)")
    return 0


def _check(r: httpx.Response) -> Any:
    if r.is_error:
        raise SystemExit(f"[live] {r.request.method} {r.request.url}: {r.status_code} {r.text}")
    return r.json()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m caregap.campaign.live")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("tunnel").set_defaults(run=_tunnel)
    c = sub.add_parser("call")
    c.add_argument("--customer", default="sunrise")
    c.add_argument("--year", type=int, default=2025)
    c.add_argument("--reviewer", default="owner")
    c.add_argument("--core", default="http://localhost:8000")
    c.add_argument("--n8n", default="http://localhost:5678")
    c.add_argument("--timeout-minutes", type=float, default=20)
    c.set_defaults(run=_call)
    r = sub.add_parser("recording")
    r.add_argument("--campaign", type=int, required=True)
    r.add_argument("--out")
    r.set_defaults(run=_recording)
    args = ap.parse_args(argv)
    result: int = args.run(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
