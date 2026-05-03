import bleach
import markdown as _md

_ALLOWED_TAGS = [
    "p", "br", "hr",
    "strong", "em", "b", "i", "u", "s", "del", "ins",
    "code", "pre", "blockquote",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "img",
    "span", "div",
]

_ALLOWED_ATTRS = {
    "a": ["href", "title", "rel", "target"],
    "img": ["src", "alt", "title"],
    "*": ["class"],
}

_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def _harden_links(attrs, new=False):
    href = attrs.get((None, "href"), "")
    if href.startswith(("http://", "https://")):
        attrs[(None, "rel")] = "noopener nofollow"
        attrs[(None, "target")] = "_blank"
    return attrs


def render_markdown(text: str) -> str:
    if not text:
        return ""
    html = _md.markdown(
        text,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )
    cleaned = bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return bleach.linkify(cleaned, callbacks=[_harden_links])
