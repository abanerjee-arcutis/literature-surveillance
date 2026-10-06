"""The Literature Update page: template.html with its sample data replaced. The app builds it from the tables on every
request; surveillance.py, run without --tables, writes it to a file."""
import json
import os
import re
from datetime import date, datetime

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")
SOURCE_NAMES = {"pubmed": "PubMed", "openalex": "OpenAlex", "scholar": "Google Scholar"}


def header(sources, since: date, until: date, generated: datetime) -> str:
    """The line under the page title: the sources searched, the search window and when the run was."""
    label = ", ".join(name for s, name in SOURCE_NAMES.items() if s in sources)
    return f"{label} surveillance · {since:%d %b} – {until:%d %b %Y} · generated {generated:%d %b %Y %H:%M}"


def fill(template: str, rows: list[dict], header_line: str, settings: dict | None = None) -> str:
    """The template with its sample DATA (and SETTINGS, when given) and header line replaced."""
    def js(value):
        return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")
    # replacement functions, not strings: re.sub would turn the JSON's \n and \\ escapes back into characters
    html = re.sub(r"const DATA = \[[\s\S]*?\n\];", lambda m: f"const DATA = {js(rows)};", template, count=1)
    if settings is not None:
        html = re.sub(r"const SETTINGS = \{[\s\S]*?\n\};", lambda m: f"const SETTINGS = {js(settings)};", html, count=1)
    return html.replace("PubMed and Google Scholar surveillance · sample batch, 30 Jun 2026", header_line)
