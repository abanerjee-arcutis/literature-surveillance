"""Parse the INFORM 'Articles of Interest' emails (PDF) into one record per article cited in the email body.

    python parse_emails.py <pdf dir>          (writes work/email_articles.json)

The PDFs are the emails as printed, with the attached articles after the signature; only the body is read.

Each record: email subject/date, tier (main = the summarised top part; fyi = 'A few more articles FYI',
'not ordered', 'case reports' sections), the citation as written, a guessed title, every link (visible URLs plus
the ReadCube/Supplement hyperlinks under the citation), PMID and DOI found in those links, and the commentary.
"""
import json
import os
import re
import sys
import urllib.parse

from pypdf import PdfReader

HEADER = re.compile(r"^(a few more articles|other articles|not ordered|did not order|didn.t order|case reports \(|"
                    r"medical letters|several ruxolitinib|i typically don.t include)", re.I)
FYI_HEADER = re.compile(r"few more|other articles|not ordered|did not order|didn.t order|case reports|several ruxolitinib|"
                        r"typically don.t include", re.I)
AUTH = r"[A-Z][\w'’\-]+(?: [A-Z][\w'’\-]+)?"
CITE_LOOSE = re.compile(rf"^({AUTH},? [A-Z]{{1,3}}\b|Comparison chart|Drugs for atopic)", re.U)
CITE_STRICT = re.compile(rf"^{AUTH},? [A-Z]{{1,3}}(\.|,| et al)", re.U)
BULLET = re.compile(r"^(\*|·|•|ｷ|-\s*\S|•|)")
MARKER = re.compile(r"^(ReadCube|Readcube|Supplement|Link to Supplement|PLEASE HOLD|Updated citation)", re.I)
URL = re.compile(r"https?://\S+")


def body_pages(reader):
    pages = []
    for p in reader.pages:
        t = p.extract_text() or ""
        links = []
        for a in p.get("/Annots") or []:
            a = a.get_object()
            uri = (a.get("/A") or {}).get("/URI")
            if uri:
                links.append(str(uri))
        cut = re.search(r"Melina Z\. Bayramyan, Pharm|Kindest regards", t)
        pages.append((t[:cut.start()] if cut else t, links))
        if cut:
            break
    return pages


def norm_url(u):
    return re.sub(r"[^a-z0-9]", "", urllib.parse.unquote(u).lower())


def pmid_of(u):
    m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", u)
    return m.group(1) if m else None


def doi_of(u):
    u = urllib.parse.unquote(u)
    m = re.search(r"10\.\d{4,9}/[^\s?#&]+", u)
    if not m:
        return None
    d = re.sub(r"(/(full|abstract|abs|pdf|epdf|fulltext|full-text|html|meta|summary))+/?$|\.pdf$", "", m.group(0), flags=re.I)
    d = re.sub(r"~.*$", "", d)                     # ovid appends ~slug
    return d.lower().rstrip(".")


def guess_title(cite):
    c = re.sub(r"\s+", " ", cite).strip()
    m = re.search(r"\bet al\.?,?\s*", c)
    if m:
        rest = c[m.end():]
    else:
        m = re.search(r"(?:^|[ ,])[A-Z]{1,3}\.\s+(?=\S)", c)      # last initials of the author list
        rest = c[m.end():] if m and m.start() < 400 else c
    rest = re.sub(r"^[.,\s]+", "", rest)
    rest = re.sub(r"\(?\$\$? ?not ordered\)?", "", rest, flags=re.I)
    rest = re.sub(r"\s*DOI:.*$", "", rest)
    title = re.split(r"(?<=[a-z0-9\)\?\"”])[.?]\s+(?=[A-Z])", rest, maxsplit=1)[0]
    if title.endswith(("?",)) is False and rest.startswith(title + "?"):
        title += "?"
    return title.strip(' ."“”')


