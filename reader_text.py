"""Reader markdown chunking and speech-preparation pipeline."""

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import unquote

import httpx

from service_clients import llm_client, vision_llm_client
from settings import settings

from reader_store import (
    get_document,
    is_replaced,
    load_appendices,
    load_speech_data,
    save_appendix,
    sentences_before,
    update_document,
)

log = logging.getLogger(__name__)

READER_PATH = Path(settings.reader.directory)
READER_PATH.mkdir(parents=True, exist_ok=True)

# A sentence ends at . ! or ? and whitespace, except after an abbreviation academic
# writing uses mid-sentence: each piece is its own TTS call, so a false break is a
# pause (and a highlight jump) in the middle of "Smith et al. (2020) found". Nor does
# one end before a lower-case word ("by U.S. state governors") or after a capital
# initial ("U.S. Senate", "D. J. Thomson"). "et al." can also end a sentence, so it
# still breaks before a capital letter.
SENTENCE_END = re.compile(
    r'(?<=[.!?])'
    r'(?<!\be\.g\.)(?<!\bE\.g\.)(?<!\bi\.e\.)(?<!\bI\.e\.)(?<!\bcf\.)(?<!\bCf\.)'
    r'(?<!\bvs\.)(?<!\bFig\.)(?<!\bFigs\.)(?<!\bEq\.)(?<!\bEqs\.)(?<!\b[A-Z]\.)'
    r'(?:(?<!\bet al\.)\s+(?![a-z])|(?<=\bet al\.)\s+(?=[A-Z]))'
)
HEADING_RE = re.compile(r'^(#{1,3})\s+(.+)$', re.MULTILINE)
MATH_RE = re.compile(
    r'\$\$?.+?\$\$?'          # $inline$ / $$display$$
    r'|\\\(.+?\\\)'           # \(inline\)
    r'|\\\[.+?\\\]'           # \[display\]
    r'|\\begin\{[a-z]+\*?\}', # equation/align/... environments
    re.DOTALL,
)
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_LEFTOVER_LATEX = re.compile(r'\\(?:frac|sum|hat|bar|begin|end|left|right|math[a-z]+|[a-z]*sqrt)\b|^\s*Rewritten:', re.MULTILINE)

MATH_TO_SPEECH_PROMPT = """Rewrite the following text, replacing all LaTeX math expressions with natural spoken English. Keep all surrounding non-math text exactly as-is. Only change the math parts.

Examples:
- $x^2$ becomes "x squared"
- $\\hat{\\beta}$ becomes "beta hat"
- $\\bar{x}$ becomes "x bar"
- $\\frac{a}{b}$ becomes "a over b"
- $\\sum_{i=1}^{n} x_i$ becomes "the sum, from i equals 1 to n, of x sub i"
- $\\int_0^1 f(x) dx$ becomes "the integral, from 0 to 1, of f of x, dee x"
- $X_t = \\int_{-\\pi}^{\\pi} e^{it\\lambda} dZ(\\lambda)$ becomes "X sub t equals the integral, from negative pi to pi, of e to the i t lambda, dee zed of lambda" (the commas are the pauses a person makes: after "the integral" or "the sum", after the limits, and before the differential)
- $\\alpha$ becomes "alpha"
- $p < 0.05$ becomes "p less than 0.05"
- $x_i$ becomes "x sub i"
- $\\frac{n(n+1)}{2}$ becomes "n times the quantity n plus 1, all over 2"
- $\\frac{a - b}{c / d}$ becomes "the quantity a minus b, all over c over d" (a numerator or denominator with more than one term must be grouped out loud, or the formula changes meaning)
- $|x|^2$ and $\\left| \\sum_t a_t \\right|^2$ become "the absolute value of x, squared" and "the squared magnitude of the sum over t of a sub t"; "magnitude" only ever means absolute value bars, so $(\\sum_i x_i)^2$ becomes "the square of the sum over i of x sub i"
- $(X^T X)^{-1} X^T y$ becomes "the inverse of the quantity X transpose X, times X transpose y", and $x^{-1}$ becomes "x inverse" (a power of negative 1 is an inverse, never "over 1")
- $e^{it\\lambda}$ becomes "e to the i t lambda", and $2\\pi f t$ becomes "2 pi f t" (symbols multiplied together are read side by side: no "times", no "to the power of")
- $3 \\cdot 4$ and $a \\cdot b$ become "3 times 4" and "a times b" (never "dot")
- $dZ(\\lambda)$ becomes "dee zed of lambda", and $dx$ becomes "dee x" (the d of a differential is its own word)
- $v_t^{(k)}$ becomes "v sub t superscript k" (a superscript in parentheses is a label, not a power)
- A multi-line equation (aligned, & and \\\\) is read as one continuous statement: "... equals ..., which equals ..."

Spoken math has no brackets, so a listener must be able to write the formula back down from your words. A comma is a pause that closes a group; "the quantity" opens one. Say every sign and every factor.
- Never pause inside a symbol ("x sub t", "mu hat") or between plain multiplied symbols.
- Close a fraction, root, exponent, sum or integral with a comma whenever anything follows it:
  $\\frac{a}{b} + c$ becomes "a over b, plus c"; $\\sqrt{a} + b$ becomes "the square root of a, plus b"; $\\sqrt{a + b} + c$ becomes "the square root of the quantity a plus b, plus c"; $e^{-x^2/2} + 1$ becomes "e to the negative x squared over 2, plus 1"; $e^{-x^2}/2$ becomes "e to the negative x squared, all over 2"
- A denominator with several factors is grouped: $\\frac{a}{b c}$ becomes "a over the quantity b c", and $\\frac{a}{b} c$ becomes "a over b, times c"; $\\frac{a + b}{c} d$ becomes "the quantity a plus b, all over c, times d"
- A long formula: $\\frac{1}{\\sqrt{2\\pi\\sigma^2}} \\exp\\left(-\\frac{(x-\\mu)^2}{2\\sigma^2}\\right)$ becomes "1 over the square root of 2 pi sigma squared, times e to the negative, the quantity x minus mu, squared, all over 2 sigma squared" ($\\exp$ is always said as "e to the")
- Nested sums: $\\frac{1}{K} \\sum_{k=0}^{K-1} \\left| \\sum_{t=0}^{N-1} v_t x_t \\right|^2$ becomes "1 over K, times the sum, from k equals 0 to K minus 1, of the squared magnitude, of the sum, from t equals 0 to N minus 1, of v sub t, x sub t"

Output ONLY the rewritten text, once. Do not repeat the original, do not label your answer, no preamble, no explanation."""


