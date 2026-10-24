"""Disable the external cron-job.org trigger once provisioning succeeds."""

import json
import os
import sys
import urllib.error
import urllib.request


def disable_job(api_key, job_id, *, opener=urllib.request.urlopen):
    if not api_key:
        raise ValueError("The cron-job.org API key is missing")
    if not str(job_id).isdigit() or int(job_id) <= 0:
        raise ValueError("The cron-job.org job identifier must be a positive integer")
    request = urllib.request.Request(
        f"https://api.cron-job.org/jobs/{int(job_id)}",
        method="PATCH",
        data=json.dumps({"job": {"enabled": False}}).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with opener(request, timeout=20) as response:
        response.read()


def main():
    try:
        job_id = os.environ.get("CRONJOB_JOB_ID", "")
        disable_job(os.environ.get("CRONJOB_API_KEY", ""), job_id)
        print(json.dumps({"cron_job_disabled": int(job_id)}))
        return 0
    except urllib.error.HTTPError as exc:
        print(f"Could not disable the external cron job: HTTP {exc.code}", file=sys.stderr)
    except (ValueError, urllib.error.URLError) as exc:
        print(f"Could not disable the external cron job: {type(exc).__name__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
