from typing import Annotated

from fastapi import Depends, Request

from app.container import AppContainer


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


ContainerDependency = Annotated[AppContainer, Depends(get_container)]
