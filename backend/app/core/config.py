"""Central configuration. Live EPE + S3 wiring is env-driven.

Set USE_MOCK=false to go live. EPE is a REST API; BRV trade extracts are read
from S3. Auth scheme, response path, S3 layout and file format are all config so
you don't touch code to point at your environment.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List


def _split(value: str) -> List[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


class Settings:
    APP_NAME: str = os.getenv("APP_NAME", "TCFC OMRC Dashboard")
    ENV: str = os.getenv("ENV", "local")

    # Master switch. false = real EPE + S3.
    USE_MOCK: bool = os.getenv("USE_MOCK", "true").lower() == "true"

    CORS_ORIGINS: List[str] = _split(
        os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    )

    # Server-side dataset cache lives here (JSON per dataset, survives reload).
    DATA_DIR: str = os.getenv("DATA_DIR", "./_datasets")

    # --- EPE API (exception data) ---
    EPE_BASE_URL: str = os.getenv("EPE_BASE_URL", "https://epe.internal/api")
    EPE_TIMEOUT: float = float(os.getenv("EPE_TIMEOUT", "60"))
    EPE_EXCEPTIONS_PATH: str = os.getenv("EPE_EXCEPTIONS_PATH", "exceptions/query")
    # Auth: scheme in {bearer, apikey, basic, none}; header name + value/token.
    EPE_AUTH_SCHEME: str = os.getenv("EPE_AUTH_SCHEME", "bearer")
    EPE_AUTH_HEADER: str = os.getenv("EPE_AUTH_HEADER", "Authorization")
    EPE_TOKEN: str = os.getenv("EPE_TOKEN", "")
    # JSON key that holds the row array in the response ("" = response is the array).
    EPE_RESPONSE_PATH: str = os.getenv("EPE_RESPONSE_PATH", "data")

    # --- BRV trade data on S3 ---
    S3_BUCKET: str = os.getenv("S3_BUCKET", "")
    S3_REGION: str = os.getenv("S3_REGION", "eu-west-2")
    # {product_type} and {date} are substituted per day in the window.
    S3_PREFIX_TEMPLATE: str = os.getenv(
        "S3_PREFIX_TEMPLATE", "brv/{product_type}/dt={date}/"
    )
    S3_FORMAT: str = os.getenv("S3_FORMAT", "parquet")  # parquet | csv
    S3_ENDPOINT_URL: str = os.getenv("S3_ENDPOINT_URL", "")  # for S3-compatible stores


@lru_cache
def get_settings() -> Settings:
    return Settings()