# A converted PDF breaks a paragraph at every page boundary, mid-sentence, and a
# figure or table that sat at the top of the next page can land between the two
# halves. Each half then becomes its own chunk, so the sentence is spoken in two
# pieces. A paragraph is rejoined to the next prose paragraph when it does not end
# like a sentence and the next one starts in lower case (or it ended on a comma).
# Floats found in between (captions, images, tables, page numbers) move after it.
# Mid-sentence: ends in a word, a comma, or a hyphen (not code's } or ;).
_OPEN_END = re.compile(r'[A-Za-z0-9,-]$')
_FLOAT = re.compile(
    r'^(?:\**_*(?:Fig\.|Figs\.|Figure|Table|Tab\.)\s*\d'   # caption
    r'|!\['                                               # image
    r'|\|'                                                # table
    r'|\d{1,4}$'                                          # page number
    r'|<[^>]+>\s*(?:</[^>]+>)?$)'                         # bare html, e.g. a page anchor
)
_NOT_PROSE = re.compile(r'^(?:#|[-*+•]\s|\d+[.)]\s|\$\$|\\\[|\\begin|```|>|\|)')
_MAX_FLOATS = 4


def _is_prose(para: str) -> bool:
    para = para.lstrip()
    return not _NOT_PROSE.match(para) and not _FLOAT.match(para)


def join_page_breaks(markdown: str) -> str:
    paras = [p.rstrip() for p in re.split(r'\n\s*\n', markdown) if p.strip()]
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(paras):
        para, floats = paras[i], []
        i += 1
        # never inside or across a fenced code block
        fenced = in_fence or "```" in para
        in_fence ^= para.count("```") % 2 == 1
        while not fenced and _is_prose(para) and _OPEN_END.search(para) and not HEADING_RE.match(para.rsplit("\n", 1)[-1]):
            j = i
            while j < len(paras) and j - i < _MAX_FLOATS and _FLOAT.match(paras[j].lstrip()):
                j += 1
            if j == len(paras) or not _is_prose(paras[j]):
                break
            nxt = paras[j].lstrip()
            if "```" in nxt or not (nxt[0].islower() or para.endswith(',')):
                break
            # a word hyphenated across the break stays one word
            para = f"{para}{nxt}" if re.search(r'[a-z]-$', para) else f"{para} {nxt}"
            floats += paras[i:j]
            i = j + 1
        out.append(para)
        out.extend(floats)
    return "\n\n".join(out)


def split_into_chunks(markdown: str) -> list[dict]:
    """Split markdown into chunks by headings and paragraphs."""
    markdown = join_page_breaks(markdown)
    chunks = []
    current_heading = None
    current_level = None
    current_text_parts = []

    for line in markdown.split("\n"):
        heading_match = HEADING_RE.match(line)
        if heading_match:
            text = "\n".join(current_text_parts).strip()
            if text:
                chunks.append({"heading": current_heading, "level": current_level, "text": text})
            current_heading = heading_match.group(2).strip()
            current_level = len(heading_match.group(1))
            current_text_parts = []
        else:
            current_text_parts.append(line)

    text = "\n".join(current_text_parts).strip()
    if text:
        chunks.append({"heading": current_heading, "level": current_level, "text": text})

    final_chunks = []
    for chunk in chunks:
        paragraphs = group_floats([p for p in re.split(r'\n\s*\n', chunk["text"]) if p.strip()])
        if len(paragraphs) <= 3:
            final_chunks.append(chunk)
        else:
            for i, para in enumerate(paragraphs):
                para = para.strip()
                if not para:
                    continue
                final_chunks.append({
                    "heading": chunk["heading"] if i == 0 else None,
                    "level": chunk["level"] if i == 0 else None,
                    "text": para,
                })

    return final_chunks


