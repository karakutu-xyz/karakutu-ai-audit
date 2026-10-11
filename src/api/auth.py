"""The one bearer check (`Authorization: Bearer <API_AUTH_TOKEN>`), shared by /api/audit, /ready
and the /api/v1 article routes."""
import secrets

from fastapi import Request, status
from fastapi.responses import JSONResponse

from src.api.models import ErrorResponse


def bearer_matches(request: Request, token: str) -> bool:
    """`Authorization: Bearer <token>`, compared in constant time. False when `token` is empty."""
    if not token:
        return False
    header = request.headers.get("authorization") or ""
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return secrets.compare_digest(supplied.strip().encode(), token.encode())


def unauthorized() -> JSONResponse:
    """401 that names nothing the caller sent."""
    return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED,
                        headers={"WWW-Authenticate": "Bearer"},
                        content=ErrorResponse(error="unauthorized", detail="a valid bearer token is required",
                                              status_code=401).model_dump())
