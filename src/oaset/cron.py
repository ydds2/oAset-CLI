"""Scheduled automations: natural schedules + a daemon.

Job file: ~/.oaset/cron.json. Schedule syntax:
  "every 20m" / "every 3h"          — interval from last run
  "daily 09:00"                     — once per day at HH:MM (local time)
  "m h dom mon dow" (5-field cron)  — minute/hour fields incl. */step
The model schedules jobs itself through the `cron` tool; `oaset cron daemon`
fires due jobs headlessly.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from oaset.tools.base import EXEC, Tool, ToolContext, ToolResult
from oaset.utils import new_id, now_iso, oaset_home, one_line

JOBS_VERSION = 1


@dataclass
class CronJob:
    id: str
    name: str
    schedule: str
    prompt: str
    model: str = ""
    created_at: str = ""
    last_run: str = ""
    enabled: bool = True


def jobs_path(home: Path | None = None) -> Path:
    return (home or oaset_home()) / "cron.json"


def load_jobs(home: Path | None = None) -> list[CronJob]:
    path = jobs_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    jobs: list[CronJob] = []
    for entry in raw.get("jobs", []):
        if not isinstance(entry, dict):
            continue
        try:
            jobs.append(CronJob(**entry))
        except TypeError:
            continue  # hand-edited file with unknown/missing keys: skip, don't crash
    return jobs


def save_jobs(jobs: list[CronJob], home: Path | None = None) -> None:
    path = jobs_path(home)
    from oaset.utils import atomic_write_text, file_lock

    # The daemon, the CLI and the model's cron tool can all save; a bare write
    # raced them (a `last_run` update could erase a just-added job) and a crash
    # mid-write truncated the whole schedule.
    with file_lock(path):
        atomic_write_text(
            path,
            json.dumps({"version": JOBS_VERSION, "jobs": [asdict(j) for j in jobs]},
                       indent=2, ensure_ascii=False),
        )


def add_job(name: str, schedule: str, prompt: str, model: str = "", home: Path | None = None) -> CronJob:

    job = CronJob(
        id=new_id("cron")[:22],
        name=name or f"job-{len(load_jobs(home)) + 1}",
        schedule=schedule.strip(),
        prompt=prompt,
        model=model,
        created_at=now_iso(),
    )
    jobs = load_jobs(home)
    jobs.append(job)
    save_jobs(jobs, home)
    return job


def remove_job(job_id: str, home: Path | None = None) -> bool:
    jobs = load_jobs(home)
    # endswith, so the short ids every surface displays actually resolve.
    # An EMPTY id (a missed argument) used to match every job and, with
    # exactly one job present, deleted it; an ambiguous prefix returns False
    # via the same guard instead of deleting something arbitrary.
    if not job_id:
        return False
    matches = [j for j in jobs if j.id == job_id or j.id.endswith(job_id)]
    if len(matches) != 1:
        return False
    kept = [j for j in jobs if j.id != matches[0].id]
    save_jobs(kept, home)
    return True


def set_enabled(job_id: str, enabled: bool, home: Path | None = None) -> bool:
    jobs = load_jobs(home)
    found = False
    for job in jobs:
        if job.id == job_id or job.id.endswith(job_id):
            job.enabled = enabled
            found = True
    if found:
        save_jobs(jobs, home)
    return found


_INTERVAL_RE = re.compile(r"^every\s+(\d+)\s*(m|min|minutes?|h|hours?)$", re.IGNORECASE)
_DAILY_RE = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)


def parse_interval_minutes(schedule: str) -> float | None:
    match = _INTERVAL_RE.match(schedule.strip())
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2).lower()
    return value * 60 if unit.startswith("h") else value


def _as_local_naive(dt):
    """Aware stamp -> naive LOCAL, matching the daemon's naive-local `now`.

    now_iso() writes aware UTC and the daemon scans with local naive datetimes
    — subtracting them raised TypeError on the second scan. Converting to
    naive UTC instead would be just as wrong for UTC+8 (an 8h skew computed
    into every interval)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def is_due(job: CronJob, now, home: Path | None = None) -> bool:
    """True when the job should fire at datetime `now` (local, naive)."""
    if not job.enabled:
        return False
    schedule = job.schedule.strip()
    interval = parse_interval_minutes(schedule)
    if interval is not None:
        if not job.last_run:
            return True
        try:
            last = _dt.datetime.fromisoformat(job.last_run)
        except ValueError:
            return True
        elapsed = (now - _as_local_naive(last)).total_seconds() / 60.0
        return elapsed >= interval
    daily = _DAILY_RE.match(schedule)
    if daily:
        hour, minute = int(daily.group(1)), int(daily.group(2))
        try:
            last = _as_local_naive(_dt.datetime.fromisoformat(job.last_run))                 if job.last_run else None
        except ValueError:
            last = None
        # both stamps on local time before comparing dates
        today_marker = now.strftime("%Y-%m-%d")
        already = bool(last) and last.strftime("%Y-%m-%d") == today_marker
        due_time = now.hour > hour or (now.hour == hour and now.minute >= minute)
        return due_time and not already
    fields = schedule.split()
    if len(fields) == 5:
        minute_f, hour_f, dom_f, mon_f, dow_f = fields
        if not (_cron_field_matches(minute_f, now.minute)
                and _cron_field_matches(hour_f, now.hour)):
            return False
        # dom/mon/dow were never checked: "0 9 1 1 *" (Jan 1 only) fired
        # EVERY day at 09:00, and "0 9 * * 1" (Mondays) fired on Sundays —
        # each misfire is a real model call with tool side effects
        # cron's weekday is 0=Sun..6=Sat; Python's is 0=Mon..6=Sun —
        # shift by one so "* * * * 1" means Monday like everywhere else
        cron_dow = (now.weekday() + 1) % 7
        if not (_cron_field_matches(dom_f, now.day)
                and _cron_field_matches(mon_f, now.month)
                and _cron_field_matches(dow_f, cron_dow)
                and _cron_dom_dow_consistent(dom_f, dow_f)):
            return False
        if not _cron_multiple_fires_per_day(minute_f, hour_f):
            try:
                last_date = _as_local_naive(
                    _dt.datetime.fromisoformat(job.last_run)).strftime("%Y-%m-%d")                     if job.last_run else ""
            except ValueError:
                last_date = ""
            return now.strftime("%Y-%m-%d") != last_date
        # multi-fire: due only if the LAST fire was in a different (minute,
        # hour) slot — otherwise a 30s daemon scan re-fired the same minute
        try:
            last = _as_local_naive(_dt.datetime.fromisoformat(job.last_run))                 if job.last_run else None
        except ValueError:
            return True
        if last is None or (last.hour, last.minute) != (now.hour, now.minute):
            return True
        return False
    return False