def clean_for_speech(text: str) -> str:
    """Remove markdown formatting, HTML artifacts, citations, and other non-speech content."""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&[a-zA-Z]+;', '', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'\\\[(?:[^\]]*?et\s+al[^\]]*?\d{4}[^\]]*?)\\\]', '', text)
    text = re.sub(r'\[(?:[A-Z][a-zA-Z\s\']+(?:et\s+al\.?)?,?\s*\d{4}[,;\s]*)+\]', '', text)
    text = re.sub(r'\((?:[A-Z][a-zA-Z\s\']+(?:et\s+al\.?)?,?\s*\d{4}[,;\s]*)+\)', '', text)
    text = re.sub(r'\[\d+(?:[,\s\-]+\d+)*\]', '', text)
    text = IMAGE_RE.sub('', text)  # an image's alt/filename is never spoken
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'#page-\d+[-\d]*', '', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^#{1,4}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\\\[', '', text)
    text = re.sub(r'\\\]', '', text)
    text = text.replace('\\\\', ' ')
    text = re.sub(r'\\([a-zA-Z]+)', r'\1', text)
    text = text.replace('†', '')
    text = text.replace('∗', '')
    text = text.replace('‡', '')
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
    return text.strip()


def has_math(text: str) -> bool:
    return bool(MATH_RE.search(text))


_LATEX_WORDS = {
    'sum': 'the sum', 'prod': 'the product', 'int': 'the integral', 'mid': 'given', 'cap': 'intersect',
    'cup': 'union', 'cdot': 'times', 'times': 'times', 'leq': 'less than or equal to',
    'le': 'less than or equal to', 'geq': 'greater than or equal to', 'ge': 'greater than or equal to',
    'neq': 'not equal to', 'ne': 'not equal to', 'approx': 'approximately', 'sim': 'distributed as',
    'in': 'in', 'infty': 'infinity', 'to': 'to', 'rightarrow': 'to', 'pm': 'plus or minus',
    'ldots': '', 'cdots': '', 'dots': '',
}


def strip_latex(text: str) -> str:
    """Last-resort de-LaTeX for when the LLM pass fails: not real spoken math, but far
    better than the command soup ("frac{1}{N} sum_{n=0}...") the TTS reads otherwise.
    Substitutions run twice to unwrap one level of nesting."""
    text = re.sub(r'\\tag\{[^}]*\}', '', text)
    text = re.sub(r'\\(?:left|right|quad|qquad|limits|displaystyle)\b|\\[,;!]', ' ', text)
    for _ in range(2):
        text = re.sub(r'\\(?:widehat|hat)\{([^{}]*)\}', r'\1 hat', text)
        text = re.sub(r'\\(?:overline|bar)\{([^{}]*)\}', r'\1 bar', text)
        text = re.sub(r'\\tilde\{([^{}]*)\}', r'\1 tilde', text)
        text = re.sub(r'\\frac\{([^{}]*)\}\{([^{}]*)\}', r'\1 over \2', text)
        text = re.sub(r'\\sqrt\{([^{}]*)\}', r'square root of \1', text)
        text = re.sub(r'\\(?:mathbf|mathrm|mathcal|text|operatorname)\{([^{}]*)\}', r'\1', text)
    text = re.sub(r'\^2(?![\d\w])', ' squared', text)
    text = re.sub(r'\^\{([^{}]*)\}|\^(\S)', lambda m: f' to the {m.group(1) or m.group(2)} ', text)
    text = re.sub(r'_\{([^{}]*)\}|_(\S)', lambda m: f' sub {m.group(1) or m.group(2)} ', text)
    text = re.sub(r'[${}]', '', text)
    text = re.sub(r'\\\(|\\\)|\\\[|\\\]', ' ', text)
    text = re.sub(r'\\(?:begin|end)\{[a-z]+\*?\}', ' ', text)
    # What is left is words (\\mid) and bare names (\\alpha, \\mu).
    text = re.sub(r'\\([a-zA-Z]+)', lambda m: f' {_LATEX_WORDS.get(m.group(1), m.group(1))} ', text)
    return re.sub(r'  +', ' ', text).strip()


def _reader_endpoints() -> list[tuple[str, str]]:
    attempts = [(settings.reader.llm_url, settings.reader.llm_model)]
    if settings.reader.llm_fallback_url:
        # complete() merges per-url params from the MAIN chain's entry for this
        # url (the fallback is also main-chain hop 3), so anything set there
        # applies to this attempt too — nothing today.
        attempts.append((settings.reader.llm_fallback_url, settings.reader.llm_fallback_model))
    return attempts


