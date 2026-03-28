#!/usr/bin/env python3
"""
kyumyee-backend — single Render service for all MCPs and APIs.

MCP 추가 방법:
1. buildCommand에 git clone 추가
2. sys.path.insert + import server
3. StreamableHTTPSessionManager 인스턴스 생성
4. lifespan에 .run() 추가
5. routes에 Mount("/<name>/mcp") 추가
"""

import os
import sys
import contextlib
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse
from starlette.requests import Request
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

# ── Delusionist Factory MCP ──────────────────────────────────────────────────
# buildCommand: git clone .../delusionist_factory_personal.git delusionist
_delusionist_dir = os.path.join(os.path.dirname(__file__), "delusionist")
os.makedirs(os.path.join(_delusionist_dir, "input"), exist_ok=True)  # gitignored in source
sys.path.insert(0, _delusionist_dir)

from mcp_server import server as delusionist_server  # noqa: E402

delusionist_sm = StreamableHTTPSessionManager(app=delusionist_server, stateless=True)

# ── 새 MCP는 위 패턴 반복 ──────────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    async with delusionist_sm.run():
        # 새 MCP 추가 시: 중첩 async with <name>_sm.run():
        yield


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/health", health),
        Mount("/delusionist/mcp", app=delusionist_sm.handle_request),
        # 새 MCP: Mount("/<name>/mcp", app=<name>_sm.handle_request),
    ],
)
