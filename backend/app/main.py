"""FastAPI entrypoint.

The frontend asks GET /api/modules to learn which modules exist, then renders
nav + routes from that. Backend and frontend both discover plugins, so adding
a module never touches the shell.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.plugin_loader import register_modules

settings = get_settings()
app = FastAPI(title=settings.APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auto-mount every plugin under app/modules/.
LOADED = register_modules(app)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "env": settings.ENV, "mock": settings.USE_MOCK}


@app.get("/api/modules")
def list_modules() -> list:
    """Module registry consumed by the frontend to build nav + routes."""
    return [
        {
            "id": m.id,
            "name": m.name,
            "description": m.description,
            "icon": m.icon,
            "base_path": m.base_path,
        }
        for m in LOADED
    ]