async def _llm_convert_math(_client: httpx.AsyncClient, text: str) -> str:
    for url, model in _reader_endpoints():
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": MATH_TO_SPEECH_PROMPT},
                {"role": "user", "content": text},
            ],
            # Thinking off: a rewrite needs no reasoning, and with it on gemma4-12b spent all
            # 8192 tokens thinking and returned no content (53 s, then the fallback) where
            # it answers correctly in under a second without (measured 2026-10-31).
            "chat_template_kwargs": {"enable_thinking": False},
            # Still room for a model that ignores the switch and thinks anyway; 2048 could
            # truncate mid-think, leaving an UNCLOSED think block that THINK_RE can't strip.
            "max_tokens": 8192,
            "temperature": 0.1,
            "stream": False,
        }
        try:
            raw = await llm_client.complete(payload, urls=[url])
            result = THINK_RE.sub("", raw or "").strip()
        except Exception as exc:
            log.warning("Reader LLM failed at %s: %s", url, exc)
            continue
        if result and "<think>" in result:
            log.warning("Reader LLM at %s returned a truncated think block — trying next", url)
            continue
        if result and (MATH_RE.search(result) or _LEFTOVER_LATEX.search(result)):
            # e.g. gemma4-12b echoing an aligned block before its rewrite
            log.warning("Reader LLM at %s left LaTeX in its rewrite — trying next", url)
            continue
        if result:
            return result

    return strip_latex(text)


# --- Tables and figures -------------------------------------------------------
# Read cell by cell a table is noise, and a figure is read as its filename. Each is
# spoken instead as its caption ("Table 2: ...", "Figure 3: ...") plus, where a model
# can supply one, a sentence of overview: the reader LLM summarises a table from its
# markdown, the vision chain describes an image it can open. The caption paragraph is
# consumed by its float so it is not read twice. Only the speech changes: the chunk's
# source_text keeps the table and the image link for the reader chat.

IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
_IMAGE_LINE = re.compile(r'^\s*(?:!\[[^\]]*\]\([^)]*\)\s*)+$')
_TABLE_LINE = re.compile(r'^\s*\|')
_TABLE_RULE = re.compile(r'^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$')
# Leading page anchors (<span id=...></span>) and bold/italic markers before a label.
_LEAD_NOISE = re.compile(r'^(?:\s*<[^>]+>|[\s*_])*')
# The label must be followed by punctuation, a bold close or a line end, so prose
# that opens "Table 2 shows ..." is never taken for a caption.
_LABEL_NUM = r'([A-Z]?\d+[A-Za-z]?|[IVXL]+)(?=[ \t]*(?:[:.|—–-]|\*\*|\n|$))'
_CAPTION_RE = {
    "table": re.compile(r'(?:Table|Tab\.?)\s*' + _LABEL_NUM, re.IGNORECASE),
    "figure": re.compile(r'(?:Figure|Figs?\.?)\s*' + _LABEL_NUM, re.IGNORECASE),
}
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50}
TABLE_PROMPT_MAX_CHARS = 6000
FIGURE_MAX_BYTES = 4 * 1024 * 1024
FIGURE_MAX_IMAGES = 4
_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}

TABLE_OVERVIEW_PROMPT = """You help read a document aloud to a listener who cannot see it. You are given a markdown table and its caption, if it has one.

Write ONE short sentence, under 25 words, giving an overview of the table: what it compares, and the main pattern or standout value if there is one. Do not restate the caption. Do not read cells one by one. Plain text, no markdown.

If the caption already says what the table shows, output exactly NONE.

Output only the sentence (or NONE). No preamble."""

FIGURE_DESCRIPTION_PROMPT = """You help read a document aloud to a listener who cannot see it. You are given a figure from the document and its caption, if it has one.

In one or two short sentences, under 40 words in total, describe what the figure shows: the kind of plot or picture and its main pattern or feature. Add what the caption does not already say; do not repeat the caption. Plain text, no markdown.

If the image is blank, decorative or a logo, output exactly NONE.

Output only the description (or NONE). No preamble."""


def _segments(text: str) -> list[tuple[str, str]]:
    """Split text into ("text" | "table" | "image", markdown) runs: blank-line
    paragraphs, with table lines and image-only lines broken out of a paragraph
    they share (a caption line directly above a table is then its own run)."""
    segments: list[tuple[str, str]] = []
    for para in re.split(r'\n\s*\n', text):
        if not para.strip():
            continue
        lines = para.strip("\n").split("\n")
        if not any(_TABLE_LINE.match(line) or _IMAGE_LINE.match(line) for line in lines):
            segments.append(("text", para.strip()))
            continue
        run_kind, run = None, []
        for line in lines:
            kind = "table" if _TABLE_LINE.match(line) else "image" if _IMAGE_LINE.match(line) else "text"
            if kind != run_kind and run:
                segments.append((run_kind, "\n".join(run).strip()))
                run = []
            # each image line is its own image (a run of them is grouped by the pairing)
            if kind == "image" and run:
                segments.append((kind, "\n".join(run).strip()))
                run = []
            run_kind = kind
            run.append(line)
        if run and "\n".join(run).strip():
            segments.append((run_kind, "\n".join(run).strip()))
    return segments


