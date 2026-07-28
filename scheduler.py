"""Cron scheduling for agents — the "morning brief".

An agent with a `schedule_cron` runs unattended: it plans, approves its own plan
(nobody is at the keyboard), collects, and writes a brief that leads with what
is new since its previous run on the same question.

Single-process by design. The Docker image runs gunicorn with exactly one
worker because the live job trace is in-process memory; that same constraint
means exactly one scheduler, so jobs cannot double-fire.
"""
import asyncio
import json
import sys
import threading
import uuid
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from models import Mission

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()
JOB_PREFIX = "agent:"


def validate_cron(expr: str) -> tuple[bool, str]:
    """Check a 5-field crontab expression without registering anything."""
    expr = (expr or "").strip()
    if not expr:
        return True, ""  # empty simply means "not scheduled"
    try:
        CronTrigger.from_crontab(expr)
        return True, ""
    except Exception as e:
        return False, str(e)


def describe_next_run(expr: str) -> str:
    ok, _ = validate_cron(expr)
    if not ok or not expr.strip():
        return ""
    try:
        trigger = CronTrigger.from_crontab(expr)
        nxt = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
        return nxt.strftime("%Y-%m-%d %H:%M UTC") if nxt else ""
    except Exception:
        return ""


def _run_scheduled_agent(agent_id: str) -> None:
    """Fired by APScheduler on its own thread. Creates a mission that will
    auto-approve, then hands off to the normal collection machinery."""
    try:
        asyncio.run(_launch(agent_id))
    except Exception as e:  # noqa: BLE001 - a scheduler thread must never die
        print(f"[SCHED] agent {agent_id} failed to launch: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)


async def _launch(agent_id: str) -> None:
    from storage import (get_agent, insert_mission, get_latest_finished_mission)
    from jobs import create_mission_job
    from agent_runner import start_planning

    agent = await get_agent(agent_id)
    if not agent or not agent.active:
        return
    question = (agent.schedule_question or "").strip()
    if not question:
        print(f"[SCHED] agent {agent.name} has a schedule but no question; skipping",
              file=sys.stderr, flush=True)
        return

    # Link to the previous run on the same question so the brief can diff.
    prior = await get_latest_finished_mission(agent_id, question, "")
    job_id = create_mission_job(question, agent.default_max_sources)
    mission = Mission(
        id=str(uuid.uuid4()), agent_id=agent_id, question=question,
        status="planning", job_id=job_id,
        parent_mission_id=prior.id if prior else None,
        budget_json=json.dumps({
            "max_passes": agent.default_max_passes,
            "max_sources": agent.default_max_sources,
            "per_req_attempts": agent.default_per_req_attempts,
            "auto_approve": True,
            "scheduled": True,
        }),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    await insert_mission(mission)
    print(f"[SCHED] launching '{question[:60]}' for agent {agent.name}",
          file=sys.stderr, flush=True)
    start_planning(mission.id)


def run_scheduled_agent_now(agent_id: str) -> None:
    """Fire a scheduled run immediately, on a worker thread so the request
    returns straight away. Used by the 'Run now' action."""
    threading.Thread(target=_run_scheduled_agent, args=(agent_id,), daemon=True).start()


def sync_agent_jobs() -> int:
    """Make the registered jobs match the agents table. Called at boot and
    whenever an agent is created, edited or deleted."""
    from storage import list_scheduled_agents

    sched = _scheduler
    if sched is None:
        return 0
    try:
        agents = asyncio.run(list_scheduled_agents())
    except Exception as e:  # noqa: BLE001
        print(f"[SCHED] could not load agents: {e}", file=sys.stderr, flush=True)
        return 0

    wanted = {}
    for a in agents:
        ok, _ = validate_cron(a.schedule_cron or "")
        if ok and (a.schedule_cron or "").strip() and (a.schedule_question or "").strip():
            wanted[JOB_PREFIX + a.id] = a

    for job in list(sched.get_jobs()):
        if job.id.startswith(JOB_PREFIX) and job.id not in wanted:
            sched.remove_job(job.id)

    for job_id, a in wanted.items():
        sched.add_job(
            _run_scheduled_agent, CronTrigger.from_crontab(a.schedule_cron),
            args=[a.id], id=job_id, replace_existing=True,
            max_instances=1,       # never overlap a still-running mission
            coalesce=True,         # a missed window fires once, not N times
            misfire_grace_time=3600,
        )
    return len(wanted)


def start_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return
        sched = BackgroundScheduler(timezone="UTC")
        sched.start()
        _scheduler = sched
    n = sync_agent_jobs()
    print(f"[SCHED] scheduler started with {n} agent schedule(s)",
          file=sys.stderr, flush=True)


def scheduled_jobs() -> list[dict]:
    if _scheduler is None:
        return []
    out = []
    for job in _scheduler.get_jobs():
        if not job.id.startswith(JOB_PREFIX):
            continue
        out.append({
            "agent_id": job.id[len(JOB_PREFIX):],
            "next_run": job.next_run_time.strftime("%Y-%m-%d %H:%M UTC")
            if job.next_run_time else "",
        })
    return out