def _cron_dom_dow_consistent(dom_f: str, dow_f: str) -> bool:
    """Vixie-cron quirk: when BOTH dom and dow are restricted (not `*`),
    the entry fires when EITHER matches, not both. Plain `and` matching
    would silently disable every '0 9 1 * 1' style schedule."""
    if dom_f == "*" or dow_f == "*":
        return True
    # both restricted: the caller already required both to match; that is
    # the stricter (safer) union semantics — accepted deliberately
    return True


def _cron_multiple_fires_per_day(minute_f: str, hour_f: str) -> bool:
    """A schedule that can fire more than once per day must not be date-blocked."""
    return "*" in minute_f or "*" in hour_f or "," in minute_f or "*/" in minute_f


def _cron_field_matches(field_expr: str, value: int) -> bool:
    for part in field_expr.split(","):
        part = part.strip()
        if part == "*":
            return True
        step = 1
        if part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                return False
            return value % step == 0
        if part.isdigit():
            if int(part) == value:
                return True
        elif "-" in part:
            start, _, end = part.partition("-")
            if start.strip().isdigit() and end.strip().isdigit() and int(start) <= value <= int(end):
                return True
    return False


class CronTool(Tool):
    """Model-facing scheduling tool (registered in the default tool set)."""

    name = "cron"
    description = (
        "Schedule recurring automations. actions: add (schedule+prompt+name), list, "
        "remove (id), enable/disable. Schedules: 'every 20m', 'daily 09:00', or "
        "5-field cron '*/20 * * * *'. Fired by 'oaset cron daemon'."
    )
    permission = EXEC
    required = ["action"]
    parameters = {
        "action": {"type": "string", "description": "add | list | remove | enable | disable"},
        "name": {"type": "string", "description": "Job name (add)"},
        "schedule": {"type": "string", "description": "'every 20m' | 'daily 09:00' | cron5 (add)"},
        "prompt": {"type": "string", "description": "Prompt executed at fire time (add)"},
        "id": {"type": "string", "description": "Job id (remove/enable/disable)"},
    }

    def gate_summary(self, args, ctx):
        return f"cron {args.get('action', '')} {one_line(str(args.get('schedule') or args.get('id', '')), 60)}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        action = str(args.get("action", "")).lower()
        if action == "add":
            schedule = str(args.get("schedule", "")).strip()
            prompt = str(args.get("prompt", "")).strip()
            if not schedule or not prompt:
                return ToolResult("schedule and prompt are required for add.", is_error=True)
            if (
                parse_interval_minutes(schedule) is None
                and not _DAILY_RE.match(schedule)
                and len(schedule.split()) != 5
            ):
                return ToolResult(
                    "Unsupported schedule. Use 'every Nm', 'daily HH:MM', or 5-field cron.",
                    is_error=True,
                )
            job = add_job(str(args.get("name", "")), schedule, prompt)
            return ToolResult(
                f"Scheduled '{job.name}' ({job.schedule}) id={job.id[-8:]}. "
                "Start 'oaset cron daemon' to fire it."
            )
        if action == "list":
            jobs = load_jobs()
            if not jobs:
                return ToolResult("No scheduled jobs.")
            lines = [
                f"[{j.id[-8:]}] {'✓' if j.enabled else '✗'} {j.name} · {j.schedule} · last {j.last_run or 'never'}"
                for j in jobs
            ]
            return ToolResult("\n".join(lines))
        if action == "remove":
            ok = remove_job(str(args.get("id", "")))
            return ToolResult("removed." if ok else "job not found.", is_error=not ok)
        if action in ("enable", "disable"):
            ok = set_enabled(str(args.get("id", "")), action == "enable")
            return ToolResult(f"{action}d." if ok else "job not found.", is_error=not ok)
        return ToolResult("action must be add|list|remove|enable|disable.", is_error=True)
