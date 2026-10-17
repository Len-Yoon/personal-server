import fcntl
import hashlib
import os
import re
import tempfile
from pathlib import Path
from threading import RLock

from markdown_it import MarkdownIt


DEFAULT_PORTFOLIO_CONTENT_PATH = Path("/data/files/.portfolio/portfolio.md")
PORTFOLIO_CONTENT_PATH = Path(
    os.getenv("PORTFOLIO_CONTENT_PATH", str(DEFAULT_PORTFOLIO_CONTENT_PATH))
)
_MARKDOWN_RENDERER = MarkdownIt("commonmark", {"html": False})
_SAVE_LOCK = RLock()


class PortfolioConflict(ValueError):
    def __init__(self, content: str, revision: str):
        super().__init__("다른 창에서 저장한 내용이 있습니다. 최신 내용과 비교한 뒤 다시 저장해주세요.")
        self.current_content = content
        self.current_revision = revision


def _revision(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_portfolio_content() -> str:
    """Return the saved portfolio source, or an empty document before first save."""
    try:
        return PORTFOLIO_CONTENT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def load_portfolio_snapshot() -> tuple[str, str]:
    # One read supplies both editor content and its version, including previews.
    content = load_portfolio_content()
    return content, _revision(content)


def save_portfolio_content(content: str, *, expected_revision: str) -> str:
    """Compare and atomically replace under thread and inter-process locks."""
    # HTML form encodings can send CRLF; snapshots use universal LF newlines.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    PORTFOLIO_CONTENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _SAVE_LOCK:
        directory_fd = os.open(PORTFOLIO_CONTENT_PATH.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # The stable parent inode survives replacement of the Markdown file.
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            current, revision = load_portfolio_snapshot()
            if not re.fullmatch(r"[a-f0-9]{64}", expected_revision or ""):
                raise PortfolioConflict(current, revision)
            if expected_revision != revision and content != current:
                raise PortfolioConflict(current, revision)
            if content == current:
                return revision  # A lost-response retry never needs another write.
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=PORTFOLIO_CONTENT_PATH.parent,
                    prefix=f".{PORTFOLIO_CONTENT_PATH.name}.", delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    temporary_file.write(content)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
                os.replace(temporary_path, PORTFOLIO_CONTENT_PATH)
                os.fsync(directory_fd)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            return _revision(content)
        finally:
            os.close(directory_fd)


def render_portfolio_markdown(content: str) -> str:
    """Render CommonMark source without allowing embedded raw HTML."""
    return _MARKDOWN_RENDERER.render(content)