def parse_caption(text: str, kind: str) -> tuple[str, str] | None:
    """("2", "Mean error by method") for a "Table 2: Mean error by method" paragraph
    (kind "table"; "figure" takes Figure/Fig./Figs.), or None if it is not one."""
    body = _LEAD_NOISE.sub("", text, count=1)
    match = _CAPTION_RE[kind].match(body)
    if not match:
        return None
    number = match.group(1)
    if re.fullmatch(r'[IVXL]+', number):
        values = [_ROMAN[c] for c in number]
        number = str(sum(-v if i + 1 < len(values) and v < values[i + 1] else v for i, v in enumerate(values)))
    rest = re.sub(r'^[\s*_:.|—–-]+', '', body[match.end():])
    return number, " ".join(rest.split())


def _pair_floats(kinds: list[str], texts: list[str]) -> list[tuple[str, int, int, int | None]]:
    """Find each float and its caption: (kind, first, last, caption index or None).
    A run of consecutive images is one figure (sub-figures share a caption). A table
    looks for its caption above first, a figure below first; a caption is used once."""
    floats, used = [], set()
    i = 0
    while i < len(kinds):
        if kinds[i] not in ("table", "image"):
            i += 1
            continue
        j = i
        if kinds[i] == "image":
            while j + 1 < len(kinds) and kinds[j + 1] == "image":
                j += 1
        kind = "table" if kinds[i] == "table" else "figure"
        order = (i - 1, j + 1) if kind == "table" else (j + 1, i - 1)
        caption = None
        for k in order:
            if 0 <= k < len(kinds) and k not in used and kinds[k] == "text" and parse_caption(texts[k], kind):
                caption = k
                used.add(k)
                break
        floats.append((kind, i, j, caption))
        i = j + 1
    return floats


def group_floats(paragraphs: list[str]) -> list[str]:
    """Join each table or figure paragraph with its caption paragraph, so the
    per-paragraph chunking of a long section never splits a caption from its float."""
    kinds = []
    for para in paragraphs:
        kinds_in = {kind for kind, _ in _segments(para)}
        kinds.append("table" if "table" in kinds_in else "image" if kinds_in == {"image"} else "text")
    groups = {}
    for _kind, first, last, caption in _pair_floats(kinds, paragraphs):
        lo, hi = min(first, caption if caption is not None else first), max(last, caption if caption is not None else last)
        groups[lo] = hi
    out, i = [], 0
    while i < len(paragraphs):
        hi = groups.get(i, i)
        out.append("\n\n".join(paragraphs[i:hi + 1]))
        i = hi + 1
    return out


def _caption_speech(label: str, caption: tuple[str, str]) -> str:
    number, rest = caption
    rest = clean_for_speech(rest).strip()
    if not rest:
        return f"{label} {number}."
    return f"{label} {number}: {rest}" + ("" if rest[-1] in ".!?" else ".")


def _overview_text(raw: str | None, max_words: int) -> str | None:
    """Clean a model's overview; "" for NONE, None for an unusable answer."""
    text = THINK_RE.sub("", raw or "").strip().strip('"').strip()
    if not text or "<think>" in text or "|" in text:
        return None
    if re.fullmatch(r'\W*NONE\W*', text, re.IGNORECASE):
        return ""
    text = clean_for_speech(text)
    sentences = split_sentences(text)
    while sentences and len(" ".join(sentences).split()) > max_words and len(sentences) > 1:
        sentences.pop()
    text = " ".join(sentences)
    return text if text and len(text.split()) <= max_words * 2 else None


async def _table_overview(table_md: str, caption_text: str | None) -> str | None:
    """One-sentence overview from the reader LLM ("" when the caption suffices),
    trying each reader endpoint in turn; None when every endpoint fails."""
    if len(table_md) > TABLE_PROMPT_MAX_CHARS:
        kept = table_md[:TABLE_PROMPT_MAX_CHARS].rsplit("\n", 1)[0]
        dropped = table_md.count("\n") - kept.count("\n")
        table_md = f"{kept}\n(table truncated: {dropped} more rows)"
    user = f"Caption: {caption_text or '(none)'}\n\nTable:\n{table_md}"
    for url, model in _reader_endpoints():
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": TABLE_OVERVIEW_PROMPT},
                {"role": "user", "content": user},
            ],
            # Thinking off, as for math: gemma4-12b otherwise spends the budget thinking.
            "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": 4096,
            "temperature": 0.1,
            "stream": False,
        }
        try:
            result = _overview_text(await llm_client.complete(payload, urls=[url]), max_words=30)
        except Exception as exc:
            log.warning("Reader table overview failed at %s: %s", url, exc)
            continue
        if result is not None:
            return result
        log.warning("Reader table overview from %s was unusable — trying next", url)
    return None


