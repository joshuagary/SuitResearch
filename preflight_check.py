#!/usr/bin/env python3
"""
Browser-driven preflight test for the Palm Beach Clerk eCaseView search.  v0.1

Why a browser: the Case Type list is filled in by page scripts after you pick a Court Type, so a
plain HTTP script can't see it. This drives a real (headless) Chromium exactly like a person at the
keyboard: pick Court Type, wait for Case Type to load, pick Case Type, enter dates, click Search ONCE.

It also records every background (XHR/fetch) request the page makes. That list is the "Network tab"
you can't open on an iPad: it shows which endpoints loaded Case Type and ran the search.

Safety rules (built in)
  * one search per run; identifying `From:` header with your contact email
  * honours robots.txt
  * if a CAPTCHA/verification challenge appears, a login/MFA page appears, or the server blocks us
    (403/429/5xx), it STOPS. It never solves challenges, never uses stealth plugins, proxies or
    fingerprint tricks, and never retries.
  * saves only: request URLs/status codes, a short text snippet, and one screenshot of the results

Usage:
  python3 preflight_browser.py --contact you@example.com \
     --court-type "Circuit Civil" --case-type "CONTRACT & DEBT" \
     --file-begin 11/8/2026 --file-end 11/15/2026
Add --dry-run to fill the form but NOT click Search.
"""
import argparse
import csv
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone

BASE = "https://appsgp.mypalmbeachclerk.com/eCaseView/"
CHALLENGE_TEXT = re.compile(
    r"not a robot|verify (that )?you|captcha (failed|invalid|required)|invalid captcha|"
    r"complete the captcha|recaptcha (failed|validation)|access denied|unusual traffic|multi-?factor|verification code",
    re.I,
)


