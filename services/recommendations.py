import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ARTICLES_DIR = BASE_DIR / "data" / "recommendations" / "articles"
PRODUCTS_DIR = BASE_DIR / "data" / "recommendations" / "products"


def _clean_slug(filename: str) -> str:
    name = filename.rsplit(".", 1)[0]
    if name[:2].isdigit() and name[2:3] == "_":
        name = name[3:]
    return name


def _format_markdown_links(text: str) -> str:
    # Convert markdown links [Label](url) to HTML <a href="url">Label</a>
    return re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r'<a href="\2">\1</a>', text)


def load_items(directory: Path) -> dict[str, dict]:
    items = {}
    if not directory.exists():
        return items

    for file_path in sorted(directory.glob("*.md")):
        slug = _clean_slug(file_path.name)
        raw_text = file_path.read_text(encoding="utf-8").strip()

        lines = raw_text.splitlines()
        title = slug
        content_lines = []

        if lines and lines[0].startswith("# "):
            title = lines[0][2:].strip()
            content_lines = lines[1:]
        else:
            content_lines = lines

        content = "\n".join(content_lines).strip()
        links = re.findall(r"\[([^\]]+)\]\((https?://[^\)]+)\)", content)
        html_content = _format_markdown_links(content)

        items[slug] = {
            "slug": slug,
            "title": title,
            "text": html_content,
            "raw": raw_text,
            "links": links,
        }

    return items


def get_articles() -> dict[str, dict]:
    return load_items(ARTICLES_DIR)


def get_products() -> dict[str, dict]:
    return load_items(PRODUCTS_DIR)


def get_article(slug: str) -> dict | None:
    return get_articles().get(slug)


def get_product(slug: str) -> dict | None:
    return get_products().get(slug)
