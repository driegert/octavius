import re

from service_clients import tts_client

# --- Markdown -> speech normalization --------------------------------------
# The LLM frequently emits light markdown (**bold**, `code`, "1)" lists,
# [links](url), ## headings). Voxtral/Kokoro receive the `input` string
# verbatim, so stray markup gets verbalized ("asterisk asterisk Important
# news") or produces undefined pauses. We strip it here, at the single choke
# point every TTS caller passes through.
#
# This is deliberately LIGHTER than reader_text.clean_for_speech, which also
# strips academic citations and LaTeX math for converted journal PDFs. Here we
# only touch conversational markdown and never delete characters that carry
# meaning in speech (e.g. a lone "*" in "3 * 4" is left alone).

_MD_IMAGE = re.compile(r'!\[([^\]]*)\]\([^)]*\)')          # ![alt](url) -> alt
_MD_LINK = re.compile(r'\[([^\]]+)\]\([^)]*\)')            # [text](url) -> text
_MD_BOLD = re.compile(r'(\*\*|__)(?=\S)(.+?)(?<=\S)\1')    # **x** / __x__ -> x
_MD_ITALIC = re.compile(r'(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\1(?!\w)')  # *x* / _x_ -> x
_MD_CODE = re.compile(r'`+\s*([^`]+?)\s*`+')               # `code` -> code
_MD_HEADING = re.compile(r'^\s*#{1,6}\s+', re.MULTILINE)   # ## Heading -> Heading
_MD_LIST = re.compile(r'^[ \t]*(?:[-*+]|\d+[.)])\s+', re.MULTILINE)  # -, *, 1., 1) markers
_WS = re.compile(r'[ \t]{2,}')


def speechify(text: str) -> str:
    """Strip conversational markdown so the TTS engine speaks prose, not markup.

    Idempotent: already-clean text (e.g. reader output) passes through
    unchanged. Never returns an empty string for non-empty input.
    """
    if not text:
        return text
    cleaned = _MD_IMAGE.sub(r'\1', text)
    cleaned = _MD_LINK.sub(r'\1', cleaned)
    cleaned = _MD_BOLD.sub(r'\2', cleaned)     # bold before italic: ** contains *
    cleaned = _MD_ITALIC.sub(r'\2', cleaned)
    cleaned = _MD_CODE.sub(r'\1', cleaned)
    cleaned = _MD_HEADING.sub('', cleaned)
    cleaned = _MD_LIST.sub('', cleaned)        # only line-leading markers (real lists)
    cleaned = _WS.sub(' ', cleaned).strip()
    return cleaned or text.strip()


async def synthesize(text: str, voice: str | None = None) -> bytes:
    """POST text to TTS. Kokoro voices go direct to Kokoro; all others go
    through Voxtral-with-Kokoro-fallback via the circuit breaker.

    Markdown is normalized to speakable prose (see `speechify`) before the
    text reaches the engine.
    """
    return await tts_client.synthesize(speechify(text), voice=voice)
