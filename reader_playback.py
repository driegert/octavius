"""Reader audio playback streaming."""

import asyncio
import json
import logging
from pathlib import Path

from db import connect_db
from tts import synthesize

from reader_store import get_document, load_speech_data, update_document

log = logging.getLogger(__name__)


async def stream_reader_audio(
    ws,
    doc_id: int,
    db_path: str | Path,
    chunk_index: int = 0,
    sentence_index: int = 0,
    voice: str | None = None,
):
    """Stream TTS audio for a document over WebSocket."""
    current_chunk_index = chunk_index
    current_sentence_index = sentence_index

    with connect_db(Path(db_path)) as conn:
        doc = get_document(conn, doc_id)
        if not doc or doc["status"] != "ready":
            await ws.send_text(json.dumps({"type": "status", "text": "Document not ready."}))
            return

        speech = load_speech_data(doc)
        if not speech:
            await ws.send_text(json.dumps({"type": "status", "text": "Speech file not found."}))
            return

        chunks = speech["chunks"]
        total_chunks = len(chunks)
        total_sentences = speech.get("total_sentences", 0)

        sentence_global = 0
        for chunk in chunks[:chunk_index]:
            sentence_global += len(chunk["sentences"])
        sentence_global += sentence_index

        try:
            for current_chunk_index in range(chunk_index, total_chunks):
                chunk = chunks[current_chunk_index]
                sentences = chunk["sentences"]
                start_sentence = sentence_index if current_chunk_index == chunk_index else 0

                for current_sentence_index in range(start_sentence, len(sentences)):
                    sentence = sentences[current_sentence_index]
                    if not sentence.strip():
                        sentence_global += 1
                        continue

                    await ws.send_text(json.dumps({
                        "type": "reader_position",
                        "chunk_index": current_chunk_index,
                        "sentence_index": current_sentence_index,
                        "total_chunks": total_chunks,
                        "total_sentences": total_sentences,
                        "sentence_global": sentence_global,
                        "heading": chunk.get("heading"),
                        "sentence_text": sentence,
                    }))

                    update_document(conn, doc_id, last_chunk=current_chunk_index, last_sentence=current_sentence_index)

                    wav_bytes = await synthesize(sentence, voice=voice)
                    await ws.send_bytes(wav_bytes)

                    sentence_global += 1

            update_document(conn, doc_id, last_chunk=0, last_sentence=0)
            await ws.send_text(json.dumps({"type": "reader_audio_done"}))

        except asyncio.CancelledError:
            update_document(conn, doc_id, last_chunk=current_chunk_index, last_sentence=current_sentence_index)
            log.info(
                "Reader: playback paused at chunk %d, sentence %d",
                current_chunk_index,
                current_sentence_index,
            )
