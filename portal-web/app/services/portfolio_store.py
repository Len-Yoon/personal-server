import os
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt


DEFAULT_PORTFOLIO_CONTENT_PATH = Path("/data/files/.portfolio/portfolio.md")
PORTFOLIO_CONTENT_PATH = Path(
    os.getenv("PORTFOLIO_CONTENT_PATH", str(DEFAULT_PORTFOLIO_CONTENT_PATH))
)
_MARKDOWN_RENDERER = MarkdownIt("commonmark", {"html": False})


def load_portfolio_content() -> str:
    """Return the saved portfolio source, or an empty document before first save."""
    try:
        return PORTFOLIO_CONTENT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def save_portfolio_content(content: str) -> None:
    """Atomically replace the saved UTF-8 Markdown portfolio document."""
    PORTFOLIO_CONTENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=PORTFOLIO_CONTENT_PATH.parent,
        prefix=f".{PORTFOLIO_CONTENT_PATH.name}.",
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)
        temporary_file.write(content)
        temporary_file.flush()
        os.fsync(temporary_file.fileno())

    try:
        os.replace(temporary_path, PORTFOLIO_CONTENT_PATH)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def render_portfolio_markdown(content: str) -> str:
    """Render CommonMark source without allowing embedded raw HTML."""
    return _MARKDOWN_RENDERER.render(content)
