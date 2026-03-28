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
import subprocess
import importlib.util
import contextlib
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse
from starlette.requests import Request
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

# ── Delusionist Factory MCP ──────────────────────────────────────────────────
_delusionist_dir = os.path.join(os.path.dirname(__file__), "delusionist")
if not os.path.exists(os.path.join(_delusionist_dir, "mcp_server.py")):
    subprocess.run(
        ["git", "clone", "--depth", "1",
         "https://github.com/Kyum-Yee/delusionist_factory_personal.git",
         _delusionist_dir],
        check=True,
    )
os.makedirs(os.path.join(_delusionist_dir, "input"), exist_ok=True)
sys.path.insert(0, _delusionist_dir)

# uvicorn registers THIS file as sys.modules['main'], so delusionist's mcp_server.py
# would pick up the wrong 'main' when it does `from main import DelusionistFactory`.
# Temporarily swap sys.modules['main'] to delusionist's main, then restore.
_spec = importlib.util.spec_from_file_location("main", os.path.join(_delusionist_dir, "main.py"))
_delusionist_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_delusionist_main)
_saved_main = sys.modules.get("main")
sys.modules["main"] = _delusionist_main

from mcp_server import server as delusionist_server  # noqa: E402

sys.modules["main"] = _saved_main  # restore

delusionist_sm = StreamableHTTPSessionManager(app=delusionist_server, stateless=False)

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
