#!/usr/bin/env python3
"""Check that one or more URLs are up. Exits non-zero if any of them isn't.

Designed to run on a schedule (GitHub Actions cron, systemd timer, Task
Scheduler). A non-zero exit is what makes CI mark the run as failed, which is
what gets you an email -- so the exit code is the alerting mechanism.

    python uptime_check.py https://example.com
    python uptime_check.py https://a.com https://b.com --timeout 15
    python uptime_check.py --url-file urls.txt

Stdlib only.
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

UA = "uptime_check/1.0 (+scheduled monitor)"
RETRIES = 2          # a single blip shouldn't page you
RETRY_WAIT = 5.0


def check(url, timeout):
    """Return (ok, status, ms, note) for one URL."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Cache-Control": "no-cache"}
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ms = (time.time() - t0) * 1000
            ok = 200 <= r.status < 400
            return ok, r.status, ms, "" if ok else "unexpected status"
    except urllib.error.HTTPError as e:
        ms = (time.time() - t0) * 1000
        # 401/403 still proves the server is alive and answering.
        ok = e.code in (401, 403)
        return ok, e.code, ms, "auth-gated but responding" if ok else "http error"
    except urllib.error.URLError as e:
        return False, None, (time.time() - t0) * 1000, f"unreachable: {e.reason}"
    except (ssl.SSLError, TimeoutError) as e:
        return False, None, (time.time() - t0) * 1000, f"tls/timeout: {e}"
    except OSError as e:
        return False, None, (time.time() - t0) * 1000, f"network: {e}"


def check_with_retry(url, timeout, retries=RETRIES):
    for attempt in range(retries + 1):
        ok, status, ms, note = check(url, timeout)
        if ok:
            return ok, status, ms, note, attempt
        if attempt < retries:
            time.sleep(RETRY_WAIT)
    return ok, status, ms, note, retries


def main():
    p = argparse.ArgumentParser(description="Check URLs are up; non-zero exit if any is down.")
    p.add_argument("urls", nargs="*", help="URLs to check")
    p.add_argument("--url-file", help="file with one URL per line (# comments allowed)")
    p.add_argument("--timeout", type=float, default=20.0, help="seconds per request (default: 20)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = p.parse_args()

    urls = list(args.urls)
    if args.url_file and os.path.exists(args.url_file):
        with open(args.url_file) as f:
            urls += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not urls:
        urls = [u.strip() for u in os.environ.get("CHECK_URLS", "").split(",") if u.strip()]
    if not urls:
        sys.exit("no URLs given -- pass them as arguments, --url-file, or $CHECK_URLS")

    results, failures = [], 0
    for url in urls:
        ok, status, ms, note, attempts = check_with_retry(url, args.timeout)
        results.append({"url": url, "ok": ok, "status": status,
                        "ms": round(ms), "note": note, "attempts": attempts + 1})
        if not ok:
            failures += 1
        if not args.json:
            mark = "UP  " if ok else "DOWN"
            extra = f"  {note}" if note else ""
            retried = f"  (after {attempts} retries)" if attempts else ""
            print(f"{mark}  {status or '---':<4} {ms:6.0f}ms  {url}{extra}{retried}")

    if args.json:
        print(json.dumps({"checked": len(urls), "failures": failures,
                          "results": results}, indent=2))

    # GitHub Actions surfaces this in the run summary.
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"## Uptime check\n\n| status | code | time | url |\n|---|---|---|---|\n")
            for r in results:
                icon = "UP" if r["ok"] else "**DOWN**"
                f.write(f"| {icon} | {r['status'] or '-'} | {r['ms']}ms | {r['url']} |\n")

    if failures:
        print(f"\n{failures} of {len(urls)} check(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\nall {len(urls)} check(s) passed")


if __name__ == "__main__":
    main()