def parse(path):
    reader = PdfReader(path)
    pages = body_pages(reader)
    text = "\n".join(t for t, _ in pages)
    annots = [u for _, ls in pages for u in ls]
    lines = text.split("\n")
    head = {}
    for ln in lines[:12]:
        for k in ("Subject", "Date"):
            if ln.startswith(k + ":"):
                head[k.lower()] = ln.split(":", 1)[1].strip()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("As always")) + 1
    entries, cur, tier, section, prev_kind = [], None, "main", "", "blank"
    i = start
    while i < len(lines):
        raw = lines[i]
        ln = raw.replace("\xa0", " ").strip()
        i += 1
        if not ln:
            prev_kind = "blank"
            continue
        if ln.startswith(("Team, congratulations", "Kindest regards", "Melina")):
            continue
        if HEADER.match(ln) and not URL.match(ln):
            section = ln
            tier = "fyi" if FYI_HEADER.search(ln) else tier
            prev_kind = "header"
            continue
        starts = (prev_kind in ("blank", "header") and CITE_LOOSE.match(ln)) or (
            prev_kind == "comment" and CITE_STRICT.match(ln) and re.search(r"\bet al\b|^[^.]{2,40}\. [A-Z]", ln))
        if starts and not (cur and cur["_state"] == "cite" and prev_kind != "blank"):
            cur = dict(email=head.get("subject"), email_date=head.get("date"), tier=tier, section=section,
                       citation=ln, urls=[], comment="", _state="cite")
            entries.append(cur)
            prev_kind = "cite"
            # a URL on the same line as the citation
            m = URL.search(ln)
            if m:
                cur["citation"] = ln[:m.start()].strip()
                cur["urls"].append(m.group(0))
                cur["_state"] = "urls"
            continue
        if cur is None:
            continue
        if URL.match(ln):
            u = URL.match(ln).group(0)
            # a URL broken over two lines: the next line has no spaces and is not a marker
            while i < len(lines) and len(u) > 90 and lines[i].strip() and " " not in lines[i].strip() \
                    and not MARKER.match(lines[i].strip()) and not URL.match(lines[i].strip()) and not BULLET.match(lines[i].strip()):
                u += lines[i].strip()
                i += 1
            cur["urls"].append(u)
            cur["_state"] = "urls"
            prev_kind = "url"
            continue
        if MARKER.match(ln):
            cur.setdefault("markers", []).append(ln)
            if cur["_state"] == "cite":
                cur["_state"] = "urls"
            m = URL.search(ln)
            if m:
                cur["urls"].append(m.group(0))
            prev_kind = "marker"
            continue
        if BULLET.match(ln) or cur["_state"] == "comment":
            cur["comment"] += (" " if cur["comment"] else "") + re.sub(r"^(\*|·|•|ｷ|•|)\s*[�\s]*", "", ln)
            cur["_state"] = "comment"
            prev_kind = "comment"
            continue
        if cur["_state"] == "cite":
            m = re.search(r"\bDOI:\s*(10\.\S+)", ln)
            if m:
                cur["text_doi"] = m.group(1).rstrip(".").lower()
            cur["citation"] += " " + ln
            prev_kind = "cite"
            continue
        # text after the links but before a bullet (a note such as 'In case report bibliography already')
        cur["comment"] += (" " if cur["comment"] else "") + ln
        cur["_state"] = "comment"
        prev_kind = "comment"

    # hyperlinks: the visible URLs anchor each entry in the annotation list; the annotations that follow an
    # entry's last visible URL, up to the next entry's first visible URL, are its ReadCube / supplement links
    keys = [norm_url(a) for a in annots]
    pos = 0
    anchors = []
    for e in entries:
        first = None
        for u in e["urls"]:
            nu = norm_url(u)
            for j in range(pos, len(keys)):
                if keys[j][:60] == nu[:60]:
                    first = j if first is None else first
                    pos = j + 1
                    break
        anchors.append((first, pos))
    for k, e in enumerate(entries):
        first, end = anchors[k]
        if first is None:
            e["links"] = list(e["urls"])
            continue
        nxt = next((anchors[m][0] for m in range(k + 1, len(entries)) if anchors[m][0] is not None), len(annots))
        e["links"] = list(dict.fromkeys(e["urls"] + annots[end:nxt]))
    for e in entries:
        e.pop("_state", None)
        e["citation"] = re.sub(r"\s+", " ", e["citation"]).strip()
        e["title_guess"] = guess_title(e["citation"])
        e["pmid"] = next((pmid_of(u) for u in e["urls"] if pmid_of(u)), None)
        dois = [doi_of(u) for u in e["links"] if doi_of(u) and "readcube.com/library" not in u]
        if e.get("text_doi"):
            dois.insert(0, e["text_doi"])
        e["dois"] = list(dict.fromkeys(dois))
        y = re.findall(r"\b20[12]\d\b", e["citation"])
        e["year"] = y[-1] if y else None
    return entries


if __name__ == "__main__":
    src = sys.argv[1]
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "work"), exist_ok=True)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work", "email_articles.json")
    allrec = []
    for name in sorted(os.listdir(src)):
        if name.lower().endswith(".pdf"):
            recs = parse(os.path.join(src, name))
            for r in recs:
                r["pdf"] = name
            allrec += recs
            print(f"{len(recs):3d}  {name}")
    for n, r in enumerate(sorted(allrec, key=lambda r: r["email_date"] or ""), 1):
        r["email_item"] = n
    json.dump(sorted(allrec, key=lambda r: r["email_item"]), open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(len(allrec), "articles")
