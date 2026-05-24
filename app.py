from __future__ import annotations
"""Local HTML-to-reader-PDF application.

The app is intentionally small and synchronous: Flask serves the UI and JSON
endpoints, BeautifulSoup/lxml performs HTML cleanup and extraction, and
ReportLab renders the selected reader blocks into a PDF. WeasyPrint can be
enabled explicitly through ``HTML2PDF_USE_WEASYPRINT=1`` if the host has its
native dependencies installed.
"""

import mimetypes
import os
import re
import shutil
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

HTML = None
if os.environ.get("HTML2PDF_USE_WEASYPRINT") == "1":
    try:
        from weasyprint import HTML
    except Exception:  # pragma: no cover - reported at runtime in /api/status
        HTML = None

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Image,
        ListFlowable,
        ListItem,
        Paragraph,
        Preformatted,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
except Exception:  # pragma: no cover - reported at runtime in /api/status
    SimpleDocTemplate = None


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "pdf"
UPLOAD_DIR = ROOT / ".uploads"
CACHE_DIR = ROOT / ".image_cache"
UPLOAD_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)
DEFAULT_OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
SESSIONS: Dict[str, "DocumentSession"] = {}


DROP_TAGS = {
    "script",
    "noscript",
    "style",
    "iframe",
    "object",
    "embed",
    "canvas",
    "svg",
    "input",
    "button",
    "select",
    "textarea",
}

NOISE_RE = re.compile(
    r"(?:^|[-_\s])("
    r"ad|ads|advert|advertisement|banner|breadcrumb|cookie|consent|footer|header|"
    r"login|menu|modal|nav|newsletter|outbrain|popup|promo|recommend|related|"
    r"share|sharing|sidebar|social|sponsor|subscribe|teaser|tracking"
    r")(?:[-_\s]|$)",
    re.I,
)

BLOCK_NOISE_RE = re.compile(
    r"(?:^|[-_\s])("
    r"affiliations?|audio|comments?|footer|hide|jump|latest|newest|newsletter|"
    r"player|related|share|sharing|sponsors?|toolbar|toolbox|"
    r"program|meta|utility|utilities"
    r")(?:[-_\s]|$)",
    re.I,
)

CONTENT_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "blockquote",
    "pre",
    "table",
    "figure",
}

CONTENT_DIV_CLASSES = {
    "abstract-title",
    "article-title-main",
    "caption",
    "figure-section",
    "h6",
    "para",
    "title",
}

CONTENT_IDS = {"html-abstract", "html-keywords"}


@dataclass
class ReaderBlock:
    """One user-selectable unit of cleaned article content."""

    id: str
    tag: str
    label: str
    html: str
    include: bool


@dataclass
class DocumentSession:
    """In-memory state for a loaded HTML file and its extracted content."""

    id: str
    source_path: Path
    source_url: Optional[str]
    title: str
    original_html: str
    blocks: List[ReaderBlock]
    image_cache: Dict[str, Path]


def html_files() -> List[dict]:
    """Return saved HTML files under ``html/`` for the UI datalist.

    Users can still enter any absolute path; this list is only a convenience
    for the archive that lives next to the app.
    """

    files = []
    for path in sorted((ROOT / "html").rglob("*")):
        if path.is_file() and path.suffix.lower() in {".html", ".htm"}:
            files.append(
                {
                    "name": str(path.relative_to(ROOT / "html")),
                    "path": str(path),
                }
            )
    return files


def read_html(path: Path) -> str:
    """Read browser-saved HTML using common web encodings.

    Saved pages in the archive are not guaranteed to be UTF-8, so the function
    tries a short ordered list before falling back to Latin-1 replacement.
    """

    data = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "windows-1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def clean_soup(raw_html: str, strip_attributes: bool = True) -> BeautifulSoup:
    """Parse and sanitize raw HTML before content extraction.

    This removes active content, obvious page chrome, comments, and most
    attributes. The result is still HTML, but much closer to static reader
    content and safer to preview in the sandboxed iframe/reader view. During
    extraction, callers can keep attributes temporarily so class/id hints are
    still available for block-level filtering.
    """

    soup = BeautifulSoup(raw_html, "lxml")
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for form in soup.find_all("form"):
        form.unwrap()
    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if tag.parent and is_noise(tag):
            tag.decompose()
    if strip_attributes:
        for tag in soup.find_all(True):
            clean_attributes(tag)
    return soup


