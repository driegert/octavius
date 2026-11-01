# Pronunciation fixes

Words or patterns the voice (Phonon TTS) reads badly. Jot them under **To fix**:
what was written, and what you heard. No regex needed. Each one becomes a rule in
`tts.speechify` (the single place all spoken text passes through) with a test, and is
checked by synthesizing it and transcribing the audio back. Only the text sent to the
voice changes; documents and replies keep their original text.

## To fix

| Written | Heard | Notes |
|---|---|---|
|       |          |        |
|       |          |        |
|       |          |        |
|       |          |        |
|       |          |        |
|       |          |        |
|       |          |        |

## Done

| Written | Heard | Now read as | Rule |
|---|---|---|---|
| Smith et al. (2020) | a pause after "al." | "et al" | `_ET_AL` (2026-10-31) |
| published by U.S. | "by you later state", garbled | "US" | `_INITIALISM` (2026-10-31) |
| 4.6x speedup, 10x faster | "4.6 back", "10 bikes" | "4.6 times", "10 times" | `_MULTIPLIER` (2026-10-31); "2x2" and "1920x1080" already read as "by" |
| 2– 8%, 2–8% | "two eight percent" | "2 to 8%" | `_DASH_RANGE`, `_HYPHEN_RANGE` (2026-10-31); en/em dash between numbers always; a hyphen only in "5-10%", "pp. 12-15", "1990-2000" (not dates, "COVID-19", "1-800-555", subtraction) |

Reader-only fixes (sentence splitting, page breaks) live in `reader_text.py`, not here.
