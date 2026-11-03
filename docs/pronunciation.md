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
| document-level sentiment; domain- and task-specific; domain-adaptive; aspect–sentiment | "document dash level", "domain dash and"; only in some sentences | "document level", "domain and task specific" | `_WORD_HYPHEN`, `_HANGING_HYPHEN` (2026-11-01); a hyphen or unspaced en/em dash between letters is read as a space, a hanging hyphen before "and"/"or"/"to" is dropped. Phonon is deterministic but reads the hyphen differently by context, which is the "sometimes"; "COVID-19" and number ranges untouched |
| 57.7% of the 241,309 (in a long sentence) | "fifty-seven, seven percent" | "57 point 7%" | `_DECIMAL` (2026-11-01); fine in a short sentence, lost the point in a long one in three of four voices tested. Fraction digits one by one ("0 point 0 5"); not money ("$3.50") or versions ("v1.2", "1.2.3"). "4.6x" is now "4 point 6 times" |
| Public Housing; Housing is...; Ontario Housing Corporation | "Public hue-zing", "Husing is", "Ontario Hues Incorporation" | "Howzing" | `_OUSING` (2026-11-02); wrong whenever capitalised or sentence-initial (Phonon capitalises a sentence start itself, so lower-casing cannot fix it), right only in lower case mid-sentence. Respelled "howzing" it was heard right in every position and in three voices; all caps keeps only the first capital ("HOWZING" is heard as "pausing"). Also "rousing"/"arousing"; "Houses", "rehousing" read fine and are untouched |
| quarters from Q1 2019 to Q2 2026 | "Q one two zero one nine to Q two two zero two six" | "Q1 of 2019 to Q2 of 2026" | `_QUARTER_YEAR` (2026-11-03); Q1-Q4 then a 19xx/20xx year, comma or not. After "Q1" the year was read digit by digit (0.8-1.3 s against 0.4-0.6 s for a plain "in 2019", three voices); with "of" it reads normally, and a comma alone did not help. "2019 Q1" already read fine |
| in Section 5.10, model | "section five point one zero" | unchanged | None, kept on purpose (2026-11-03): reading the value as a decimal is the right call over "five ten" for a section number |

Reader-only fixes (sentence splitting, page breaks) live in `reader_text.py`, not here.
