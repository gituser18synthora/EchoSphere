"""Exception types and global handlers — users never see raw SQL/Mongo errors."""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymysql.err import MySQLError
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("backend.errors")


class ApiError(Exception):
    """Domain error with a user-safe message."""

    def __init__(self, message: str, status_code: int = 400, errors: list | None = None):
        self.message = message
        self.status_code = status_code
        self.errors = errors or []
        super().__init__(message)


class NotFoundError(ApiError):
    def __init__(self, entity: str = "Record"):
        super().__init__(f"{entity} not found.", status.HTTP_404_NOT_FOUND)


class ForbiddenError(ApiError):
    def __init__(self, message: str = "You do not have permission to perform this action."):
        super().__init__(message, status.HTTP_403_FORBIDDEN)


class ProviderNotAvailableError(ApiError):
    """A configured provider/model is inactive under platform AI governance.

    Raised at runtime resolution so live sessions fail closed instead of
    silently using (or replacing) a deactivated provider.
    """

    def __init__(self, message: str):
        super().__init__(message, status.HTTP_503_SERVICE_UNAVAILABLE)


class HardDeleteBlockedError(ApiError):
    def __init__(self):
        super().__init__(
            "Permanent deletion is disabled in the development environment.",
            status.HTTP_403_FORBIDDEN,
        )


def _err(status_code: int, message: str, errors: list | None = None) -> JSONResponse:
    body: dict = {"success": False, "message": message}
    if errors:
        body["errors"] = errors
    return JSONResponse(status_code=status_code, content=body)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError):
        return _err(exc.status_code, exc.message, exc.errors)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return _err(exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        errors = [
            {"field": ".".join(str(p) for p in e.get("loc", []) if p != "body"), "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        return _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "Validation failed.", errors)

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        logger.error("Integrity error on %s: %s", request.url.path, exc, exc_info=False)
        return _err(
            status.HTTP_409_CONFLICT,
            "The record conflicts with existing data (duplicate or missing reference).",
        )

    @app.exception_handler(OperationalError)
    async def operational_error_handler(request: Request, exc: OperationalError):
        logger.error("Database unavailable on %s: %s", request.url.path, exc, exc_info=False)
        return _err(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The database is temporarily unavailable. Please try again shortly.",
        )

    @app.exception_handler(SQLAlchemyError)
    async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError):
        logger.exception("Database error on %s", request.url.path)
        return _err(status.HTTP_500_INTERNAL_SERVER_ERROR, "An internal database error occurred.")

    @app.exception_handler(MySQLError)
    async def pymysql_error_handler(request: Request, exc: MySQLError):
        logger.exception("MySQL error on %s", request.url.path)
        return _err(status.HTTP_500_INTERNAL_SERVER_ERROR, "An internal database error occurred.")

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s", request.url.path)
        return _err(status.HTTP_500_INTERNAL_SERVER_ERROR, "An unexpected error occurred.")
