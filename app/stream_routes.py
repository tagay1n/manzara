"""Streaming/log API route registration."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse


def register_stream_routes(
    app: FastAPI,
    *,
    state_provider: Callable[[], Any],
    sse_poll_interval_seconds: float,
    sse_heartbeat_every_empty_polls: int,
) -> Dict[str, Any]:
    """Register run-log pagination and SSE event stream endpoints."""

    @app.get("/api/runs/{run_id}/logs")
    def run_logs(
        run_id: int,
        after_log_id: int = Query(0, ge=0),
        before_log_id: Optional[int] = Query(None, gt=0),
        tail: bool = Query(False),
        limit: int = Query(400, ge=1, le=2000),
    ) -> JSONResponse:
        """Return logs for one run with cursor pagination (after/before/tail)."""
        state = state_provider()
        run = state.db.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        if tail and (after_log_id > 0 or before_log_id is not None):
            raise HTTPException(
                status_code=400,
                detail="tail mode cannot be combined with after_log_id or before_log_id",
            )
        if before_log_id is not None and after_log_id > 0:
            raise HTTPException(
                status_code=400,
                detail="before_log_id cannot be combined with after_log_id",
            )

        lines = state.db.get_logs(
            run_id=run_id,
            after_log_id=after_log_id,
            before_log_id=before_log_id,
            tail=tail,
            limit=limit,
        )
        next_after_log_id = int(lines[-1]["log_id"]) if lines else int(after_log_id or 0)
        if lines:
            next_before_log_id = int(lines[0]["log_id"])
        elif before_log_id is not None:
            next_before_log_id = int(before_log_id)
        else:
            next_before_log_id = 0

        has_more_before = (
            state.db.has_logs_before(run_id, next_before_log_id)
            if next_before_log_id > 0
            else False
        )

        return JSONResponse(
            {
                "run": run,
                "lines": lines,
                "next_after_log_id": next_after_log_id,
                "next_before_log_id": next_before_log_id,
                "has_more_before": has_more_before,
            }
        )

    @app.get("/api/events/stream")
    async def events_stream(
        request: Request,
        after_event_id: int = Query(0, ge=0),
    ) -> StreamingResponse:
        """Server-Sent Events stream for near-real-time dashboard updates."""
        header_last_event: Optional[str] = request.headers.get("last-event-id")
        cursor = after_event_id
        if header_last_event is not None:
            try:
                cursor = max(cursor, int(header_last_event))
            except ValueError:
                pass

        async def event_generator():
            nonlocal cursor
            heartbeat_counter = 0
            try:
                while True:
                    state = state_provider()
                    if state.shutting_down:
                        break

                    if await request.is_disconnected():
                        break

                    events = state.db.get_events_after(cursor, limit=200)
                    if events:
                        for event in events:
                            cursor = int(event["event_id"])
                            data = json.dumps(event, ensure_ascii=False)
                            yield f"id: {cursor}\nevent: {event['type']}\ndata: {data}\n\n"
                        heartbeat_counter = 0
                        # Keep loop cooperative for cancellation/shutdown.
                        await asyncio.sleep(0)
                    else:
                        heartbeat_counter += 1
                        if heartbeat_counter >= sse_heartbeat_every_empty_polls:
                            yield ": heartbeat\n\n"
                            heartbeat_counter = 0
                        await asyncio.sleep(sse_poll_interval_seconds)
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    return {
        "run_logs": run_logs,
        "events_stream": events_stream,
    }