def _resolve_image(target: str, base_dir: Path | None) -> Path | None:
    """The local file a markdown image points at, if it is one we may send: an
    image type, at most FIGURE_MAX_BYTES. Remote and data: URLs are never fetched."""
    target = unquote(target.strip())
    if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.IGNORECASE) and not target.lower().startswith("file://"):
        return None
    path = Path(target.removeprefix("file://")).expanduser()
    if not path.is_absolute():
        if base_dir is None:
            return None
        path = base_dir / path
    try:
        if path.suffix.lower() not in _IMAGE_MIME or not path.is_file():
            return None
        if path.stat().st_size > FIGURE_MAX_BYTES:
            log.info("Reader: figure %s is over %d bytes; caption only", path, FIGURE_MAX_BYTES)
            return None
    except OSError:
        return None
    return path


async def _figure_description(images: list[Path], caption_text: str | None) -> str | None:
    """A sentence or two from the vision chain ("" for NONE, None on failure)."""
    content: list[dict] = [{"type": "text", "text": f"Caption: {caption_text or '(none)'}"}]
    for path in images[:FIGURE_MAX_IMAGES]:
        try:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            log.warning("Reader: could not read figure %s: %s", path, exc)
            continue
        content.append({"type": "image_url", "image_url": {"url": f"data:{_IMAGE_MIME[path.suffix.lower()]};base64,{b64}"}})
    if len(content) == 1:
        return None
    # Entry by entry, each with its own model: complete() keeps the caller's model
    # for every url it is given, and the vision hops serve different aliases.
    for entry in settings.vision_llm_chain:
        payload = {
            "model": entry["model"],
            "messages": [
                {"role": "system", "content": FIGURE_DESCRIPTION_PROMPT},
                {"role": "user", "content": content},
            ],
            "chat_template_kwargs": {"enable_thinking": False},
            # room for a model that ignores the switch and thinks anyway
            "max_tokens": 4096,
            "temperature": 0.2,
            "stream": False,
        }
        try:
            result = _overview_text(await vision_llm_client.complete(payload, urls=[entry["url"]]), max_words=45)
        except Exception as exc:
            log.warning("Reader figure description failed at %s: %s", entry["url"], exc)
            continue
        if result is not None:
            return result
    return None


async def _table_speech(table_md: str, caption: tuple[str, str] | None, caption_raw: str | None) -> str:
    rows = [line for line in table_md.split("\n") if _TABLE_LINE.match(line) and not _TABLE_RULE.match(line)]
    data_rows = max(len(rows) - 1, 0)
    lead = _caption_speech("Table", caption) if caption else None
    if lead and has_math(lead):
        lead = clean_for_speech(await _llm_convert_math(None, lead))
    overview = await _table_overview(table_md, clean_for_speech(caption_raw) if caption_raw else None)
    if not overview:  # None: every endpoint failed; "": NONE, the caption says it
        return lead or f"A table with {data_rows} row{'s' if data_rows != 1 else ''}."
    return f"{lead or 'A table.'} {overview}"


async def _figure_speech(image_mds: list[str], caption: tuple[str, str] | None, caption_raw: str | None, base_dir: Path | None) -> str:
    links = [m for md in image_mds for m in IMAGE_RE.finditer(md)]
    if caption is None:
        # a caption carried in the alt text: ![Figure 1: ...](x.png)
        for m in links:
            caption = parse_caption(m.group(1), "figure")
            if caption:
                caption_raw = m.group(1)
                break
    lead = _caption_speech("Figure", caption) if caption else None
    if lead and has_math(lead):
        lead = clean_for_speech(await _llm_convert_math(None, lead))
    images = [p for p in (_resolve_image(m.group(2), base_dir) for m in links) if p]
    description = await _figure_description(images, clean_for_speech(caption_raw) if caption_raw else None) if images else None
    if description:
        return f"{lead or 'A figure.'} {description}"
    return lead or ""


async def _speak_segments(segments: list[tuple[str, str]], base_dir: Path | None) -> list[str]:
    """Speech for a chunk's segments in order: text cleaned (math via the LLM),
    each float replaced by its spoken form, and consumed captions dropped."""
    kinds = [kind for kind, _ in segments]
    texts = [text for _, text in segments]
    floats = {first: (kind, last, caption) for kind, first, last, caption in _pair_floats(kinds, texts)}
    consumed = {caption for _, _, caption in floats.values() if caption is not None}
    parts, i = [], 0
    while i < len(segments):
        if i in floats:
            kind, last, cap = floats[i]
            caption = parse_caption(texts[cap], kind) if cap is not None else None
            caption_raw = caption[1] if caption else None
            if kind == "table":
                parts.append(await _table_speech(texts[i], caption, caption_raw))
            else:
                parts.append(await _figure_speech(texts[i:last + 1], caption, caption_raw, base_dir))
            i = last + 1
            continue
        if i not in consumed:
            para = texts[i]
            parts.append(clean_for_speech(await _llm_convert_math(None, para) if has_math(para) else para))
        i += 1
    return [part for part in parts if part]


async def _convert_chunk(client: httpx.AsyncClient, chunk: dict, base_dir: Path | None = None) -> str:
    text = chunk["text"]
    if chunk["heading"]:
        text = f"{chunk['heading']}\n\n{text}"
    return "\n\n".join(await _speak_segments(_segments(text), base_dir))


