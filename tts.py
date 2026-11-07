import asyncio
import io
import logging
import re
import time
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass

import numpy as np

from service_clients import tts_client

log = logging.getLogger(__name__)

# --- Markdown -> speech normalization --------------------------------------
# The LLM frequently emits light markdown (**bold**, `code`, "1)" lists,
# [links](url), ## headings). The TTS engine receives the `input` string
# verbatim, so stray markup gets verbalized ("asterisk asterisk Important
# news") or produces undefined pauses. We strip it here, at the single choke
# point every TTS caller passes through.
#
# This is deliberately LIGHTER than reader_text.clean_for_speech, which also
# strips academic citations and LaTeX math for converted journal PDFs. Here we
# only touch conversational markdown (a reply's LaTeX is rewritten first, by
# `speakable`) and never delete characters that carry
# meaning in speech (e.g. a lone "*" in "3 * 4" is left alone).

_MD_IMAGE = re.compile(r'!\[([^\]]*)\]\([^)]*\)')          # ![alt](url) -> alt
_MD_LINK = re.compile(r'\[([^\]]+)\]\([^)]*\)')            # [text](url) -> text
_MD_BOLD = re.compile(r'(\*\*|__)(?=\S)(.+?)(?<=\S)\1')    # **x** / __x__ -> x
_MD_ITALIC = re.compile(r'(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\1(?!\w)')  # *x* / _x_ -> x
_MD_CODE = re.compile(r'`+\s*([^`]+?)\s*`+')               # `code` -> code
_MD_HEADING = re.compile(r'^\s*#{1,6}\s+', re.MULTILINE)   # ## Heading -> Heading
_MD_LIST = re.compile(r'^[ \t]*(?:[-*+]|\d+[.)])\s+', re.MULTILINE)  # -, *, 1., 1) markers
# Orphan emphasis: a bold/italic span can be split across streamed sentences
# (agent.py yields sentence-by-sentence), leaving a dangling "**" or a lone "*"
# with no matching partner in this chunk. A run of 2+ asterisks is always
# markup; a single "*" is stripped only when it hugs a word edge, so
# multiplication ("3 * 4", "2*3") survives.
_MD_ORPHAN_BOLD = re.compile(r'\*{2,}')
_MD_ORPHAN_EMPH = re.compile(r'(?<!\S)\*(?=\S)|(?<=\S)\*(?!\S)')
_WS = re.compile(r'[ \t]{2,}')
# A Reader chat citation, "[§14]" or "[§3, §7]": a link in the panel, silence when spoken.
_CITE = re.compile(r'\s*\[§[^\[\]]{0,40}\]')
# "et al." in citations: the engine reads the period as a sentence end and pauses
# mid-sentence. Only the text sent to TTS changes; the document keeps the period.
_ET_AL = re.compile(r'\b(et al)\.', re.IGNORECASE)
# Dotted initialisms (U.S., U.K.): Phonon garbles one that ends the text ("published
# by U.S." came back as "by you later state" with extra babble) and reads "US" cleanly.
# A multiplier: Phonon reads "4.6x" as "4.6 back" and "10x" as "10 bikes". Only an x that ends
# the number ("2x2" and "1920x1080" already read as "2 by 2"; "0x1F" is untouched).
_MULTIPLIER = re.compile(r'(\d(?:[\d,]*\d)?(?:\.\d+)?)[x\u00d7](?!\w|\.\d)')
_INITIALISM = re.compile(r'\b(?:[A-Z]\.){2,}(?=[\s,;:)]|$)')
# Number ranges: Phonon reads "2–8%" and "2– 8%" as "two eight percent". An en or em dash
# between digits is always a range ("2–8%", "2 – 8", "1990–2000", "pp. 12–15"), spaced or not.
# A plain hyphen is only a range where nothing else is likely: before a percent sign
# ("5-10%"), after a page marker ("pp. 12-15"), or between two years ("1990-2000"). Not in
# general, because it is also subtraction ("5 - 3"), a sign ("-5"), a name ("COVID-19"),
# an ISO date ("2026-10-31") or a phone/ID chain ("1-800-555"); a hyphen or word on
# either side of the pair rules it out.
_DASH_RANGE = re.compile(r'(\d)[ \t]*[–—][ \t]*(?=\d)')
_NUM = r'\d+(?:\.\d+)?'
_HYPHEN_RANGE = re.compile(
    rf'(?<![\w.\-−])({_NUM})(?: - |-)(?=(?:{_NUM})%)'                  # 5-10%, 5 - 10%
    r'|(?<=\bpp\.)( ?\d+)-(?=\d+(?![\w\-]))'                                # pp. 12-15
    r'|(?<![\w.\-])((?:1[5-9]|20)\d\d)-(?=(?:1[5-9]|20)\d\d(?![\w\-]))'      # 1990-2000
)

