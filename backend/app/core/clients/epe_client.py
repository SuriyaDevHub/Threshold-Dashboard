"""EPE client — exception data (REST). Auth + response path are config-driven."""
from __future__ import annotations

from typing import Dict, List, Optional

from app.core.clients import mock
from app.core.clients.base import BaseClient
from app.core.config import get_settings


def _extract(raw, path: str):
    if isinstance(raw, dict) and path:
        return raw.get(path, raw.get("data", []))
    return raw


class EPEClient(BaseClient):
    async def fetch_exceptions(
        self,
        product_type: str,
        legal_entities: List[str],
        source_systems: List[str],
        start_date: str,
        end_date: str,
    ) -> List[Dict]:
        s = get_settings()
        if s.USE_MOCK:
            return mock.mock_exception_records(
                product_type, legal_entities, source_systems, start_date, end_date
            )
        raw = await self._post(
            s.EPE_EXCEPTIONS_PATH,
            {
                "productType": product_type,
                "legalEntities": legal_entities,
                "sourceSystems": source_systems,
                "startDate": start_date,
                "endDate": end_date,
            },
        )
        return _extract(raw, s.EPE_RESPONSE_PATH)


def get_epe_client() -> EPEClient:
    s = get_settings()
    auth = {"scheme": s.EPE_AUTH_SCHEME, "header": s.EPE_AUTH_HEADER, "token": s.EPE_TOKEN}
    return EPEClient(s.EPE_BASE_URL, s.EPE_TIMEOUT, auth=auth)
