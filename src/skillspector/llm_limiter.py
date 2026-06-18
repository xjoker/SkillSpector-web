# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime limits for LLM calls."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager

LLM_MAX_CONCURRENCY_ENV = "SKILLSPECTOR_LLM_MAX_CONCURRENCY"
DEFAULT_LLM_MAX_CONCURRENCY = 10

_LLM_SINGLE_CALL_LOCK = threading.Lock()


def get_llm_max_concurrency(configured: int | None = None) -> int:
    if configured is not None:
        return max(1, configured)
    raw = os.environ.get(LLM_MAX_CONCURRENCY_ENV, str(DEFAULT_LLM_MAX_CONCURRENCY))
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_LLM_MAX_CONCURRENCY


@contextmanager
def llm_call_gate(configured: int | None = None) -> Iterator[None]:
    if get_llm_max_concurrency(configured) == 1:
        with _LLM_SINGLE_CALL_LOCK:
            yield
        return
    yield


@asynccontextmanager
async def async_llm_call_gate(configured: int | None = None):
    if get_llm_max_concurrency(configured) != 1:
        yield
        return

    import asyncio

    await asyncio.to_thread(_LLM_SINGLE_CALL_LOCK.acquire)
    try:
        yield
    finally:
        _LLM_SINGLE_CALL_LOCK.release()
