"""Meta endpoints — service identity / version."""

from fastapi import APIRouter

from src.api.deps import SettingsDep
from src.schemas.admin import VersionResponse


router = APIRouter(tags=["meta"])


@router.get(
    "/version",
    response_model = VersionResponse,
    summary        = "Return the service name and version",
    description    = "Returns values read from pyproject.toml at startup. Useful for deployment sanity checks.",
)
def get_version(settings: SettingsDep) -> VersionResponse:
    """Read name+version from the cached Settings object."""
    return VersionResponse(name=settings.app_name, version=settings.app_version)