# Hyphens between words: Phonon reads some as "dash", depending on the words around
# them ("document-level" as "document dash level", "domain-adaptive" only in a long
# sentence). Joined by a hyphen or an unspaced en/em dash, the words are read as two;
# a hanging hyphen ("domain- and task-specific") is dropped. Letters on both sides
# only: "COVID-19", "1-800" and ranges are left to the rules above.
_WORD_HYPHEN = re.compile(r'(?<=[A-Za-z])[-–—](?=[A-Za-z])')
_HANGING_HYPHEN = re.compile(r'(?<=[A-Za-z])-(?=\s+(?:and|or|to|nor)\b)')
# Decimals: in a long sentence Phonon loses the point ("57.7%" read as "57. 7%" or
# "57 pus 7%"). Spelled out, the way it is said: "57 point 7", fraction digits one by
# one ("0.05" is "0 point 0 5"). Not money ("$3.50"), versions ("v1.2", "1.2.3").
_DECIMAL = re.compile(r'(?<![\w.$£€])(\d+)\.(\d+)(?![\d.]*\.\d)')
# A quarter before a year: Phonon reads the year in "Q1 2019" digit by digit ("Q one two
# zero one nine"), and a comma ("Q1, 2019") does not help; "Q1 of 2019" reads the year
# normally (measured 2026-11-03: the year took 0.8-1.3 s after "Q1", 0.4-0.6 s as plain
# "in 2019" or "Q1 of 2019", three voices). "2019 Q1" already reads fine.
_QUARTER_YEAR = re.compile(r'\b(Q[1-4]),?[ \t]+((?:19|20)\d\d)\b(?!\.\d)')

# "housing" (and "rousing", "arousing"): Phonon reads it as "hue-zing" whenever it is
# capitalised or starts a sentence ("Public Housing" -> "Public Husing", "Ontario Housing
# Corporation" -> "Ontario Hues Incorporation", "Housing is..." -> "Husing is..."); only
# lower case mid-sentence is right, and lower-casing cannot fix a sentence start. Respelled
# "howzing", it is heard as "housing" in every position and voice tested (2026-11-02).
# "Rehousing", "Houses", "House" read fine and are left alone.
_OUSING = re.compile(r'\b(h|r|ar)(ousing)(s?)\b', re.IGNORECASE)


# "pi": Phonon clips it to "p" at the end of a phrase ("from negative pi to p of e", heard
# through Whisper with Delia, 2026-11-07, and by Dave). Respelled "pie", it is heard as "pi"
# in Delia and Imogen at 1x, and in Delia at 1.4x. All-caps "PI" (a principal investigator) is left alone.
_PI = re.compile(r'\b[Pp]i\b|π')


def _respell_ousing(m: re.Match) -> str:
    # Keep only the first letter's case: all-caps "HOWZING" is heard as "pausing".
    return m.group(1)[0] + m.group(1)[1:].lower() + 'owzing' + m.group(3).lower()


def _say_decimal(m: re.Match) -> str:
    return f"{m.group(1)} point {' '.join(m.group(2))}"


def _undot(m: re.Match) -> str:
    return m.group(0).replace('.', '') + ('.' if m.end() == len(m.string) else '')