def is_noise(tag: Tag) -> bool:
    """Heuristically decide whether a tag is layout or advertising noise.

    The rule combines semantic tags such as ``nav`` and ``aside`` with class,
    id, role, and aria-label patterns commonly used for menus, ads, sharing
    controls, cookie prompts, and related-content boxes. Page-level containers
    and article-like headers are protected because many news sites place real
    headlines, lead images, and captions in elements whose classes include
    words such as ``header``.
    """

    if tag.name in {"html", "body", "main", "article"}:
        return False
    if not getattr(tag, "attrs", None):
        return False
    if tag.name in {"nav", "footer", "aside"}:
        return True
    blob = " ".join(
        filter(
            None,
            [
                tag.get("id"),
                " ".join(tag.get("class", [])),
                tag.get("role"),
                tag.get("aria-label"),
            ],
        )
    )
    if not blob or not NOISE_RE.search(blob):
        return False
    if tag.name == "header" and tag.find(["h1", "h2", "p", "figure", "img"]):
        return False
    return True


def clean_attributes(tag: Tag) -> None:
    """Keep only attributes that are useful for reader output.

    Most styling, behavior, tracking, and framework attributes are discarded.
    Image lazy-loading attributes are preserved here so a later image pass can
    choose the best available source.
    """

    allowed = {
        "href",
        "src",
        "srcset",
        "data-src",
        "data-srcset",
        "data-original",
        "alt",
        "title",
        "colspan",
        "rowspan",
    }
    for attr in list(tag.attrs):
        if attr not in allowed:
            del tag.attrs[attr]
    if tag.name == "a" and tag.get("href", "").lower().startswith("javascript:"):
        del tag.attrs["href"]


def rewrite_preview_assets(soup: BeautifulSoup, source_path: Path, session_id: str) -> None:
    """Rewrite local asset references so Flask can serve them in previews.

    Browser-saved pages usually refer to sibling ``*_files`` directories. The
    app proxies those through ``/asset/<session>`` so the preview can render
    local files without exposing arbitrary filesystem paths.
    """

    for tag in soup.find_all(["img", "source"]):
        if tag.get("src") and not tag["src"].startswith(("/asset/", "/downloaded/")):
            tag["src"] = asset_url(source_path, tag["src"], session_id)
    for tag in soup.find_all("link"):
        if tag.get("href") and not tag["href"].startswith(("/asset/", "/downloaded/")):
            tag["href"] = asset_url(source_path, tag["href"], session_id)


def candidate_roots(soup: BeautifulSoup) -> Iterable[Tag]:
    """Yield possible article containers ordered from specific to broad.

    We try semantic/typical article selectors first, then fall back to the
    body and finally individual div/section nodes for pages without modern
    structure.
    """

    selectors = ["article", "main", "[role=main]", ".article", ".content", "#content"]
    seen = set()
    for selector in selectors:
        for tag in soup.select(selector):
            if id(tag) not in seen:
                seen.add(id(tag))
                yield tag
    body = soup.body or soup
    yield body
    for tag in body.find_all(["div", "section"]):
        yield tag


def text_score(tag: Tag) -> int:
    """Score a possible content root by readable text density.

    Paragraphs and headings increase confidence, while link-heavy regions are
    penalized because menus and related-link boxes often contain lots of text
    but little article content.
    """

    text = tag.get_text(" ", strip=True)
    paragraph_bonus = 80 * len(tag.find_all("p"))
    heading_bonus = 40 * len(tag.find_all(re.compile("^h[1-6]$")))
    link_penalty = int(0.5 * len(" ".join(a.get_text(" ", strip=True) for a in tag.find_all("a"))))
    return len(text) + paragraph_bonus + heading_bonus - link_penalty


