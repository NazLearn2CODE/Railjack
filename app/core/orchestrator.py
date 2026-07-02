"""Centralized 2DOT orchestration (blueprint §1.1): a supervisor agent delegates
subtasks to specialist workers.

Workers are first-class OS processes — each is an `AgentSession` that flows
through the shared `HiveMindScheduler` (admission / rate-limit / AIMD /
circuit-breaker / token-budget), the security approval gate, and the receipt
ledger. Delegation is exposed to the supervisor as a host-executed tool via the SDK's
in-process MCP server (see `ClaudeSdkProvider(delegate_many=...)`):
`Team.delegate_many` fans N workers out concurrently (`asyncio.gather`). Workers
run on a plain provider with NO delegate tool, so depth is capped at 1 — the
definition of the Centralized topology (Hierarchical/Decentralized are deferred).

See ADR 2026-07-02-centralized-2dot-topology and 2026-07-02-delegate-many-fanout.
"""
from __future__ import annotations

import asyncio
import uuid
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.core.agent import AgentSession, ALLOWED_TOOLS
from app.core.provider import ClaudeSdkProvider, Provider
from app.core.scheduler import HiveMindScheduler
from app.core.security import SecurityPolicy

logger = logging.getLogger("orbiter.orchestrator")


@dataclass
class WorkerRole:
    """A specialist worker the supervisor can delegate to.

    `allowed_tools` defaults to the native tool set, which excludes "delegate" —
    workers are leaves and cannot spawn sub-workers.
    """
    name: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=lambda: list(ALLOWED_TOOLS))


# A sensible general-purpose team for the default dashboard surface. The role
# names are surfaced to the supervisor so it knows who it can delegate to.
DEFAULT_ROLES: list[WorkerRole] = [
    WorkerRole(
        name="researcher",
        system_prompt=(
            "You are a research specialist. Investigate and gather context using "
            "Read, Grep, Glob, and WebSearch. Report findings concisely — make no changes."
        ),
    ),
    WorkerRole(
        name="coder",
        system_prompt=(
            "You are an implementation specialist. Make focused changes using Read, "
            "Edit, Write, and Bash. Return a concise summary of what you changed and why."
        ),
    ),
]


def default_supervisor_prompt(roles: list[WorkerRole]) -> str:
    """Supervisor system prompt naming the hired roles and mandating fan-out delegation.

    Pure function over the role list — no defaults baked into the prompt, so a
    custom role set is reflected correctly.
    """
    roster = ", ".join(r.name for r in roles)
    return (
        "You are the supervisor of a specialist team. Decompose the task into "
        "subtasks and dispatch them to workers via the `delegate_many` tool (args: "
        "delegations, a list of {role, task}). Independent subtasks run concurrently "
        "— fan them out together in one call. Each delegation returns its worker's "
        "result under a role heading; synthesize them into a final answer.\n\n"
        f"Roles available: {roster}\n\n"
        "Delegate subtasks rather than implementing directly, then combine the results."
    )