def speechify(text: str) -> str:
    """Strip conversational markdown so the TTS engine speaks prose, not markup.

    Idempotent: already-clean text (e.g. reader output) passes through
    unchanged. Never returns an empty string for non-empty input.
    """
    if not text:
        return text
    cleaned = _CITE.sub('', text)
    cleaned = _MD_IMAGE.sub(r'\1', cleaned)
    cleaned = _MD_LINK.sub(r'\1', cleaned)
    cleaned = _MD_BOLD.sub(r'\2', cleaned)     # bold before italic: ** contains *
    cleaned = _MD_ITALIC.sub(r'\2', cleaned)
    cleaned = _MD_ORPHAN_BOLD.sub('', cleaned)  # dangling ** from a split/unclosed span
    cleaned = _MD_ORPHAN_EMPH.sub('', cleaned)  # lone * hugging a word edge
    cleaned = _MD_CODE.sub(r'\1', cleaned)
    cleaned = _MD_HEADING.sub('', cleaned)
    cleaned = _MD_LIST.sub('', cleaned)        # only line-leading markers (real lists)
    cleaned = _ET_AL.sub(r'\1', cleaned)
    cleaned = _MULTIPLIER.sub(r'\1 times', cleaned)
    cleaned = _DASH_RANGE.sub(r'\1 to ', cleaned)
    cleaned = _HYPHEN_RANGE.sub(lambda m: next(g for g in m.groups() if g) + ' to ', cleaned)
    cleaned = _HANGING_HYPHEN.sub('', cleaned)
    cleaned = _WORD_HYPHEN.sub(' ', cleaned)
    cleaned = _DECIMAL.sub(_say_decimal, cleaned)
    cleaned = _QUARTER_YEAR.sub(r'\1 of \2', cleaned)
    cleaned = _OUSING.sub(_respell_ousing, cleaned)
    cleaned = _PI.sub('pie', cleaned)
    cleaned = _INITIALISM.sub(_undot, cleaned.rstrip())
    cleaned = _WS.sub(' ', cleaned).strip()
    return cleaned or text.strip()


# --- A reply's math -> speech ------------------------------------------------
# A reply keeps its LaTeX for the screen and history; only the copy handed to TTS is
# rewritten, one sentence at a time, by the reader's math conversion.

# The rules every renderer of a reply uses (static/math-render.js, Android's
# MathDelimiters): an inline $ has no space just inside it and no digit right after
# its closing one, so "$5 and $10" is not math.
_MATH_SPAN = re.compile(
    r'\$\$.+?\$\$'
    r'|\\\[.+?\\\]'
    r'|\\\(.+?\\\)'
    r'|\\begin\{([a-zA-Z]+\*?)\}.*?\\end\{\1\}'
    r'|(?<![\\$])\$(?![\s$])(?:[^$\n]|\n(?!\n))*?[^\s\\$]\$(?!\d)',
    re.DOTALL,
)
_LATEX_COMMAND = re.compile(r'\\[a-zA-Z]{2,}')

# The rewrite normally takes 0.2-0.4 s, and a reply waits on it, so a stuck endpoint
# must cost one sentence a short wait and the rest of the reply nothing.
# Canadian "zed". Only in a sentence that held math, so "Gen Z" elsewhere keeps its "zee";
# the rewrite prompt alone did not hold (the 12B wrote "where Z has", 2026-11-07).
_LETTER_Z = re.compile(r'\b[Zz]\b')

SPEECH_MATH_TIMEOUT_S = 4.0
SPEECH_MATH_COOLDOWN_S = 60.0
_speech_math_down_until = 0.0


def breaks_outside_math(pattern: re.Pattern, text: str) -> list[re.Match]:
    """Matches of a sentence-break pattern, leaving out any inside an equation."""
    spans = [m.span() for m in _MATH_SPAN.finditer(text)]
    return [m for m in pattern.finditer(text) if not any(a <= m.start() < b for a, b in spans)]


async def speakable(text: str) -> str:
    """A sentence of a reply as it should be read aloud: its math in spoken English.

    Text without math comes back untouched and at no cost. A failed or slow rewrite
    falls back to the local strip_latex; speech must never hold up on it for long.
    """
    global _speech_math_down_until
    if not (_MATH_SPAN.search(text) or _LATEX_COMMAND.search(text)):
        return text
    from reader_text import _llm_convert_math, strip_latex

    cleaned = _CITE.sub('', text)  # a tag means nothing to the rewriter, and is not spoken
    if time.monotonic() < _speech_math_down_until:
        return _LETTER_Z.sub('zed', strip_latex(cleaned))
    try:
        spoken = await asyncio.wait_for(_llm_convert_math(None, cleaned), SPEECH_MATH_TIMEOUT_S)
        return _LETTER_Z.sub('zed', spoken)
    except asyncio.TimeoutError:
        _speech_math_down_until = time.monotonic() + SPEECH_MATH_COOLDOWN_S
        log.warning("Math-to-speech took over %.0f s; using the local rewrite for %.0f s",
                    SPEECH_MATH_TIMEOUT_S, SPEECH_MATH_COOLDOWN_S)
        return _LETTER_Z.sub('zed', strip_latex(cleaned))


# --- A sentence with a large formula -> clips --------------------------------
# A large formula is its own clip, so the listener's player can slow it down (the
# speech_clip frame, "Math speed" on /). Dave chose this over slowing the whole sentence
# by ear (2026-11-07). A passing $n$ stays in its sentence, at the normal speed.
_LARGE_MATH_COMMAND = re.compile(r'\\(?:int|sum|prod|frac|sqrt|begin)(?![a-zA-Z])')
LARGE_MATH_MIN_CHARS = 15
_LEADING_PUNCTUATION = re.compile(r'\s*[.,;:!?]*')

