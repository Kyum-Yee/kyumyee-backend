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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse, Response
from starlette.requests import Request
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from egg_web import build_stream, extract_egg_bytes


# ── Auth Middleware ──────────────────────────────────────────────────────────
class MCPAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/delusionist/mcp"):
            api_key = os.environ.get("MCP_API_KEY", "")
            if not api_key or request.headers.get("Authorization") != f"Bearer {api_key}":
                return Response("Unauthorized", status_code=401)
        return await call_next(request)


# ── Delusionist Factory MCP ──────────────────────────────────────────────────
_DELUSIONIST_REPO = "https://github.com/Kyum-Yee/delusionist_factory_personal.git"
_DELUSIONIST_SHA = os.environ.get("DELUSIONIST_COMMIT_SHA", "")
_delusionist_dir = os.path.join(os.path.dirname(__file__), "delusionist")
if not os.path.exists(os.path.join(_delusionist_dir, "mcp_server.py")):
    try:
        if _DELUSIONIST_SHA:
            subprocess.run(
                ["git", "clone", "--no-checkout", _DELUSIONIST_REPO, _delusionist_dir],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-C", _delusionist_dir, "checkout", _DELUSIONIST_SHA],
                check=True, capture_output=True, text=True,
            )
        else:
            subprocess.run(
                ["git", "clone", "--depth", "1", _DELUSIONIST_REPO, _delusionist_dir],
                check=True, capture_output=True, text=True,
            )
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] git clone failed: {e.stderr}", file=sys.stderr)
        sys.exit(1)
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


MAX_TOTAL = 300 * 1024 * 1024  # 300 MB


async def egg_extract(request: Request) -> JSONResponse:
    try:
        form = await request.form()
        file_items = form.getlist("files")
        if not file_items:
            return JSONResponse({"success": False, "error": "No files uploaded"}, status_code=400)

        parts: list[bytes] = []
        total = 0
        for item in file_items:
            data = await item.read()
            total += len(data)
            if total > MAX_TOTAL:
                return JSONResponse(
                    {"success": False, "error": f"Total upload exceeds {MAX_TOTAL // 1024 // 1024} MB limit"},
                    status_code=413,
                )
            parts.append(data)

        archive_size = sum(len(p) for p in parts)
        stream, is_split, volume_count = build_stream(parts)
        files, _, is_solid = extract_egg_bytes(stream, detected_split=is_split)

        total_files = sum(1 for f in files if not f["isDirectory"])
        total_dirs = sum(1 for f in files if f["isDirectory"])
        total_size = sum(f["size"] for f in files if not f["isDirectory"])

        return JSONResponse({
            "success": True,
            "files": files,
            "archiveSize": archive_size,
            "isSplit": is_split,
            "isSolid": is_solid,
            "volumeCount": volume_count,
            "totalFiles": total_files,
            "totalDirs": total_dirs,
            "totalSize": total_size,
        })
    except Exception as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=400)


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/health", health),
        Route("/egg-extract", egg_extract, methods=["POST", "GET"]),
        Mount("/delusionist/mcp", app=delusionist_sm.handle_request),
        # 새 MCP: Mount("/<name>/mcp", app=<name>_sm.handle_request),
    ],
)

app.add_middleware(MCPAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://kyumyee-playground.vercel.app"],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Mcp-Session-Id"],
)
