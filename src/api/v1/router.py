"""
v1 router — composes every endpoint module under `/api/v1`.

Adding a new resource means: write the endpoint module, then include its
router here. The prefix lives on this composite router, not on each child.
"""

from fastapi import APIRouter

from src.api.v1.endpoints import admin, health, meta, teams


v1_router = APIRouter(prefix="/api/v1")

# Order is purely cosmetic (OpenAPI doc ordering), not semantic.
v1_router.include_router(health.router)
v1_router.include_router(teams.router)
v1_router.include_router(admin.router)
v1_router.include_router(meta.router)
