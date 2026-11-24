"""Cache and security headers for the browser UI pages and /static."""
from fastapi import FastAPI, Request

UI_PAGES = {"/", "/inbox", "/reader"}

# Defence in depth for pages that render untrusted text (replies quoting web
# pages, saved items, reader documents): even if an escaping bug lets markup
# through, it cannot run script, load remote images, or post anywhere.
# No 'unsafe-inline' anywhere: the pages have no inline <script>, no on*=
# handlers and no style="" attributes (JS sets styles through el.style, which
# CSP allows). Adding an external resource means adding its origin here; see
# AGENTS.md "Frontend". connect-src 'self' covers same-origin ws:/wss: (CSP3).
UI_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",  # marked is vendored in static/vendor/, so no CDN is trusted
    "style-src 'self' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",  # 'self': KaTeX's fonts, vendored with it
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
])
UI_SECURITY_HEADERS = {
    "Content-Security-Policy": UI_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def install_ui_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def revalidate_ui(request: Request, call_next):
        # Pages and their assets change together on disk with no build step, so a
        # browser must not run a cached old script against new HTML: always
        # revalidate (StaticFiles/FileResponse answer with ETag + 304).
        response = await call_next(request)
        if request.url.path.startswith("/static/") or request.url.path in UI_PAGES:
            response.headers.setdefault("Cache-Control", "no-cache")
        # Every HTML response, not just the page routes: the same files are also
        # reachable as /static/index.html etc., and that copy must not escape the CSP.
        is_html = response.headers.get("content-type", "").startswith("text/html")
        if request.url.path in UI_PAGES or is_html:
            for name, value in UI_SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
        return response