async def _convert_all_chunks(chunks: list[dict], base_dir: Path | None = None) -> list[str]:
    """`base_dir` is the source markdown's directory: relative image links resolve
    against it. Without one, figures are spoken from their captions alone."""
    math_count = sum(1 for chunk in chunks if has_math(chunk["text"]))
    float_kinds = [kind for chunk in chunks for kind, _ in _segments(chunk["text"]) if kind != "text"]
    log.info(
        "Reader: %d of %d chunks contain math, %d table and %d image blocks — only those hit a model",
        math_count, len(chunks), float_kinds.count("table"), float_kinds.count("image"),
    )

    # The reader model (qwen3.6-35b-a3b) serves 3 parallel slots on lilripper.
    sem = asyncio.Semaphore(3)

    async def convert(chunk: dict) -> str:
        async with sem:
            return await _convert_chunk(client, chunk, base_dir)

    async with httpx.AsyncClient(timeout=60.0) as client:
        return list(await asyncio.gather(*(convert(chunk) for chunk in chunks)))


def split_sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in SENTENCE_END.split(text) if sentence.strip()]


def _build_speech_chunks(raw_chunks: list[dict], converted_texts: list[str], start_index: int = 0) -> list[dict]:
    speech_chunks = []
    for i, (raw_chunk, speech_text) in enumerate(zip(raw_chunks, converted_texts), start=start_index):
        sentences = split_sentences(speech_text)
        if not sentences:
            sentences = [speech_text] if speech_text else [""]
        chunk = {
            "index": i,
            "heading": raw_chunk["heading"],
            "speech_text": speech_text,
            "sentences": sentences,
            # The chunk's markdown (LaTeX and citations intact) for the reader chat's look-ups.
            "source_text": raw_chunk["text"],
        }
        # Heading depth (1-3, from #/##/###) for the reader's table of contents.
        # Documents converted before 2026-10-30 have none; clients treat that as 1.
        if raw_chunk["heading"] and raw_chunk.get("level"):
            chunk["level"] = raw_chunk["level"]
        speech_chunks.append(chunk)
    return speech_chunks


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON to `path` via temp file + os.replace so a reader (playback,
    which reads this file at play start) never sees a half-written file, and a
    crash mid-write keeps the old one."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _write_speech(conn: sqlite3.Connection, doc_id: int, title: str, chunks: list[dict], **fields) -> None:
    total_sentences = sum(len(chunk["sentences"]) for chunk in chunks)
    speech_path = READER_PATH / f"{doc_id}.json"
    # The row's title wins over the one captured when the job started: a rename
    # that landed while this job was converting must not be undone here.
    row = get_document(conn, doc_id)
    if row and row.get("title"):
        title = row["title"]
    _atomic_write_json(speech_path, {"title": title, "total_sentences": total_sentences, "chunks": chunks})
    last_chunk = fields.get("last_chunk", row["last_chunk"] if row else 0)
    last_sentence = fields.get("last_sentence", row["last_sentence"] if row else 0)
    update_document(
        conn,
        doc_id,
        speech_file=str(speech_path),
        chunk_count=len(chunks),
        status="ready",
        error=None,
        total_sentences=total_sentences,
        progress_sentences=sentences_before(chunks, last_chunk, last_sentence),
        **fields,
    )
    log.info("Reader: document %d ready — %d chunks, %d sentences", doc_id, len(chunks), total_sentences)


def rename_document(conn: sqlite3.Connection, doc_id: int, title: str) -> None:
    """Update a document's title in the DB and, if a speech JSON exists, rewrite
    its stored title too (temp + os.replace, same as `_write_speech`). A missing
    or unreadable speech file is not an error — the DB title is the source of
    truth (playback never reads the JSON title) and the rename still succeeds.
    While the document is `processing` the JSON is left alone (see below)."""
    update_document(conn, doc_id, title=title)
    doc = get_document(conn, doc_id)
    if not doc or not doc.get("speech_file"):
        return
    if doc.get("status") == "processing":
        # An ingest/append job is about to replace the speech file; rewriting it
        # here could race that write and clobber freshly converted chunks. The
        # job re-reads the row's title just before it writes, so nothing is lost.
        return
    speech = load_speech_data(doc)
    if not speech:
        log.warning("Reader: document %d has speech_file %s but it is missing or unreadable; title not rewritten there", doc_id, doc["speech_file"])
        return
    speech["title"] = title
    _atomic_write_json(Path(doc["speech_file"]), speech)


def with_appendices(doc_id: int, markdown: str) -> str:
    """Fold any text appended to this document after its original ingest onto the
    end of `markdown`. A retry replays the ORIGINAL source (re-downloads the URL,
    re-reads the file); without this step it would silently drop everything Dave
    appended later. If the document was `replace`d, the original source is what
    Dave discarded, so it is dropped here rather than resurrected. Each block is
    separated by a blank line so it starts its own paragraph and never merges
    into the last one of the base text."""
    appendices = load_appendices(doc_id)
    if not appendices:
        return markdown
    base = "" if is_replaced(doc_id) else markdown.rstrip()
    parts = [base] + [block.strip() for block in appendices]
    return "\n\n".join(part for part in parts if part)


