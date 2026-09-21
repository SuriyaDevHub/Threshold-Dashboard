"""BRV client — trade data read from S3 (parquet/csv), filtered by LE/source."""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Dict, List

from app.core.clients import mock
from app.core.config import get_settings


def _date_range(start: str, end: str) -> List[str]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    return [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]


class BRVClient:
    async def fetch_trades_s3(
        self,
        product_type: str,
        legal_entities: List[str],
        source_systems: List[str],
        start_date: str,
        end_date: str,
    ) -> List[Dict]:
        s = get_settings()
        if s.USE_MOCK:
            return mock.mock_trade_records(
                product_type, legal_entities, source_systems, start_date, end_date
            )
        return self._read_s3(
            product_type, legal_entities, source_systems, start_date, end_date
        )

    def _read_s3(self, product_type, legal_entities, source_systems, start_date, end_date):
        """Synchronous S3 read. Layout from S3_PREFIX_TEMPLATE, one prefix per day."""
        import boto3  # imported lazily so mock mode needs no AWS deps

        s = get_settings()
        kwargs = {"region_name": s.S3_REGION}
        if s.S3_ENDPOINT_URL:
            kwargs["endpoint_url"] = s.S3_ENDPOINT_URL
        s3 = boto3.client("s3", **kwargs)

        le_set = set(legal_entities or [])
        ss_set = set(source_systems or [])
        rows: List[Dict] = []

        for d in _date_range(start_date, end_date):
            prefix = s.S3_PREFIX_TEMPLATE.format(product_type=product_type, date=d)
            token = None
            while True:
                lp = {"Bucket": s.S3_BUCKET, "Prefix": prefix}
                if token:
                    lp["ContinuationToken"] = token
                listing = s3.list_objects_v2(**lp)
                for obj in listing.get("Contents", []):
                    body = s3.get_object(Bucket=s.S3_BUCKET, Key=obj["Key"])["Body"].read()
                    for r in self._parse(body, s.S3_FORMAT):
                        if le_set and r.get("legal_entity") not in le_set:
                            continue
                        if ss_set and r.get("source_system") not in ss_set:
                            continue
                        rows.append(r)
                if listing.get("IsTruncated"):
                    token = listing.get("NextContinuationToken")
                else:
                    break
        return rows

    @staticmethod
    def _parse(body: bytes, fmt: str) -> List[Dict]:
        if fmt == "parquet":
            import pyarrow.parquet as pq  # lazy import

            table = pq.read_table(io.BytesIO(body))
            return table.to_pylist()
        # csv
        text = body.decode("utf-8", errors="replace")
        return list(csv.DictReader(io.StringIO(text)))


def get_brv_client() -> BRVClient:
    return BRVClient()