def choose_root(soup: BeautifulSoup) -> Tag:
    """Select the highest-scoring content root for block extraction.

    Page-level roots often win by a tiny margin because they include comments,
    footers, or recommendation blocks. When a non-page container has a similar
    score, prefer it as the more likely article boundary.
    """

    candidates = list(candidate_roots(soup))
    best_score = max(text_score(candidate) for candidate in candidates)
    close_candidates = [candidate for candidate in candidates if text_score(candidate) >= best_score * 0.8]
    return max(close_candidates, key=lambda candidate: (root_priority(candidate), text_score(candidate)))


def root_priority(tag: Tag) -> int:
    """Rank likely article roots ahead of generic page wrappers."""

    labels = ancestry_blob(tag).lower()
    title_bonus = 5 if tag.find(["h1", "h2"], recursive=True) or tag.find(class_="article-title-main") else 0
    if tag.name in {"article", "main"}:
        return 5 + title_bonus
    if any(marker in labels for marker in ("article-container", "content-column", "l-story__main")):
        return 4 + title_bonus
    if any(marker in labels for marker in ("journalfulltext", "widget-articlefulltext", "contenttab")):
        return 3 + title_bonus
    if tag.name in {"body", "html", "[document]"}:
        return title_bonus
    return 1 + title_bonus


def is_content_block_tag(tag: Tag) -> bool:
    """Return whether a tag should become a selectable reader block."""

    if tag.name in CONTENT_TAGS:
        return True
    if tag.get("id") in CONTENT_IDS:
        return True
    if tag.name == "div" and CONTENT_DIV_CLASSES.intersection(tag.get("class", [])):
        return True
    if tag.name == "section" and CONTENT_DIV_CLASSES.intersection(tag.get("class", [])):
        return True
    return False


def is_heading_like_block(tag: Tag) -> bool:
    """Return whether a short block should be kept as a heading."""

    if tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return True
    return tag.name == "div" and {"abstract-title", "h6", "article-title-main"}.intersection(tag.get("class", []))


def ancestry_blob(tag: Tag) -> str:
    """Collect class/id/role labels from a tag and its ancestors."""

    parts = []
    for current in [tag, *list(tag.parents)]:
        if not isinstance(current, Tag):
            continue
        if current.name in {"html", "body"}:
            continue
        parts.extend(
            filter(
                None,
                [
                    current.get("id"),
                    " ".join(current.get("class", [])),
                    current.get("role"),
                    current.get("aria-label"),
                ],
            )
        )
    return " ".join(parts)


def is_block_noise(tag: Tag) -> bool:
    """Skip selectable blocks that live inside post-article UI regions."""

    blob = ancestry_blob(tag)
    lowered_blob = blob.lower()
    noisy_substrings = (
        "articlejump",
        "figuresandtables",
        "figuretab",
        "field-terms",
        "tabletab",
        "rendered-terms",
        "story__tags",
        "supplementtab",
        "tag-list",
    )
    if any(marker in lowered_blob for marker in noisy_substrings):
        return True
    if not blob or not BLOCK_NOISE_RE.search(blob):
        return False
    if (
        tag.name == "figure"
        and tag.find(["img", "figcaption", "p"])
        and "l-story__full" in blob
        and not re.search(r"(?:latest|newest|secondary|sponsor)", blob, re.I)
    ):
        return False
    return True


def page_source_url(soup: BeautifulSoup) -> Optional[str]:
    """Find the original page URL from common saved-page metadata."""

    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical and canonical.get("href"):
        return canonical["href"]
    og_url = soup.find("meta", attrs={"property": "og:url"})
    if og_url and og_url.get("content"):
        return og_url["content"]
    return None


