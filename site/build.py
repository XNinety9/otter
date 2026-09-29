"""Build the website into _site/: the landing page, plus docs pages rendered from the
repository's own Markdown so they can't drift from it.

    uvx --with markdown --with pymdown-extensions python site/build.py && python3 -m http.server -d _site
"""

import html
import re
import shutil
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
OUT = ROOT / "_site"
REPO = "https://github.com/XNinety9/otter"

# (output page, title, source, nav label)
PAGES = [
    ("guide.html", "Guide", ROOT / "README.md", "Guide"),
    ("protocol.html", "Device protocol", ROOT / "docs" / "protocol.md", "Protocol"),
]
# Repository paths that have a page on the site.
PAGE_FOR = {"README.md": "guide.html", "docs/protocol.md": "protocol.html"}

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} · Otter</title>
  <link rel="icon" type="image/png" href="assets/favicon.png">
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <nav class="nav">
    <div class="wrap">
      <a class="brand" href="./"><img src="assets/logo-mark.png" alt="" width="30" height="30"> Otter</a>
      <div class="links">{nav}<a href="{repo}">GitHub</a></div>
    </div>
  </nav>
  <main class="wrap docs">
    <aside class="toc"><div class="title">On this page</div>{toc}</aside>
    <article class="doc">
      <h1>{title}</h1>
      {content}
      <p class="caption">Rendered from <a href="{repo}/blob/dev/{source}">{source}</a>.</p>
    </article>
  </main>
</body>
</html>
"""


def readme_body(text: str) -> str:
    """The README without its logo, title and badge, which the site shows its own way."""
    text = re.sub(r'^<p align="center">.*?</p>\s*', "", text, flags=re.S)
    text = re.sub(r"^# Otter\s*\n", "", text)
    text = re.sub(r"^\[!\[CI\].*\n", "", text.lstrip(), count=1)
    return re.sub(r"^\*\*Website and docs:.*\n", "", text, flags=re.M)  # we are the website


def rewrite_links(body: str, source: Path) -> str:
    """Links to other Markdown pages point to their site page, other repo files to GitHub."""
    base = source.parent.relative_to(ROOT)

    def fix(match: re.Match) -> str:
        attr, url = match.group(1), html.unescape(match.group(2))
        if re.match(r"^(https?:|mailto:|#)", url):
            return match.group(0)
        path, _, anchor = url.partition("#")
        repo_path = (base / path).as_posix() if str(base) != "." else path
        repo_path = str(Path(repo_path)).replace("\\", "/")
        if repo_path in PAGE_FOR:
            target = PAGE_FOR[repo_path] + (f"#{anchor}" if anchor else "")
        else:
            target = f"{REPO}/blob/dev/{repo_path}" + (f"#{anchor}" if anchor else "")
        return f'{attr}="{html.escape(target)}"'

    return re.sub(r'(href|src)="([^"]+)"', fix, body)


def render(page: str, title: str, source: Path) -> str:
    text = source.read_text()
    if source.name == "README.md":
        text = readme_body(text)
    else:
        text = re.sub(r"^# .*\n", "", text, count=1)  # the template shows the title
    md = markdown.Markdown(
        # superfences: code blocks nested in lists (fenced_code turns them into inline code).
        extensions=["pymdownx.superfences", "tables", "toc", "sane_lists", "attr_list"],
        extension_configs={"toc": {"permalink": "#", "toc_depth": "2-3"}},
    )
    body = rewrite_links(md.convert(text), source)
    nav = "".join(
        f'<a href="{p}"{" aria-current=\"page\"" if p == page else ""}>{label}</a>' for p, _, _, label in PAGES
    )
    return TEMPLATE.format(
        title=title, nav=nav, toc=md.toc, content=body, repo=REPO, source=source.relative_to(ROOT).as_posix()
    )


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(SITE / "assets", OUT / "assets")
    for name in ("index.html", "style.css"):
        shutil.copy(SITE / name, OUT / name)
    for page, title, source, _ in PAGES:
        (OUT / page).write_text(render(page, title, source))
    (OUT / ".nojekyll").touch()  # serve files as they are
    print(f"built {OUT.relative_to(ROOT)}: " + ", ".join(sorted(p.name for p in OUT.iterdir() if p.is_file())))


if __name__ == "__main__":
    main()
