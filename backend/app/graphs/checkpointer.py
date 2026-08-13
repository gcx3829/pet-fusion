from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import cast

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


def sqlite_checkpointer(path: Path) -> AbstractAsyncContextManager[AsyncSqliteSaver]:
    path.parent.mkdir(parents=True, exist_ok=True)
    return cast(
        AbstractAsyncContextManager[AsyncSqliteSaver],
        AsyncSqliteSaver.from_conn_string(str(path.resolve())),
    )