# Each clip comes with about 0.5 s of silence before it and 0.6 s after: fine between
# sentences, too long inside one. Keep what the sample Dave picked had, about 0.25 s.
SEAM_LEAD_S = 0.15
SEAM_TAIL_S = 0.10
_SEAM_WINDOW_S = 0.02
_SEAM_SILENCE_RMS = 0.01


@dataclass
class SpeechClip:
    text: str
    math: bool = False


def _is_large_math(span: str) -> bool:
    return span.startswith(('$$', '\\[')) or bool(_LARGE_MATH_COMMAND.search(span)) or len(span) >= LARGE_MATH_MIN_CHARS


async def speech_clips(sentence: str) -> list[SpeechClip]:
    """A sentence as clips to speak in order: large formulas apart from the words around them."""
    clips: list[SpeechClip] = []
    pos = 0
    for match in _MATH_SPAN.finditer(sentence):
        if not _is_large_math(match.group()):
            continue
        prose = sentence[pos:match.start()]
        if clips and clips[-1].math:  # punctuation after a formula is read with it
            punctuation = _LEADING_PUNCTUATION.match(prose).group()
            clips[-1].text += punctuation.strip()
            prose = prose[len(punctuation):]
        if re.search(r'\w', _CITE.sub('', prose)):
            clips.append(SpeechClip(prose.strip().rstrip(':').rstrip()))
        clips.append(SpeechClip(match.group(), math=True))
        pos = match.end()
    if not clips:
        return [SpeechClip(await speakable(sentence))]
    prose = sentence[pos:]
    if clips[-1].math:
        punctuation = _LEADING_PUNCTUATION.match(prose).group()
        clips[-1].text += punctuation.strip()
        prose = prose[len(punctuation):]
    if re.search(r'\w', _CITE.sub('', prose)):
        clips.append(SpeechClip(prose.strip()))
    spoken = await asyncio.gather(*(speakable(clip.text) for clip in clips))
    for clip, text in zip(clips, spoken):
        clip.text = text
    return clips


def trim_seam(wav_bytes: bytes, *, lead: bool, tail: bool) -> bytes:
    """Shorten the silence at a clip's joined ends. Anything but 16-bit WAV passes through."""
    try:
        with wave.open(io.BytesIO(wav_bytes)) as reader:
            params = reader.getparams()
            frames = reader.readframes(reader.getnframes())
        if params.sampwidth != 2:
            return wav_bytes
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768
        window = max(1, int(params.framerate * params.nchannels * _SEAM_WINDOW_S))
        count = len(samples) // window
        if not count:
            return wav_bytes
        rms = np.sqrt((samples[:count * window].reshape(count, window) ** 2).mean(axis=1))
        voiced = np.flatnonzero(rms >= _SEAM_SILENCE_RMS)
        if not len(voiced):
            return wav_bytes
        unit = params.framerate * params.nchannels
        start = max(0, int(voiced[0]) * window - int(SEAM_LEAD_S * unit)) if lead else 0
        end = min(len(samples), (int(voiced[-1]) + 1) * window + int(SEAM_TAIL_S * unit)) if tail else len(samples)
        start -= start % params.nchannels
        end -= end % params.nchannels
        out = io.BytesIO()
        with wave.open(out, 'wb') as writer:
            writer.setparams(params)
            writer.writeframes(frames[start * 2:end * 2])
        return out.getvalue()
    except (wave.Error, EOFError, ValueError):
        return wav_bytes


async def speech_audio(sentence: str, voice: str | None = None) -> AsyncIterator[tuple[bytes, bool]]:
    """A sentence of a reply as (wav, is_math) clips, in order."""
    clips = await speech_clips(sentence)
    for i, clip in enumerate(clips):
        wav_bytes = await synthesize(clip.text, voice=voice)
        if len(clips) > 1:
            wav_bytes = trim_seam(wav_bytes, lead=i > 0, tail=i < len(clips) - 1)
        yield wav_bytes, clip.math


async def synthesize(text: str, voice: str | None = None) -> bytes:
    """POST text to TTS (Phonon); a voice it lacks is sent as the default.

    Markdown is normalized to speakable prose (see `speechify`) before the
    text reaches the engine.
    """
    return await tts_client.synthesize(speechify(text), voice=voice)
