"""Liveness and readiness endpoints — used by orchestrators (Kubernetes, Docker) to gate traffic."""

from fastapi import APIRouter, Request, status

from src.schemas.admin import HealthResponse


router = APIRouter(prefix="/health", tags=["health"])


@router.get(
    "",
    response_model = HealthResponse,
    summary        = "Liveness probe",
    description    = "Returns 200 as long as the process is running. Does NOT check downstream dependencies.",
)
def liveness() -> HealthResponse:
    """Liveness = 'is the process alive'. Keep this cheap — no I/O, no locks."""
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model = HealthResponse,
    summary        = "Readiness probe",
    description    = "Returns 200 when the service is ready to serve traffic (i.e. the shared httpx client is up).",
    responses      = {503: {"model": HealthResponse, "description": "Service is not ready yet."}},
)
def readiness(request: Request) -> HealthResponse:
    """
    Readiness = 'can the service actually handle a request?'.

    We consider the service ready once the lifespan has attached the shared
    httpx client to `app.state`. If orchestrators hit this before startup
    completes they get a 503 so they keep the pod out of rotation.
    """
    http_client = getattr(request.app.state, "http", None)
    if http_client is None:
        # Return a 503 with a consistent body shape.
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
            content     = HealthResponse(status="not_ready").model_dump(),
        )
    return HealthResponse(status="ok")