def srcset_candidates(value: str) -> List[tuple[int, str]]:
    """Parse a ``srcset``/``data-srcset`` string into scored image URLs.

    Width descriptors such as ``1120w`` score by pixel width; density
    descriptors such as ``2x`` are scaled into comparable integer scores.
    """

    candidates = []
    for part in value.split(","):
        pieces = part.strip().rsplit(None, 1)
        if not pieces:
            continue
        url = pieces[0]
        descriptor = pieces[1] if len(pieces) > 1 else ""
        score = 0
        if descriptor.endswith("w") and descriptor[:-1].isdigit():
            score = int(descriptor[:-1])
        elif descriptor.endswith("x"):
            try:
                score = int(float(descriptor[:-1]) * 1000)
            except ValueError:
                score = 0
        candidates.append((score, url))
    return candidates


def image_candidates(img: Tag) -> List[str]:
    """Return image URLs from lazy-loading and responsive attributes.

    The list is deduplicated and sorted with the highest-resolution srcset
    candidate first, followed by ordinary ``src``/``data-src`` fallbacks.
    """

    scored: List[tuple[int, int, str]] = []
    order = 0
    for attr in ("src", "data-src", "data-original"):
        value = (img.get(attr) or "").strip()
        if value:
            scored.append((0, order, value))
            order += 1
    for attr in ("srcset", "data-srcset"):
        for score, value in srcset_candidates(img.get(attr) or ""):
            scored.append((score, order, value))
            order += 1
    seen = set()
    urls = []
    for _, _, value in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True):
        if value not in seen:
            seen.add(value)
            urls.append(value)
    return urls


def cache_filename(url: str, content_type: str = "") -> str:
    """Build a stable cache filename for a downloaded remote image."""

    parsed = urlparse(url)
    suffix = Path(unquote(parsed.path)).suffix
    if not suffix:
        suffix = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ".img"
    return f"{uuid.uuid5(uuid.NAMESPACE_URL, url).hex}{suffix[:12]}"


def download_image(url: str, session_id: str, image_cache: Dict[str, Path]) -> Optional[str]:
    """Download a remote image into the per-session cache.

    Returns a Flask URL for the cached image when successful. Failures are
    swallowed deliberately: remote images are opportunistic and the app can
    still fall back to saved local assets or omit the image.
    """

    if not url.startswith(("http://", "https://")):
        return None
    if url in image_cache:
        return f"/downloaded/{session_id}/{image_cache[url].name}"
    request = Request(url, headers={"User-Agent": "html2pdf-local/0.1"})
    try:
        with urlopen(request, timeout=5) as response:
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                return None
            target = CACHE_DIR / session_id / cache_filename(url, content_type)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
        image_cache[url] = target
        return f"/downloaded/{session_id}/{target.name}"
    except Exception:
        return None


def choose_image_src(source_path: Path, img: Tag, session_id: str, image_cache: Dict[str, Path]) -> Optional[str]:
    """Choose the best displayable source for an image tag.

    Remote high-resolution candidates are tried first. If they fail, the
    function prefers a browser-saved local image over a remote URL, because
    local assets are more reliable for offline archives and PDF generation.
    """

    local_fallback = None
    remote_fallback = None
    for candidate in image_candidates(img):
        if candidate.startswith(("http://", "https://")):
            remote_fallback = remote_fallback or candidate
            downloaded = download_image(candidate, session_id, image_cache)
            if downloaded:
                return downloaded
        elif candidate.startswith(("data:", "/asset/", "/downloaded/")):
            return candidate
        elif resolve_asset(source_path, candidate):
            local_fallback = local_fallback or asset_url(source_path, candidate, session_id)
    if local_fallback:
        return local_fallback
    return remote_fallback


