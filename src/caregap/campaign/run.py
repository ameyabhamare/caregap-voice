"""Run a demo campaign end to end through n8n (``make run-campaign``).

    python -m caregap.campaign.run --size 20

1. Seeds synthetic consent (idempotent) and resets the Twilio mock's counters, so the
   mock's outcomes are reproducible.
2. Creates a campaign through the core service (retry delay shortened to 1 minute).
3. Triggers the n8n campaign-tick and notification workflows until the campaign is done.
   n8n's own schedules would get there too, once a minute.
4. Approves first-N holds (compliant scripts) as a stand-in for the human reviewer,
   recorded as ``run-campaign-demo``. Scripts that failed compliance are left for a person.
5. Prints the funnel, escalations and delivered notifications.

``--cover-outcomes`` scripts the mock so the calls cycle through every path (opt-out,
call-back request, retries ending in failure, a Twilio 503 retried by n8n) instead of
the mock's default distribution.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg

from caregap.campaign.consent import seed_consent
from caregap.config import get_settings

DEMO_REVIEWER = "run-campaign-demo"
#: Per-member mock outcomes for --cover-outcomes, assigned in turn to selected members.
COVER = [
    ["dtmf-9"],
    ["dtmf-0"],
    ["no-answer", "busy", "failed"],
    ["api-error", "completed"],
    ["no-answer", "completed"],
    ["completed"],
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--customer", default="sunrise", help="a config in customers/")
    ap.add_argument("--size", type=int, default=20)
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--core", default="http://localhost:8000")
    ap.add_argument("--n8n", default="http://localhost:5678")
    ap.add_argument("--mock", default="http://localhost:8100")
    ap.add_argument("--retry-delay-minutes", type=int, default=1)
    ap.add_argument("--timeout-minutes", type=float, default=45)
    ap.add_argument("--cover-outcomes", action="store_true", help="script every call path")
    args = ap.parse_args(argv)
    settings = get_settings()

    counts = seed_consent(settings.database_url)
    print(f"[run-campaign] consent seeded: {counts}")
    http = httpx.Client(timeout=60)
    http.post(f"{args.mock}/mock/reset").raise_for_status()
    health = http.get(f"{args.core}/health").json()
    print(f"[run-campaign] core: {health['provider']}/{health['model']}, clock {health['clock']}")

    _wait_for_webhooks(http, args.n8n)

    summary = _check(
        http.post(
            f"{args.core}/campaigns",
            json={
                "customer": args.customer,
                "name": f"demo {datetime.now(UTC):%Y-%m-%d %H:%M}",
                "size": args.size,
                "measurement_year": args.year,
                "retry_delay_minutes": args.retry_delay_minutes,
            },
        )
    )
    cid = summary["id"]
    print(
        f"[run-campaign] campaign {cid} ({summary['customer']}): {summary['members']} members, "
        f"{summary['states']}"
    )

    if args.cover_outcomes:
        _script_outcomes(http, args.mock, settings.database_url, cid)

    deadline = time.monotonic() + args.timeout_minutes * 60
    approved: list[int] = []
    last = ""
    while True:
        for review in _check(http.get(f"{args.core}/reviews")):
            if review["status"] == "pending_approval":
                _check(
                    http.post(
                        f"{args.core}/reviews/{review['id']}",
                        json={
                            "decision": "approve",
                            "reviewer": DEMO_REVIEWER,
                            "note": "first-N hold approved by the demo script",
                        },
                    )
                )
                approved.append(review["id"])
        summary = _check(http.get(f"{args.core}/campaigns/{cid}"))
        outbox = _check(http.get(f"{args.core}/notifications/outbox"))["items"]
        line = json.dumps(summary["states"], sort_keys=True)
        if line != last:
            print(f"[run-campaign] {time.strftime('%H:%M:%S')} {line}")
            last = line
        if summary["done"] and not outbox:
            break
        if time.monotonic() > deadline:
            print("[run-campaign] timed out; the campaign keeps running on n8n's schedule")
            return 1
        for hook in ("campaign-tick", "notify"):
            http.post(f"{args.n8n}/webhook/{hook}").raise_for_status()
        time.sleep(5)

    escalations = [
        e for e in _check(http.get(f"{args.core}/escalations")) if e["campaign_id"] == cid
    ]
    print("\n=== campaign", cid, "===")
    print(f"members selected:  {summary['members']}")
    print(f"skipped:           {summary['skipped'] or {}}")
    print(f"final states:      {summary['states']}")
    print(f"calls:             {summary['calls']} ({sum(summary['calls'].values())} placed)")
    print(f"human-reviewed:    {summary['human_reviewed']} "
          f"({len(approved)} first-N holds approved by {DEMO_REVIEWER})")  # fmt: skip
    print(f"escalations:       {summary['escalations'] or {}}")
    for e in escalations:
        print(f"  #{e['id']} {e['kind']}: {e['member_id']} - {e['detail']}")
    waiting = summary["states"].get("awaiting_review", 0)
    if waiting:
        print(f"waiting on people: {waiting} script(s) failed compliance and are in GET /reviews")
    return 0


def _script_outcomes(http: httpx.Client, mock: str, conninfo: str, campaign_id: int) -> None:
    from caregap.telephony.twilio import to_e164

    with psycopg.connect(conninfo) as conn:
        phones = [
            r[0]
            for r in conn.execute(
                "SELECT m.phone FROM campaign_members cm JOIN members m USING (member_id)"
                " WHERE cm.campaign_id = %s AND cm.state = 'selected' ORDER BY cm.member_id",
                (campaign_id,),
            )
        ]
    for i, phone in enumerate(phones):
        outcomes = COVER[i % len(COVER)]
        http.put(f"{mock}/mock/numbers/{to_e164(phone)}", json={"outcomes": outcomes})
    print(f"[run-campaign] scripted mock outcomes for {len(phones)} members")


def _wait_for_webhooks(http: httpx.Client, n8n: str, timeout_s: float = 120) -> None:
    """n8n reports healthy before it has registered the active workflows' webhooks."""
    deadline = time.monotonic() + timeout_s
    while http.post(f"{n8n}/webhook/notify").status_code == 404:
        if time.monotonic() > deadline:
            raise SystemExit("[run-campaign] n8n webhooks not registered; run `make n8n-import`")
        time.sleep(2)


def _check(r: httpx.Response) -> Any:
    if r.is_error:
        req = r.request
        raise SystemExit(f"[run-campaign] {req.method} {req.url}: {r.status_code} {r.text}")
    return r.json()


if __name__ == "__main__":
    sys.exit(main())
