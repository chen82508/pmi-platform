"""Admin prompt editor (legacy war-index `/api/admin/prompts` parity).

GET returns the latest version of every prompt in `core_prompts`, keyed by
prompt name, decorated with the active production factor model (model id +
temperature) bound to that exact prompt version when one exists.

Both GET and PUT require the service API key (router-level `require_api_key`):
the templates are the platform's classification IP, so unlike most read
routes this one is not public — previously only PUT was gated, which left the
templates + bound model ids readable unauthenticated. The gate is a no-op in
dev (`PMI_API_REQUIRE_AUTH=false`), where the security-group allowlist is the
compensating control.

PUT respects the platform's append-only prompt contract (§6.1): an edited
``content`` never updates a row in place — it inserts ``(name, version+1)``
with a fresh sha256. Versions created here are DB-only drafts: git markdown
remains the canonical source for versions referenced by index-def
``prompt_ref``s, and a draft only takes effect once a factor model is
registered/promoted against it (`pmi-core models`). ``model`` /
``temperature`` / ``top_p`` / ``reasoning_effort`` are display-only and
ignored on save.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends
from pmi_core.models import CoreFactorModel, CorePrompt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session, require_api_key
from pmi_api.schemas import PromptRecord, PromptsSaveResponse

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_api_key)],
)


async def _latest_prompts(session: AsyncSession) -> dict[str, CorePrompt]:
    """Latest CorePrompt row per name (highest version wins)."""
    rows = (
        (await session.execute(select(CorePrompt).order_by(CorePrompt.name, CorePrompt.version)))
        .scalars()
        .all()
    )
    latest: dict[str, CorePrompt] = {}
    for row in rows:  # ordered by version asc → last write per name is latest
        latest[row.name] = row
    return latest


@router.get("/prompts", response_model=dict[str, PromptRecord])
async def list_prompts(
    session: AsyncSession = Depends(get_session),
) -> dict[str, PromptRecord]:
    latest = await _latest_prompts(session)

    # Active production factor models, keyed by the prompt row they bind.
    active = (
        (
            await session.execute(
                select(CoreFactorModel).where(CoreFactorModel.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    by_prompt_id = {m.prompt_id: m for m in active}

    out: dict[str, PromptRecord] = {}
    for name, row in sorted(latest.items()):
        bound = by_prompt_id.get(row.id)
        out[name] = PromptRecord(
            content=row.template,
            model=bound.llm_model_id if bound else None,
            temperature=float(bound.temperature)
            if bound and bound.temperature is not None
            else None,
        )
    return out


@router.put("/prompts", response_model=PromptsSaveResponse)
async def save_prompts(
    prompts: dict[str, PromptRecord],
    session: AsyncSession = Depends(get_session),
) -> PromptsSaveResponse:
    latest = await _latest_prompts(session)

    new_versions: dict[str, int] = {}
    for name, record in prompts.items():
        current = latest.get(name)
        if current is not None and current.template == record.content:
            continue  # unchanged → no new version
        version = (current.version + 1) if current is not None else 1
        session.add(
            CorePrompt(
                name=name,
                version=version,
                template=record.content,
                sha256=hashlib.sha256(record.content.encode("utf-8")).hexdigest(),
            )
        )
        new_versions[name] = version

    if new_versions:
        await session.commit()
    return PromptsSaveResponse(status="ok", new_versions=new_versions)