def simplify_block(
    tag: Tag,
    source_path: Path,
    session_id: str,
    image_cache: Dict[str, Path],
) -> Optional[ReaderBlock]:
    """Convert one content tag into a cleaned, selectable reader block.

    The function works on a detached copy of the tag, removes nested noise,
    resolves image sources, drops responsive/lazy attributes after choosing an
    image, and filters out very short text-only fragments.
    """

    tag = BeautifulSoup(str(tag), "lxml").find(tag.name)
    if not tag:
        return None
    for child in list(tag.find_all(True)):
        if is_noise(child):
            child.decompose()
            continue
        clean_attributes(child)
    for img in tag.find_all("img"):
        src = choose_image_src(source_path, img, session_id, image_cache)
        if src:
            img["src"] = src
        for attr in ("srcset", "data-src", "data-srcset", "data-original"):
            if attr in img.attrs:
                del img.attrs[attr]
        if not img.get("alt"):
            img["alt"] = ""
    text = tag.get_text(" ", strip=True)
    has_image = bool(tag.find("img"))
    if len(text) < 25 and not has_image and not is_heading_like_block(tag):
        return None
    label = text[:120] if text else "Image"
    block_id = f"b-{uuid.uuid4().hex[:10]}"
    return ReaderBlock(block_id, tag.name, label, str(tag), True)


def extract_blocks(source_path: Path) -> DocumentSession:
    """Load an HTML file and create a complete in-memory session.

    This is the main extraction pipeline: read, clean, choose the article root,
    split it into top-level content blocks, resolve image candidates, and store
    the result in ``SESSIONS`` for later preview/PDF requests.
    """

    raw_html = read_html(source_path)
    source_url = page_source_url(BeautifulSoup(raw_html, "lxml"))
    soup = clean_soup(raw_html, strip_attributes=False)
    title = (soup.title.get_text(" ", strip=True) if soup.title else source_path.stem) or source_path.stem
    blocks: List[ReaderBlock] = []
    session_id = uuid.uuid4().hex
    image_cache: Dict[str, Path] = {}
    root = choose_root(soup)

    for tag in root.find_all(is_content_block_tag, recursive=True):
        if tag.find_parent(is_content_block_tag):
            continue
        if is_block_noise(tag):
            continue
        block = simplify_block(tag, source_path, session_id, image_cache)
        if block:
            blocks.append(block)

    if not blocks:
        for paragraph in root.find_all(lambda tag: tag.name in {"p", "img"} or is_content_block_tag(tag)):
            if is_block_noise(paragraph):
                continue
            block = simplify_block(paragraph, source_path, session_id, image_cache)
            if block:
                blocks.append(block)

    for tag in soup.find_all(True):
        clean_attributes(tag)
    rewrite_preview_assets(soup, source_path, session_id)
    session = DocumentSession(session_id, source_path, source_url, title, str(soup), blocks, image_cache)
    SESSIONS[session_id] = session
    return session


def asset_url(source_path: Path, src: str, session_id: str) -> str:
    """Convert a local relative asset path into a session-scoped Flask URL."""

    src = src.strip()
    if src.startswith(("data:", "http://", "https://", "file:")):
        return src
    return f"/asset/{session_id}?src={src}"


def resolve_asset(source_path: Path, src: str) -> Optional[Path]:
    """Resolve a browser-saved asset path relative to the source HTML file.

    The path is constrained to the source file's directory tree to avoid
    arbitrary file access. A Unicode-normalized fallback handles macOS filename
    composition differences in saved pages.
    """

    src = unquote(src.split("#", 1)[0].split("?", 1)[0])
    path = (source_path.parent / src).resolve()
    try:
        path.relative_to(source_path.parent.resolve())
    except ValueError:
        return None
    if path.exists() and path.is_file():
        return path
    return resolve_normalized_path(source_path.parent.resolve(), src)


def same_name(left: str, right: str) -> bool:
    """Compare filenames after NFC normalization."""

    return unicodedata.normalize("NFC", left) == unicodedata.normalize("NFC", right)


def resolve_normalized_path(base: Path, src: str) -> Optional[Path]:
    """Resolve a relative path by matching each component with NFC names.

    This is slower than a direct ``Path.exists`` lookup, so it is only used as
    a fallback when visually identical Unicode filenames do not compare equal.
    """

    candidate = base
    parts = [part for part in Path(src).parts if part not in {"", "."}]
    if parts and parts[0] == "/":
        return None
    for part in parts:
        if part == "..":
            return None
        if not candidate.exists() or not candidate.is_dir():
            return None
        matches = [child for child in candidate.iterdir() if same_name(child.name, part)]
        if not matches:
            return None
        candidate = matches[0]
    return candidate if candidate.exists() and candidate.is_file() else None


