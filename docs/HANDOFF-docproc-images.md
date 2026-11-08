# Handoff: docproc PDF output has images but no image links

2026-10-31. For whoever next works on docproc (the PDF → markdown converter on
lilripper `:8251`; mcp-tools' `server_documents.py` only wraps it).

## What Octavius does now

The Reader speaks a markdown image `![](path)` as its `Figure N` caption plus a
1-2 sentence description from the vision chain, resolving `path` against the
markdown file's folder (`reader_text._resolve_image`). A converted PDF reaches
the Reader through `reader_ingest_handlers.ingest_pdf_document`: it polls
`get_conversion_result`, regexes the first `/….md` path out of the reply,
reads that file, and passes it as `original_md_path`, whose parent folder is
the image base. So any image link docproc leaves in the `.md` is picked up
with no further Octavius change.

## What docproc produces today (checked on reader document 40, structure only)

- Output folder: the `.md`, one `<stem>_meta.json` (286 bytes) and 13 image
  files named marker-style, `_page_<P>_Figure_<B>.jpeg` /
  `_page_<P>_Picture_<B>.jpeg`.
- The `.md` has **zero** `![` links, no `<img>`, and no `<span id="page-P-B">`
  anchors. Its 19 caption paragraphs (`**Fig. N.** …`, `**Table N** …`) are
  present as text.
- The `.json` is not a block tree. Its only key is `columns`:
  `{two_column_pages, pages_analyzed, fraction, recommended, sampled, message}`,
  which is a column-layout analysis. There are no blocks, types, positions or
  captions.

So nothing ties an image file to a place in the text. The page number in the
filename is not enough: guessing by page order would attach figures to the
wrong caption in two-column papers. Octavius deliberately does not guess, so
converted PDFs get captions only.

## What docproc should change (any one is enough)

1. **Preferred:** keep marker's image links in the markdown. Marker's markdown
   renderer writes `![](_page_P_Figure_B.jpeg)` where the block sits. Presumably something
   in docproc's pipeline (a post-process or a renderer option) drops them while
   still saving the files. Leave them in, relative to the `.md`. Octavius needs
   no change.
2. Or also write marker's JSON render (`--output_format json`) next to the
   `.md`: a block tree with `id` (`/page/P/Figure/B`), `block_type`
   (`Figure`/`Picture`/`Caption`/`Table`), `polygon` and `html`. Octavius would
   then insert the links at ingest by matching each image's (page, block)
   against that tree's reading order. This is more work on our side.

## How to verify after the change

Re-convert one figure-heavy PDF, then `grep -c '!\[' <out>.md` (it should equal
the number of `_page_*` files), and retry the document in `/reader`. Figures
should be spoken with a description. Existing documents keep their old speech
until they are retried.