def _derive_title(conn: sqlite3.Connection, doc_id: int, markdown: str, title: str) -> str:
    is_generic = title in ("Untitled", "") or title.startswith("reader_") or title.endswith((".pdf", ".md", ".txt"))
    if not is_generic:
        return title
    heading_match = HEADING_RE.search(markdown)
    if heading_match:
        extracted = heading_match.group(2).strip()
        if 5 < len(extracted) < 200:
            update_document(conn, doc_id, title=extracted)
            log.info("Reader: extracted title from content: %s", extracted)
            return extracted
    elif markdown.strip():
        first_line = markdown.strip().split("\n")[0].strip()[:120]
        if first_line:
            update_document(conn, doc_id, title=first_line)
            return first_line
    return title


async def ingest_document(
    conn: sqlite3.Connection,
    doc_id: int,
    markdown: str,
    title: str,
    original_md_path: str | None = None,
    source_dir: str | None = None,
):
    """Process markdown into speech-ready JSON. Updates the DB row on completion.
    Relative image links resolve against `source_dir`, else `original_md_path`'s directory."""
    try:
        log.info("Reader: ingesting document %d: %s", doc_id, title)

        markdown = with_appendices(doc_id, markdown)
        title = _derive_title(conn, doc_id, markdown, title)

        raw_chunks = split_into_chunks(markdown)
        if not raw_chunks:
            update_document(conn, doc_id, status="failed", error="No content found")
            return

        log.info("Reader: document %d split into %d chunks", doc_id, len(raw_chunks))

        base_dir = source_dir or (str(Path(original_md_path).parent) if original_md_path else None)
        converted_texts = await _convert_all_chunks(raw_chunks, Path(base_dir) if base_dir else None)
        speech_chunks = _build_speech_chunks(raw_chunks, converted_texts)
        _write_speech(conn, doc_id, title, speech_chunks, original_md_file=original_md_path)

    except Exception as exc:
        log.exception("Reader: ingest failed for document %d", doc_id)
        update_document(conn, doc_id, status="failed", error=str(exc))


async def append_to_document(
    conn: sqlite3.Connection,
    doc_id: int,
    text: str,
    replace: bool = False,
    prior_status: str | None = None,
):
    """Add `text` to the end of an existing document's speech JSON.

    Incremental on purpose: only the new text is chunked and sent through math
    conversion, and the existing chunks are kept byte-for-byte with their
    indices, so `last_chunk` / `last_sentence` still point where Dave left
    off. Re-chunking the whole document would not guarantee that — a section
    that grows past three paragraphs is re-split per paragraph.

    With `replace=True` the existing chunks are discarded and the document is
    rebuilt from `text` alone (for when the original pull got a sign-in page
    rather than the article). A document with no usable speech file (e.g. a
    failed URL pull) is rebuilt from `text` either way.

    Atomic from the document's point of view: the appendix file is persisted
    only after conversion succeeds and just before the speech file is written,
    so a failed append (LLM down, disk full) leaves the document exactly as it
    was — a `ready` document stays `ready`, with the failure recorded in
    `error` — and no orphaned appendix waits to be folded into a later retry.
    `prior_status` is what the row said before the caller flipped it to
    `processing`; it is what a failure restores.
    """
    try:
        doc = get_document(conn, doc_id)
        if not doc:
            raise ValueError(f"Document {doc_id} not found")
        title = doc["title"]

        existing: list[dict] = []
        if not replace:
            speech = load_speech_data(doc)
            if speech:
                existing = list(speech.get("chunks") or [])
            elif doc.get("speech_file"):
                log.warning(
                    "Reader: document %d has speech_file %s but it is missing; rebuilding from appended text",
                    doc_id, doc["speech_file"],
                )

        raw_chunks = split_into_chunks(text)
        if not raw_chunks:
            raise ValueError("No content found in appended text")

        log.info(
            "Reader: %s document %d with %d new chunks (after %d existing)",
            "replacing" if replace else "appending to", doc_id, len(raw_chunks), len(existing),
        )
        converted_texts = await _convert_all_chunks(raw_chunks)
        new_chunks = _build_speech_chunks(raw_chunks, converted_texts, start_index=len(existing))

        # Persistence is mandatory, not best-effort: without the appendix a
        # retry replays the original source and silently drops this text — the
        # exact failure this feature exists to prevent.
        save_appendix(doc_id, text, replace=replace)

        # Position can only be kept when the chunks it points into survive.
        fields = {} if existing else {"last_chunk": 0, "last_sentence": 0}
        _write_speech(conn, doc_id, title, existing + new_chunks, **fields)

    except Exception as exc:
        log.exception("Reader: append failed for document %d", doc_id)
        restored = prior_status if prior_status in ("ready", "failed") else "failed"
        update_document(conn, doc_id, status=restored, error=f"Append failed: {exc}")