def pdf_ready_html(session: DocumentSession, block: ReaderBlock) -> str:
    """Prepare a reader block for HTML-based PDF engines.

    UI asset URLs are converted to ``file://`` URLs so WeasyPrint can load
    images directly from disk when that optional renderer is enabled.
    """

    soup = BeautifulSoup(block.html, "lxml")
    for img in soup.find_all("img"):
        src = img.get("src", "")
        path = resolve_ui_asset(session, src)
        if path:
            img["src"] = path.as_uri()
    return "".join(str(child) for child in (soup.body or soup).contents)


def reader_document(session: DocumentSession, selected_ids: set[str]) -> str:
    """Build a complete reader-mode HTML document for WeasyPrint output."""

    body = "\n".join(pdf_ready_html(session, block) for block in session.blocks if block.id in selected_ids)
    escaped_title = escape(session.title)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{escaped_title}</title>
  <style>
    @page {{ size: A4; margin: 20mm 17mm; }}
    body {{
      color: #1d1d1f;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 11.5pt;
      line-height: 1.55;
    }}
    article {{ max-width: 720px; margin: 0 auto; }}
    h1, h2, h3, h4 {{ line-height: 1.2; page-break-after: avoid; }}
    h1 {{ font-size: 24pt; margin: 0 0 16pt; }}
    h2 {{ font-size: 17pt; margin: 22pt 0 8pt; }}
    h3 {{ font-size: 14pt; margin: 18pt 0 6pt; }}
    p, ul, ol, blockquote, pre, table, figure {{ margin: 0 0 11pt; }}
    img {{ max-width: 100%; height: auto; display: block; margin: 8pt auto; }}
    figure {{ break-inside: avoid; }}
    figcaption {{ color: #60646c; font-size: 9.5pt; }}
    blockquote {{ border-left: 3px solid #c9cdd3; padding-left: 12pt; color: #383b40; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 9.5pt; }}
    th, td {{ border-bottom: 1px solid #dfe2e6; padding: 4pt 5pt; vertical-align: top; }}
    a {{ color: inherit; text-decoration: none; }}
  </style>
</head>
<body><article>
<h1>{escaped_title}</h1>
{body}
</article></body>
</html>"""


def resolve_ui_asset(session: DocumentSession, src: str) -> Optional[Path]:
    """Map an image URL used in the UI back to a local filesystem path."""

    if src.startswith("/downloaded/"):
        name = Path(unquote(urlparse(src).path)).name
        path = CACHE_DIR / session.id / name
        return path if path.exists() else None
    if src.startswith("/asset/"):
        parsed = urlparse(src)
        query_src = parse_qs(parsed.query).get("src", [""])[0]
        return resolve_asset(session.source_path, query_src)
    if src.startswith("file:"):
        return Path(unquote(urlparse(src).path))
    if src.startswith(("http://", "https://", "data:")):
        return None
    return resolve_asset(session.source_path, src)


def normalize_href(session: DocumentSession, href: str) -> Optional[str]:
    """Return a PDF-safe link target or ``None`` when the link is unusable."""

    href = (href or "").strip()
    if not href or href.startswith("#") or href.lower().startswith("javascript:"):
        return None
    parsed = urlparse(href)
    if parsed.scheme in {"http", "https", "mailto", "tel"}:
        return href
    if parsed.scheme:
        return None
    base = session.source_url or session.source_path.as_uri()
    return urljoin(base, href)


def safe_paragraph_html(tag: Tag, session: DocumentSession) -> str:
    """Reduce a tag to ReportLab-friendly inline markup.

    ReportLab supports only a small HTML subset inside ``Paragraph`` objects,
    so this unwraps unsupported elements while preserving simple emphasis,
    line-breaks, and safe ``<a href=...>`` links.
    """

    allowed = {"a", "b", "strong", "i", "em", "u", "br", "sup", "sub"}
    soup = BeautifulSoup(str(tag), "lxml")
    root = soup.find(tag.name)
    if not root:
        return ""
    for child in root.find_all(True):
        if child.name == "a":
            href = normalize_href(session, child.get("href", ""))
            if href:
                child.attrs = {"href": href, "color": "#1a5fb4"}
            else:
                child.unwrap()
        elif child.name not in allowed:
            child.unwrap()
        else:
            child.attrs = {}
    return root.decode_contents().strip() or root.get_text(" ", strip=True)


def add_image(story: list, session: DocumentSession, img: Tag, max_width: float) -> None:
    """Append a scaled image flowable to a ReportLab story when possible."""

    path = resolve_ui_asset(session, img.get("src", ""))
    if not path:
        return
    try:
        flowable = Image(str(path))
        scale = min(1.0, max_width / flowable.drawWidth)
        flowable.drawWidth *= scale
        flowable.drawHeight *= scale
        story.append(flowable)
        story.append(Spacer(1, 5))
    except Exception:
        return


def generate_reportlab_pdf(session: DocumentSession, selected_ids: set[str], output_path: Path) -> None:
    """Render selected reader blocks into a PDF using ReportLab.

    This is the default renderer because it is pure Python plus Pillow on this
    machine. The conversion is intentionally conservative: headings, lists,
    tables, preformatted text, paragraphs, and images get mapped to ReportLab
    flowables rather than trying to reproduce arbitrary webpage CSS.
    """

    if SimpleDocTemplate is None:
        raise RuntimeError("Neither WeasyPrint nor ReportLab is available for PDF generation.")
    styles = getSampleStyleSheet()
    styles["Normal"].fontName = "Helvetica"
    styles["Normal"].fontSize = 10.5
    styles["Normal"].leading = 16
    styles["Title"].fontName = "Helvetica-Bold"
    styles["Heading2"].fontName = "Helvetica-Bold"
    styles["Heading3"].fontName = "Helvetica-Bold"
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=session.title,
    )
    max_width = A4[0] - 36 * mm
    story = [Paragraph(session.title, styles["Title"]), Spacer(1, 10)]
    for block in session.blocks:
        if block.id not in selected_ids:
            continue
        soup = BeautifulSoup(block.html, "lxml")
        root = (soup.body or soup).find(True)
        if not root:
            continue
        if root.name in {"h1", "h2"}:
            story.append(Paragraph(safe_paragraph_html(root, session), styles["Heading2"]))
        elif root.name in {"h3", "h4", "h5", "h6"}:
            story.append(Paragraph(safe_paragraph_html(root, session), styles["Heading3"]))
        elif root.name in {"ul", "ol"}:
            items = [
                ListItem(Paragraph(safe_paragraph_html(li, session), styles["Normal"]))
                for li in root.find_all("li", recursive=False)
                if li.get_text(" ", strip=True)
            ]
            if items:
                story.append(ListFlowable(items, bulletType="1" if root.name == "ol" else "bullet"))
        elif root.name == "pre":
            story.append(Preformatted(root.get_text("\n", strip=False), styles["Code"]))
        elif root.name == "table":
            rows = []
            for tr in root.find_all("tr"):
                row = [
                    Paragraph(safe_paragraph_html(cell, session), styles["Normal"])
                    for cell in tr.find_all(["th", "td"])
                ]
                if row:
                    rows.append(row)
            if rows:
                table = Table(rows, hAlign="LEFT")
                table.setStyle(
                    TableStyle(
                        [
                            ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                            ("LEADING", (0, 0), (-1, -1), 11),
                        ]
                    )
                )
                story.append(table)
        else:
            for img in root.find_all("img"):
                add_image(story, session, img, max_width)
            text = root.get_text(" ", strip=True)
            if text:
                story.append(Paragraph(safe_paragraph_html(root, session), styles["Normal"]))
        story.append(Spacer(1, 7))
    doc.build(story)


@app.get("/")
def index() -> str:
    """Serve the single-page UI with known archive files and defaults."""

    return render_template("index.html", files=html_files(), output_dir=str(DEFAULT_OUTPUT_DIR))


@app.get("/api/status")
def status():
    """Report runtime capabilities to the browser UI."""

    return jsonify(
        {
            "pdf_available": HTML is not None or SimpleDocTemplate is not None,
            "pdf_engine": "WeasyPrint" if HTML is not None else "ReportLab",
            "default_output_dir": str(DEFAULT_OUTPUT_DIR),
        }
    )


@app.post("/api/load")
def load():
    """Load an uploaded/path-based HTML file and return extracted blocks."""

    uploaded = request.files.get("file")
    path_text = request.form.get("path", "").strip()
    if uploaded and uploaded.filename:
        filename = secure_filename(uploaded.filename) or "source.html"
        path = UPLOAD_DIR / f"{uuid.uuid4().hex}-{filename}"
        uploaded.save(path)
    elif path_text:
        path = Path(path_text).expanduser().resolve()
    else:
        return jsonify({"error": "Choose or enter an HTML file."}), 400
    if not path.exists() or path.suffix.lower() not in {".html", ".htm"}:
        return jsonify({"error": "The selected path is not an .html or .htm file."}), 400
    session = extract_blocks(path)
    return jsonify(
        {
            "session_id": session.id,
            "title": session.title,
            "source_path": str(session.source_path),
            "original_url": f"/original/{session.id}",
            "blocks": [block.__dict__ for block in session.blocks],
        }
    )


@app.get("/original/<session_id>")
def original(session_id: str):
    """Serve the cleaned original-page preview for a loaded session."""

    session = SESSIONS.get(session_id)
    if not session:
        return Response("Unknown session", status=404)
    return Response(session.original_html, mimetype="text/html")


@app.get("/asset/<session_id>")
def asset(session_id: str):
    """Serve a local browser-saved asset through a session-scoped URL."""

    session = SESSIONS.get(session_id)
    if not session:
        return Response("Unknown session", status=404)
    resolved = resolve_asset(session.source_path, request.args.get("src", ""))
    if not resolved:
        return Response("Asset not found", status=404)
    return send_file(resolved, mimetype=mimetypes.guess_type(str(resolved))[0])


@app.get("/downloaded/<session_id>/<name>")
def downloaded_asset(session_id: str, name: str):
    """Serve an image downloaded from a remote srcset/data-src candidate."""

    if session_id not in SESSIONS:
        return Response("Unknown session", status=404)
    path = CACHE_DIR / session_id / Path(name).name
    if not path.exists() or not path.is_file():
        return Response("Asset not found", status=404)
    return send_file(path, mimetype=mimetypes.guess_type(str(path))[0])


@app.post("/api/generate")
def generate():
    """Generate a PDF from the selected block IDs and output settings."""

    if HTML is None and SimpleDocTemplate is None:
        return jsonify({"error": "No PDF engine is available. Install WeasyPrint or ReportLab."}), 500
    payload = request.get_json(force=True)
    session = SESSIONS.get(payload.get("session_id"))
    if not session:
        return jsonify({"error": "Load an HTML file first."}), 400
    selected_ids = set(payload.get("selected_ids") or [])
    if not selected_ids:
        return jsonify({"error": "Select at least one content block."}), 400
    output_dir = Path(payload.get("output_dir") or DEFAULT_OUTPUT_DIR).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    name = secure_filename(payload.get("filename") or f"{session.source_path.stem}.pdf")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    output_path = output_dir / name
    if HTML is not None:
        html = reader_document(session, selected_ids)
        HTML(string=html, base_url=str(session.source_path.parent)).write_pdf(output_path)
    else:
        generate_reportlab_pdf(session, selected_ids, output_path)
    return jsonify({"ok": True, "output_path": str(output_path)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
