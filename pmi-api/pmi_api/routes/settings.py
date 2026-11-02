"""App-level client settings (legacy war-index `/api/settings` parity).

The maga-index SPA reads `future_phrase` to render question titles
("…within the next 12 months"). Value comes from env (PMI_API_FUTURE_PHRASE)
so deploys can tune copy without a code change.
"""

from __future__ import annotations

from fastapi import APIRouter

from pmi_api.config import api_settings
from pmi_api.schemas import AppSettingsResponse

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=AppSettingsResponse)
async def get_settings() -> AppSettingsResponse:
    return AppSettingsResponse(future_phrase=api_settings.future_phrase)
