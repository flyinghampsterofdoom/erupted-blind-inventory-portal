from fastapi import Request
from fastapi.templating import Jinja2Templates


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def get_client_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get('x-forwarded-for')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    if request.client:
        return request.client.host
    return None
