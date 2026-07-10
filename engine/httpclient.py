from __future__ import annotations

import httpx

from .config import HTTPX_VERIFY

CLIENT = httpx.AsyncClient(
    verify=HTTPX_VERIFY,
    limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
)
