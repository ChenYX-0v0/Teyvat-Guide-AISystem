"""依赖注入。"""

from fastapi import Request

from app.chains.orchestrator import Orchestrator
from app.core.settings import Settings


def get_orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings
