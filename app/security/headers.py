from fastapi import FastAPI, Request
from starlette.responses import Response


ROBOTS_HEADER = "noindex, nofollow, noarchive"


def install_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Robots-Tag"] = ROBOTS_HEADER
        return response