class Team:
    """Centralized topology: a supervisor plus hired specialist workers.

    The supervisor is an ordinary `AgentSession` whose `ClaudeSdkProvider`
    carries a `delegate_many` callback (→ in-process `delegate_many` MCP tool).
    `delegate_many()` is the OS-level fan-out primitive: it spawns N worker
    `AgentSession`s concurrently (`asyncio.gather`) on the Team's shared
    scheduler/security and returns their results as per-role sections. Both
    supervisor and workers bill against the same `HiveMindScheduler` (admission
    bounds their concurrency; AIMD/breaker react).
    """

    def __init__(
        self,
        scheduler: HiveMindScheduler,
        security: Optional[SecurityPolicy] = None,
        worker_provider: Optional[Provider] = None,
        register: Optional[Callable[[AgentSession], None]] = None,
    ):
        self.scheduler = scheduler
        self.security = security
        # Plain provider with no delegate callback → workers are leaves (no recursion).
        self.worker_provider = worker_provider
        self.roles: dict[str, WorkerRole] = {}
        # Adopt each worker into the session manager before run(), mirroring the
        # delegate-callback pattern. A registered worker's approval gate is then
        # actionable via the shared POST /api/sessions/{id}/approve (otherwise its
        # gated tools block to a fail-closed timeout). main.py wires manager.register.
        self.register = register
        # The supervisor whose bus worker events forward onto (set by supervisor()).
        self._supervisor: Optional[AgentSession] = None

    def hire(self, *roles: WorkerRole) -> "Team":
        for role in roles:
            self.roles[role.name] = role
        return self

    async def _run_worker(self, role: str, task: str, fanout_id: str) -> str:
        """Run one worker for `role` on `task`; forward its events onto the
        supervisor's bus tagged with `fanout_id`. Return its final text or an
        error string.

        Never raises: a worker failure / over-budget / breaker-open is returned as
        a descriptive string so the supervisor can recover or report (tool-result
        semantics). Admission accounting and receipts still apply via the worker's
        own AgentSession.run(). Extracted from the old delegate(); shared by every
        delegation in a delegate_many fan-out.
        """
        r = self.roles.get(role)
        if r is None:
            available = ", ".join(sorted(self.roles)) or "(none hired)"
            return f"Unknown role '{role}'. Available: {available}."

        # The CLI requires a valid UUID for --session-id (it rejects prefixed ids at
        # init: "Error: Invalid session ID. Must be a valid UUID."). The role is
        # carried by the worker_event frame + AgentSession.kind, not the id.
        worker = AgentSession(
            session_id=str(uuid.uuid4()),
            prompt=task,
            scheduler=self.scheduler,
            system_prompt=r.system_prompt,
            security=self.security,
            provider=self.worker_provider,
            allowed_tools=r.allowed_tools,
            kind="worker",
        )

        # Forward every worker event onto the supervisor's bus as a nested
        # worker_event frame tagged with fanout_id (the grouping key), so worker
        # activity streams live through the supervisor's /ws connection AND replays
        # via GET /api/sessions/{supervisor} (ingest persists).
        sup = self._supervisor
        if sup is not None:
            role_name = role
            worker_id = worker.session_id

            async def _to_supervisor(ev: dict) -> None:
                await sup.ingest({
                    "type": "worker_event",
                    "role": role_name,
                    "worker_id": worker_id,
                    "fanout_id": fanout_id,
                    "event": ev,
                })

            worker.event_sink = _to_supervisor

        # Register before run() so the worker's approval gate is actionable through
        # the shared /approve surface (excluded from list_sessions by kind).
        if self.register is not None:
            self.register(worker)

        logger.info("Supervisor fanning out to '%s': %.80s", role, task)
        try:
            await worker.run()
        except Exception as e:  # never raises — a crashed worker becomes a string the supervisor can act on
            logger.exception("Worker '%s' raised during run()", role)
            return f"Worker '{role}' crashed: {e}"

        if worker.status == "completed":
            return worker.final_text() or f"(worker '{role}' produced no text)"
        return f"Worker '{role}' ended {worker.status}: {worker.error_message or 'no detail'}"

    async def delegate_many(self, delegations: list[dict]) -> str:
        """Fan out N workers concurrently; return per-role result sections in input
        order. Each worker is a first-class AgentSession through the shared
        scheduler/security/gate/receipts. `_run_worker` never raises, so gather
        cannot throw — a mixed success/failure fan-out returns all N sections.

        # ponytail: no explicit concurrency cap — AdmissionControl (AIMD live limit,
        # init 4 / max 10) already bounds in-flight workers and queues the rest.
        """
        if not delegations:
            return "(no delegations)"
        fanout_id = f"fanout-{uuid.uuid4().hex[:8]}"
        results = await asyncio.gather(*[
            self._run_worker(d.get("role", ""), d.get("task", ""), fanout_id)
            for d in delegations
        ])
        return "\n\n".join(
            f"### {d.get('role', '?')}\n{res}" for d, res in zip(delegations, results)
        )

    def supervisor(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[list[str]] = None,
    ) -> AgentSession:
        """Build the supervisor `AgentSession` with the `delegate_many` tool wired.

        The supervisor's `ClaudeSdkProvider` carries the delegate_many callback,
        which `stream()` exposes as an in-process `delegate_many` MCP tool.
        `allowed_tools` defaults to the native set plus "delegate_many".
        """
        tools = list(allowed_tools if allowed_tools is not None else ALLOWED_TOOLS)
        if "delegate_many" not in tools:
            tools.append("delegate_many")
        sup = AgentSession(
            session_id=str(uuid.uuid4()),  # must be a valid UUID (CLI rejects prefixed ids)
            prompt=prompt,
            scheduler=self.scheduler,
            system_prompt=system_prompt,
            security=self.security,
            provider=ClaudeSdkProvider(delegate_many=self.delegate_many),
            allowed_tools=tools,
            kind="supervisor",
        )
        # Remember the supervisor so worker events (delegate) forward onto its bus.
        self._supervisor = sup
        return sup
