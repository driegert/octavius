"""Reader playback frames (reader_play/heard/pause/stop) for a WebSocket session.

A mixin composed into ``websocket_session.WebSocketSessionHandler``; the
session state lives on ``self.state`` (see ``websocket_session``).
"""
from __future__ import annotations

import asyncio
import logging

from reader_playback import ReaderPacer, save_heard_position, stream_reader_audio

log = logging.getLogger(__name__)


class ReaderPlaybackMixin:
    async def handle_reader_play(self, data: dict):
        await self.cancel_reader_task()
        self.state.reader_seq = data.get("seq", 0)
        # `download` (strict, like `paced`): the client caches the audio without playing it, so the
        # stream must not touch the reading place. No pacer either: nothing would send reader_heard.
        download = data.get("download") is True
        self.state.reader_pacer = ReaderPacer(self.state.reader_seq) if data.get("paced") is True and not download else None
        self.state.reader_task = asyncio.create_task(
            stream_reader_audio(
                self.ws,
                data["doc_id"],
                self.state.history.db_path,
                chunk_index=data.get("chunk_index", 0),
                sentence_index=data.get("sentence_index", 0),
                voice=data.get("voice", self.state.voice),
                seq=data.get("seq", 0),
                pacer=self.state.reader_pacer,
                save_place=not download,
            )
        )


    async def handle_reader_heard(self, data: dict):
        # Sent by a paced client as each sentence starts playing. Stale frames from
        # an earlier play (after a seek) are dropped so they can't move the place back.
        # Only a live paced stream takes them: once it has ended (and written "finished")
        # a late or duplicate frame must not move the place back.
        task = self.state.reader_task
        if not self.state.reader_pacer or data.get("seq") != self.state.reader_seq or not task or task.done():
            return
        # Save before waking the stream: on the last sentence it then writes "finished" after this.
        self._save_heard_position(data)
        if self.state.reader_pacer:
            self.state.reader_pacer.report(data.get("seq"), data.get("sentence_global"))


    async def handle_reader_pause(self, data: dict):
        await self.cancel_reader_task()
        self._save_heard_position(data)


    async def handle_reader_stop(self, data: dict):
        await self.cancel_reader_task()
        self._save_heard_position(data)


    def _save_heard_position(self, data: dict) -> None:
        # Optional fields (2026-10-30): the sentence the client was hearing. Saved after
        # the cancel, so it replaces the further-ahead place the stream recorded.
        if "chunk_index" in data:
            save_heard_position(self.state.history.db_path, data.get("doc_id"),
                                data.get("chunk_index"), data.get("sentence_index"))


    async def cancel_reader_task(self):
        if self.state.reader_task and not self.state.reader_task.done():
            self.state.reader_task.cancel()
            try:
                await self.state.reader_task
            except (asyncio.CancelledError, Exception):
                pass
