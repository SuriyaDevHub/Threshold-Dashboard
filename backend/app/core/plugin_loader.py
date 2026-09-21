"""Plugin loader.

Every folder under app/modules/ that exposes:
    META   -> dict with id, name, description, icon
    router -> fastapi.APIRouter
is auto-discovered and mounted at  /api/modules/{id}.

To add a backend module: drop a new package in app/modules/. No edits here,
no edits in main.py. That is the whole plugin contract.
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import List

from fastapi import APIRouter, FastAPI

from app import modules as modules_pkg


@dataclass
class LoadedModule:
    id: str
    name: str
    description: str
    icon: str
    base_path: str


REQUIRED_META_KEYS = {"id", "name", "description", "icon"}


def discover_modules() -> List[LoadedModule]:
    loaded: List[LoadedModule] = []
    for mod_info in pkgutil.iter_modules(modules_pkg.__path__):
        if not mod_info.ispkg:
            continue
        pkg = importlib.import_module(f"{modules_pkg.__name__}.{mod_info.name}")
        meta = getattr(pkg, "META", None)
        router = getattr(pkg, "router", None)
        if meta is None or router is None:
            continue
        missing = REQUIRED_META_KEYS - set(meta)
        if missing:
            raise RuntimeError(
                f"Module '{mod_info.name}' META missing keys: {missing}"
            )
        if not isinstance(router, APIRouter):
            raise RuntimeError(f"Module '{mod_info.name}' router is not an APIRouter")
        loaded.append(
            LoadedModule(
                id=meta["id"],
                name=meta["name"],
                description=meta["description"],
                icon=meta["icon"],
                base_path=f"/api/modules/{meta['id']}",
            )
        )
    return sorted(loaded, key=lambda m: m.name)


def register_modules(app: FastAPI) -> List[LoadedModule]:
    loaded: List[LoadedModule] = []
    for mod_info in pkgutil.iter_modules(modules_pkg.__path__):
        if not mod_info.ispkg:
            continue
        pkg = importlib.import_module(f"{modules_pkg.__name__}.{mod_info.name}")
        meta = getattr(pkg, "META", None)
        router = getattr(pkg, "router", None)
        if meta is None or router is None:
            continue
        app.include_router(
            router,
            prefix=f"/api/modules/{meta['id']}",
            tags=[meta["name"]],
        )
        loaded.append(
            LoadedModule(
                id=meta["id"],
                name=meta["name"],
                description=meta["description"],
                icon=meta["icon"],
                base_path=f"/api/modules/{meta['id']}",
            )
        )
    return sorted(loaded, key=lambda m: m.name)
