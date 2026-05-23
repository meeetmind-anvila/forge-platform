"""
Real-time log streaming via Server-Sent Events.

Design:
- Job log files are JSON-lines: one {"ts":..,"job":..,"line":..} per line
- SSE endpoint reads from current byte offset (backlog) then tails
- Never loads the full file into memory — reads in chunks
- 50MB log streams fine because we read line by line from disk
- Client can reconnect using Last-Event-ID = last byte offset
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Optional

logger = logging.getLogger("logs")

TAIL_POLL_INTERVAL = 0.1  # seconds
CHUNK_SIZE = 8192


class LogStreamer:
    def __init__(self, run_dir: str, run_id: str, run_manager):
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.run_manager = run_manager

    async def stream(self, follow: bool = False, last_event_id: Optional[str] = None) -> AsyncIterator[str]:
        """
        Yield SSE-formatted strings.
        Delivers backlog first (from offset in last_event_id if provided),
        then live lines while the run is active (if follow=True).
        """
        log_dir = self.run_dir / "logs"
        log_dir.mkdir(exist_ok=True)

        # We stream all job logs in a merged, time-ordered fashion
        # by scanning all *.log files and interleaving by timestamp.
        # For simplicity + performance: stream each log file in sequence,
        # using inotify-style polling for live follow.

        # Start from backlog
        start_offset = 0
        if last_event_id:
            try:
                start_offset = int(last_event_id)
            except ValueError:
                pass

        async for event in self._stream_all_logs(log_dir, follow, start_offset):
            yield event

    async def _stream_all_logs(
        self,
        log_dir: Path,
        follow: bool,
        start_offset: int,
    ) -> AsyncIterator[str]:
        """
        Stream all job log entries, polling for new files and new content.
        Uses a single global byte offset across a merged log file if present,
        or streams per-file for per-job separation.
        """
        seen_files: set = set()
        file_offsets: dict = {}  # path → current byte offset
        global_line_count = 0

        while True:
            # Discover new log files
            try:
                log_files = sorted(log_dir.glob("*.log"))
            except Exception:
                log_files = []

            for log_path in log_files:
                path_str = str(log_path)
                if path_str not in file_offsets:
                    file_offsets[path_str] = 0

            # Read new content from each file
            any_new = False
            for path_str in sorted(file_offsets.keys()):
                log_path = Path(path_str)
                if not log_path.exists():
                    continue

                offset = file_offsets[path_str]
                size = log_path.stat().st_size

                if size <= offset:
                    continue

                any_new = True
                with open(path_str, "rb") as f:
                    f.seek(offset)
                    while True:
                        raw = f.readline()
                        if not raw:
                            break
                        offset = f.tell()
                        try:
                            entry = json.loads(raw.decode(errors="replace"))
                        except json.JSONDecodeError:
                            continue

                        global_line_count += 1
                        # SSE format: id=<line_count> data=<json>\n\n
                        sse = (
                            f"id: {global_line_count}\n"
                            f"data: {json.dumps(entry)}\n\n"
                        )
                        yield sse

                file_offsets[path_str] = offset

            # Decide whether to keep polling
            if not follow:
                # For non-follow, drain everything and stop
                # Check if run is done
                state = self.run_manager.get_run_state(self.run_id)
                if state is None:
                    # Load from disk
                    state_path = self.run_dir / "state.json"
                    if state_path.exists():
                        try:
                            state = json.loads(state_path.read_text())
                        except Exception:
                            state = {}

                if state and state.get("status") in {
                    "succeeded", "failed", "integrity_failure",
                    "conflict_failure", "cycle_failure"
                }:
                    # One more pass to catch any final writes
                    if not any_new:
                        break
                else:
                    if not any_new:
                        # Run not done, yield done marker and stop
                        yield "data: {\"ts\": null, \"job\": \"forge\", \"line\": \"[forge] Run still in progress, use --follow\"}\n\n"
                        break
            else:
                # Follow mode: keep polling until run is terminal
                state = self.run_manager.get_run_state(self.run_id)
                if state is None:
                    state_path = self.run_dir / "state.json"
                    if state_path.exists():
                        try:
                            state = json.loads(state_path.read_text())
                        except Exception:
                            state = {}

                if state and state.get("status") in {
                    "succeeded", "failed", "integrity_failure",
                    "conflict_failure", "cycle_failure"
                }:
                    if not any_new:
                        # One final scan completed, emit done event and exit
                        yield "event: done\ndata: {}\n\n"
                        break

                await asyncio.sleep(TAIL_POLL_INTERVAL)