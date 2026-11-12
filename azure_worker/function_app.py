import logging
import os
import urllib.error
import urllib.request

import azure.functions as func

app = func.FunctionApp()


@app.timer_trigger(
    schedule="0 */5 * * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def oppfindagent_scheduler(timer: func.TimerRequest) -> None:
    """Ask the production app to process one due user every five minutes."""
    if timer.past_due:
        logging.warning("OppFindAgent timer invocation is past due")

    endpoint = os.environ["OPPFINDAGENT_WORKER_URL"]
    secret = os.environ["OPPFINDAGENT_CRON_SECRET"]
    request = urllib.request.Request(
        endpoint,
        headers={"Authorization": f"Bearer {secret}", "User-Agent": "OppFindAgent-Azure-Scheduler/1.0"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Worker returned HTTP {response.status}: {body}")
            logging.info("OppFindAgent worker tick completed: %s", body)
    except urllib.error.HTTPError as exc:
        logging.exception("OppFindAgent worker returned HTTP %s", exc.code)
        raise
    except Exception:
        logging.exception("OppFindAgent worker invocation failed")
        raise
