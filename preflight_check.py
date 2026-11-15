#!/usr/bin/env python3
"""
Gentle preflight check for a public court-records site (e.g. Palm Beach Clerk eCaseView).

What it does (and nothing more):
  1. Fetches robots.txt and reports whether your URLs are allowed.
  2. Fetches the landing page once and looks for CAPTCHA / MFA markers.
  3. Optionally requests a SHORT list of URLs you supply (max 10), slowly,
     and records status codes. It does NOT parse cases or download documents.

It STOPS immediately on 403 / 429 / 5xx, a challenge page, or a redirect to a login/MFA page.
It does not rotate IPs, spoof browsers, or try to get around any control.

Usage:
  python3 preflight_check.py --contact you@example.com
  python3 preflight_check.py --contact you@example.com --urls urls.txt --delay 8

urls.txt = one URL per line, copied from the browser's Network tab during a normal manual search.
Run from your own computer. Python 3 standard library only.
"""
import argparse
import csv
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone

BASE = "https://appsgp.mypalmbeachclerk.com/eCaseView/"
MAX_URLS = 10
MARKERS = {
    "recaptcha": re.compile(r"recaptcha|g-recaptcha|grecaptcha", re.I),
    "captcha": re.compile(r"captcha", re.I),
    "mfa": re.compile(r"multi-?factor|\bmfa\b|verification code|one-time", re.I),
    "login": re.compile(r"type=[\"']password[\"']|sign[ -]?in|log[ -]?in", re.I),
}


def fetch(url, ua, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(500_000).decode("utf-8", errors="replace")
            return r.status, r.geturl(), body
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception as e:  # network error
        return 0, url, f"ERROR: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contact", required=True, help="your email, included in the User-Agent")
    ap.add_argument("--urls", help="file with up to 10 URLs to request, one per line")
    ap.add_argument("--delay", type=float, default=8.0, help="seconds between requests (min 5)")
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()

    delay = max(args.delay, 5.0)
    ua = f"newsroom-research-preflight/0.1 (+mailto:{args.contact})"
    parts = urllib.parse.urlsplit(args.base)
    origin = f"{parts.scheme}://{parts.netloc}"
    log = []

    def record(kind, url, status, note=""):
        row = [datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, url, status, note]
        log.append(row)
        print(" | ".join(str(x) for x in row))

    # 1. robots.txt
    rp = urllib.robotparser.RobotFileParser()
    status, _, body = fetch(origin + "/robots.txt", ua)
    record("robots.txt", origin + "/robots.txt", status)
    if status == 200:
        rp.parse(body.splitlines())
    else:
        rp = None  # no robots.txt found; terms of use still apply

    def allowed(u):
        return True if rp is None else rp.can_fetch(ua, u)

    stop = False

    def check_block(status, final_url, body, requested):
        nonlocal stop
        reason = None
        if status in (403, 429) or status >= 500 or status == 0:
            reason = f"status {status}"
        elif urllib.parse.urlsplit(final_url).path != urllib.parse.urlsplit(requested).path and re.search(
            r"login|signin|mfa|captcha|challenge", final_url, re.I
        ):
            reason = f"redirected to {final_url}"
        elif MARKERS["recaptcha"].search(body) and re.search(r"verify|not a robot|challenge", body, re.I):
            reason = "CAPTCHA challenge text present"
        if reason:
            stop = True
            record("STOP", requested, status, reason)
        return reason

    # 2. landing page, once
    if not allowed(args.base):
        record("STOP", args.base, "-", "disallowed by robots.txt")
        return finish(log)
    time.sleep(2)
    status, final_url, body = fetch(args.base, ua)
    found = [k for k, rx in MARKERS.items() if rx.search(body)]
    record("landing", args.base, status, "markers: " + (",".join(found) or "none"))
    if check_block(status, final_url, body, args.base):
        return finish(log)

    # 3. optional short URL list
    if args.urls:
        with open(args.urls) as f:
            urls = [l.strip() for l in f if l.strip() and not l.startswith("#")][:MAX_URLS]
        for u in urls:
            if not allowed(u):
                record("skip", u, "-", "disallowed by robots.txt")
                continue
            time.sleep(delay)
            status, final_url, body = fetch(u, ua)
            record("request", u, status, f"{len(body)} bytes")
            if check_block(status, final_url, body, u):
                break

    finish(log)


def finish(log):
    name = "preflight_log_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
    with open(name, "w", newline="") as f:
        csv.writer(f).writerows([["time_utc", "kind", "url", "status", "note"]] + log)
    print(f"\nLog saved to {name}. If anything says STOP, do not retry or work around it.")


if __name__ == "__main__":
    sys.exit(main())