def display_date(s):
    """11/8/2026 or 2026-11-08 -> 'Nov 8, 2026' (the format shown on the page). Other text passes through."""
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            return f"{d.strftime('%b')} {d.day}, {d.year}"
        except ValueError:
            pass
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contact", required=True)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--court-type", default="Circuit Civil")
    ap.add_argument("--case-type", default="CONTRACT & DEBT")
    ap.add_argument("--file-begin", default="")
    ap.add_argument("--file-end", default="")
    ap.add_argument("--dry-run", action="store_true", help="fill the form, do not click Search")
    ap.add_argument("--timeout", type=int, default=30, help="seconds to wait for each step")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    log = []

    def record(kind, url, status, note=""):
        row = [datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, url, status, note]
        log.append(row)
        print(" | ".join(str(x) for x in row), flush=True)

    # robots.txt (plain request, once)
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(args.base))
    rp = None
    try:
        with urllib.request.urlopen(urllib.request.Request(origin + "/robots.txt", headers={"From": args.contact}), timeout=20) as r:
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(r.read().decode("utf-8", "replace").splitlines())
            record("robots.txt", origin + "/robots.txt", r.status)
    except Exception as e:
        record("robots.txt", origin + "/robots.txt", getattr(e, "code", "n/a"), "no robots rules found")
    if rp is not None and not rp.can_fetch("*", args.base):
        record("STOP", args.base, "-", "disallowed by robots.txt")
        return finish(log)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot = f"preflight_results_{stamp}.png"
    timeout = args.timeout * 1000

    with sync_playwright() as p:
        launch = {"headless": True}
        if os.environ.get("CHROME_PATH"):
            launch["executable_path"] = os.environ["CHROME_PATH"]
            launch["args"] = ["--no-sandbox"]
        browser = p.chromium.launch(**launch)
        ctx = browser.new_context(extra_http_headers={"From": args.contact}, viewport={"width": 1280, "height": 1600})
        page = ctx.new_page()
        page.set_default_timeout(timeout)

        stopped = {"v": None}

        def on_response(resp):
            try:
                req = resp.request
                u = resp.url
                if req.resource_type in ("xhr", "fetch") or "recaptcha" in u:
                    body_note = f"{req.method} {req.resource_type}"
                    if req.method == "POST" and req.post_data and "recaptcha" not in u:
                        body_note += f" post_fields={sorted(k for k in re.findall(r'([\\w\\[\\].-]+)=', req.post_data))[:25] or 'json/body'}"
                    record("xhr", u, resp.status, body_note)
                    if resp.status in (403, 429) or resp.status >= 500:
                        stopped["v"] = f"server returned {resp.status} to {u}"
            except Exception:
                pass

        page.on("response", on_response)

        def check_page(step):
            if stopped["v"]:
                record("STOP", page.url, "-", stopped["v"])
                return True
            txt = page.inner_text("body")[:6000]
            if CHALLENGE_TEXT.search(txt) or page.locator("iframe[src*='recaptcha/api2/bframe']").is_visible():
                record("STOP", page.url, "-", f"challenge/verification showing after step: {step}")
                return True
            if re.search(r"login|signin|mfa|challenge", page.url, re.I) and not page.url.startswith(args.base):
                record("STOP", page.url, "-", f"redirected to login/challenge after: {step}")
                return True
            return False

        try:
            resp = page.goto(args.base, wait_until="networkidle")
            record("landing", args.base, resp.status if resp else "-", f"title={page.title()!r}")
            if check_page("landing"):
                return finish(log)

            # Court Type
            court = page.get_by_label("Court Type", exact=True)
            court.select_option(label=args.court_type)
            record("step", page.url, "-", f"Court Type = {args.court_type}")
            page.wait_for_load_state("networkidle")

            # Case Type: wait until the dependent list contains our choice
            case = page.get_by_label("Case Type", exact=True)
            try:
                page.wait_for_function(
                    """([sel, text]) => {
                        const el = [...document.querySelectorAll('select')].find(s => (s.labels && s.labels[0] && s.labels[0].innerText.trim() === 'Case Type'));
                        return el && [...el.options].some(o => o.text.trim().toLowerCase() === text.toLowerCase());
                    }""",
                    arg=["Case Type", args.case_type],
                    timeout=timeout,
                )
            except PWTimeout:
                opts = case.locator("option").all_inner_texts()
                record("STOP", page.url, "-", f"'{args.case_type}' never appeared in Case Type. Options seen: {opts[:40]}")
                return finish(log)
            case.select_option(label=args.case_type)
            record("step", page.url, "-", f"Case Type = {args.case_type}")

            # Dates
            for label, val in (("File Begin Date", args.file_begin), ("File End Date", args.file_end)):
                if val:
                    field = page.get_by_label(label, exact=True)
                    field.fill(display_date(val))
                    field.press("Tab")
                    record("step", page.url, "-", f"{label} = {display_date(val)}")

            if check_page("form filled"):
                return finish(log)
            if args.dry_run:
                record("note", page.url, "-", "dry run: form filled, Search not clicked")
                page.screenshot(path=shot, full_page=True)
                return finish(log)

            # ONE search
            page.get_by_role("button", name="Search", exact=True).click()
            try:
                page.wait_for_load_state("networkidle", timeout=timeout)
            except PWTimeout:
                record("note", page.url, "-", "page still busy after wait; reading what is there")
            page.wait_for_timeout(1500)
            if check_page("search"):
                page.screenshot(path=shot, full_page=True)
                return finish(log)

            text = re.sub(r"\s+", " ", page.inner_text("body")).strip()
            record("snippet", page.url, "-", text[:1500])
            page.screenshot(path=shot, full_page=True)
            record("screenshot", shot, "-", "saved")
        except PWTimeout as e:
            record("STOP", page.url, "-", f"timed out: {str(e).splitlines()[0]}")
        except Exception as e:
            record("STOP", page.url, "-", f"error: {type(e).__name__}: {str(e).splitlines()[0]}")
        finally:
            browser.close()
    finish(log)


def finish(log):
    name = "preflight_browser_log_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
    with open(name, "w", newline="") as f:
        csv.writer(f).writerows([["time_utc", "kind", "url", "status", "note"]] + log)
    print(f"\nLog saved to {name}. If anything says STOP, do not retry or work around it.")


if __name__ == "__main__":
    sys.exit(main())
