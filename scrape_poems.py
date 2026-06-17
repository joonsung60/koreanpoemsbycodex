#!/usr/bin/env python3
"""Collect Korean Wikisource translated poems as raw TXT plus metadata.

The scraper intentionally avoids modernization, spelling correction, and LLM/API
rewriting.  It uses MediaWiki API responses and rendered Wikisource HTML, then
writes the repository raw text convention: line breaks are ``\n`` and stanza
breaks are exactly ``\n\n\n``.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


BASE = "https://ko.wikisource.org"
API = BASE + "/w/api.php"
OUT = Path("data/wikisource_translated_poems")
HEADERS = {"User-Agent": "koreanpoemsbycodex/2.2 (educational text preservation)"}

DEFAULT_SEEDS = (
    "https://ko.wikisource.org/wiki/박용철_번역시집",
    "https://ko.wikisource.org/wiki/번역:악의_꽃",
    "https://ko.wikisource.org/wiki/번역:삶이_그대를_속일지라도",
    "https://ko.wikisource.org/wiki/번역:애너벨_리",
)

SEARCH_TERMS = (
    '"번역시집"',
    '"번역 시집"',
    '"시편" "역자"',
    '"시집" "역자"',
)

NON_POEM_TITLE_HINTS = (
    "로빈슨 크루소",
    "공산당 선언",
    "게티즈버그 연설",
    "국제연합",
    "대한민국",
    "고려사",
    "논어",
    "도덕경",
    "돈키호테",
    "동물 농장",
    "대헌장",
    "법률",
    "규정",
    "결의",
    "성경",
    "시편",  # Biblical Psalms, not poetry for this corpus.
    "산문집",
    "중국문학오십년사",
    "하타요가",
    "라자 요가",
    "바가바드 기타",
    "묵자",
    "삼국사기",
    "삼국유사",
    "선조소경대왕수정실록",
    "소공녀",
    "이상한 나라의 앨리스",
    "오즈의 마법사",
    "톰 소여의 모험",
    "미적분",
    "손자병법",
    "사서장구집주",
)

POEM_CATEGORY_HINTS = {"시", "시집", "시가"}


@dataclass
class Candidate:
    title: str
    source_collection: str
    translator_type: str
    force_poem: bool = False
    relation: str = ""
    discovery: str = ""


@dataclass
class Entry:
    title: str
    filename: str
    source_url: str
    source_collection: str
    original_title: str = ""
    original_author: str = ""
    translator: list[str] = field(default_factory=list)
    translator_type: str = "unknown"
    original_language: str = ""
    license_or_status: str = "review_required"
    relation: str = ""
    source_page_title: str = ""
    collection_dir: str = ""


@dataclass
class Page:
    title: str
    pageid: int
    html: str
    categories: list[str]
    links: list[str]


def api(params: dict[str, str], retries: int = 6) -> dict:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(API + "?" + query, headers=HEADERS)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(min(10, 1.5 * (attempt + 1)))
    raise RuntimeError(f"API request failed: {params}") from last


def page_url(title: str) -> str:
    return BASE + "/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe="/:")


def title_from_url(value: str) -> str:
    if value.startswith("http"):
        tail = urllib.parse.urlparse(value).path.split("/wiki/", 1)[-1]
        return urllib.parse.unquote(tail).replace("_", " ")
    return value.replace("_", " ")


def safe_name(value: str) -> str:
    value = value.replace("번역:", "translation_")
    value = re.sub(r"[\\/*?:\"<>|]", "_", value).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:130] or "untitled"


def clean_text(text: str) -> str:
    text = html.unescape(text)
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    lines = [ln.strip() for ln in text.splitlines()]
    kept: list[str] = []
    for line in lines:
        if not line:
            kept.append("")
            continue
        if line in {"[편집]", "편집", "←", "→"}:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        kept.append(line)
    while kept and not kept[0]:
        kept.pop(0)
    while kept and not kept[-1]:
        kept.pop()
    text = "\n".join(kept)
    text = re.sub(r"\n{2,}", "\n\n\n", text)
    return text.strip()


class TextExtractor(HTMLParser):
    def __init__(self, skip_noexport: bool = True):
        super().__init__()
        self.skip_noexport = skip_noexport
        self.skip_stack: list[str] = []
        self.buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k: v or "" for k, v in attrs}
        cls = d.get("class", "")
        node_id = d.get("id", "")
        skip_classes = (
            "licenseContainer",
            "mw-editsection",
            "pagenum",
            "reference",
            "metadata",
            "noprint",
        )
        if tag in {"style", "script", "sup"}:
            self.skip_stack.append(tag)
            return
        if self.skip_noexport and "ws-noexport" in cls:
            self.skip_stack.append(tag)
            return
        if any(part in cls for part in skip_classes) or node_id in {"catlinks"}:
            self.skip_stack.append(tag)
            return
        if self.skip_stack:
            return
        if tag == "br":
            self.buf.append("\n")
        elif tag in {"p", "div", "dl", "dd", "li", "tr", "h1", "h2", "h3"}:
            self.buf.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_stack:
            if self.skip_stack[-1] == tag:
                self.skip_stack.pop()
            return
        if tag in {"p", "div", "dl", "dd", "li", "tr", "h1", "h2", "h3"}:
            self.buf.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_stack:
            if data.strip():
                self.buf.append(data)


def html_to_text(fragment: str, skip_noexport: bool = True) -> str:
    parser = TextExtractor(skip_noexport=skip_noexport)
    parser.feed(fragment)
    return clean_text("".join(parser.buf))


def strip_tags(fragment: str) -> str:
    return html_to_text(fragment, skip_noexport=False).replace("\n", " ").strip()


def poem_html_to_text(fragment: str) -> str:
    line_token = "@@WIKISOURCE_LINE_BREAK@@"
    stanza_token = "@@WIKISOURCE_STANZA_BREAK@@"
    fragment = re.sub(r"<style\b.*?</style>", "", fragment, flags=re.S)
    fragment = re.sub(r"<script\b.*?</script>", "", fragment, flags=re.S)
    fragment = re.sub(r"<sup\b.*?</sup>", "", fragment, flags=re.S)
    fragment = re.sub(r"<span\b[^>]*\bpagenum\b[^>]*>.*?</span>", "", fragment, flags=re.S)
    fragment = re.sub(r"<span\b[^>]*\bws-noexport\b[^>]*>.*?</span>", "", fragment, flags=re.S)
    fragment = re.sub(r"</p>\s*<p\b[^>]*>", stanza_token, fragment, flags=re.S)
    fragment = re.sub(r"(?:<br\s*/?>\s*){2,}", stanza_token, fragment, flags=re.I)
    fragment = re.sub(r"<br\s*/?>", line_token, fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = html.unescape(fragment)
    fragment = re.sub(r"[ \t]*[\r\n]+[ \t]*", " ", fragment)
    fragment = re.sub(rf"\s*{re.escape(stanza_token)}\s*", "\n\n\n", fragment)
    fragment = re.sub(rf"\s*{re.escape(line_token)}\s*", "\n", fragment)
    return clean_text(fragment)


def parse_page(title: str) -> Page:
    data = api(
        {
            "action": "parse",
            "page": title,
            "prop": "text|categories|links",
            "format": "json",
            "redirects": "1",
        }
    )
    parsed = data["parse"]
    cats = [c.get("*", "") for c in parsed.get("categories", []) if c.get("*")]
    links = [l.get("*", "") for l in parsed.get("links", []) if l.get("ns") in {0, 114} and l.get("*")]
    return Page(parsed["title"], int(parsed.get("pageid", 0)), parsed["text"]["*"], cats, links)


def allpages(namespace: int, prefix: str | None = None) -> list[str]:
    params = {
        "action": "query",
        "list": "allpages",
        "apnamespace": str(namespace),
        "aplimit": "500",
        "format": "json",
    }
    if prefix:
        params["apprefix"] = prefix
    titles: list[str] = []
    while True:
        data = api(params)
        titles.extend(p["title"] for p in data.get("query", {}).get("allpages", []))
        cont = data.get("continue", {}).get("apcontinue")
        if not cont:
            break
        params["apcontinue"] = cont
    return titles


def search_pages(query: str) -> list[str]:
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srnamespace": "0|114",
        "srlimit": "50",
        "format": "json",
    }
    titles: list[str] = []
    while True:
        data = api(params)
        titles.extend(p["title"] for p in data.get("query", {}).get("search", []))
        cont = data.get("continue", {}).get("sroffset")
        if not cont:
            break
        params["sroffset"] = str(cont)
    return titles


def collection_for(title: str) -> str:
    if "/" in title:
        return title.rsplit("/", 1)[0]
    if title.startswith("번역:"):
        return "번역 namespace standalone"
    return title


def translator_type_for(title: str) -> str:
    if title.startswith("번역:"):
        return "wikisource_user_translation"
    if "번역시집" in title or "번역 시집" in title:
        return "named_historical_translator"
    return "unknown"


def candidate_for(title: str, discovery: str, force: bool = False, relation: str = "") -> Candidate:
    return Candidate(
        title=title,
        source_collection=collection_for(title),
        translator_type=translator_type_for(title),
        force_poem=force,
        relation=relation,
        discovery=discovery,
    )


def discover(seed_values: Iterable[str], include_translation_namespace: bool, include_search: bool, logs: Path) -> list[Candidate]:
    found: dict[str, Candidate] = {}

    def add(cand: Candidate) -> None:
        old = found.get(cand.title)
        if old:
            old.force_poem = old.force_poem or cand.force_poem
            if cand.relation and cand.relation not in old.relation:
                old.relation = "; ".join(x for x in (old.relation, cand.relation) if x)
            return
        found[cand.title] = cand

    for raw_seed in seed_values:
        seed = title_from_url(raw_seed)
        add(candidate_for(seed, "seed", force=False, relation="seed_page"))
        try:
            page = parse_page(seed)
        except Exception as exc:
            log(logs / "failures.log", f"seed\t{seed}\t{exc}")
            continue
        collection_like = bool({"시집", "시"} & set(page.categories)) or "악의 꽃" in seed or "번역시집" in seed
        for link in page.links:
            if link.startswith(seed + "/") or link.startswith(collection_for(seed) + "/"):
                add(candidate_for(link, "seed_link", force=collection_like, relation="same_collection"))
        if collection_like:
            ns = 114 if seed.startswith("번역:") else 0
            prefix = seed + "/"
            if ns == 114:
                prefix = prefix.removeprefix("번역:")
            try:
                for child in allpages(ns, prefix):
                    add(candidate_for(child, "seed_subpage", force=True, relation="same_collection"))
            except Exception as exc:
                log(logs / "failures.log", f"subpages\t{seed}\t{exc}")

    if include_search:
        for term in SEARCH_TERMS:
            try:
                for title in search_pages(term):
                    force = "번역시집" in title or "번역 시집" in title or "/번역시집" in title
                    if "시편" in title and "박용철 번역시집" in title:
                        force = True
                    add(candidate_for(title, f"search:{term}", force=force, relation="search_hit"))
            except Exception as exc:
                log(logs / "failures.log", f"search\t{term}\t{exc}")

    if include_translation_namespace:
        try:
            for title in allpages(114):
                add(candidate_for(title, "translation_namespace_scan"))
        except Exception as exc:
            log(logs / "failures.log", f"translation_namespace\t{exc}")

    return list(found.values())


def is_obvious_non_poem(title: str) -> bool:
    return any(hint in title for hint in NON_POEM_TITLE_HINTS)


def is_poem_page(page: Page, cand: Candidate) -> bool:
    if is_obvious_non_poem(page.title):
        return False
    has_poem_div = '<div class="poem"' in page.html or "class=\"poem\"" in page.html
    if not has_poem_div and "/" not in page.title and (
        "시집" in page.categories or "번역시집" in page.title or "악의 꽃" in page.title
    ):
        return False
    if cand.force_poem:
        return True
    if has_poem_div:
        return True
    cats = set(page.categories)
    if cats & POEM_CATEGORY_HINTS and ("번역" in cats or page.title.startswith("번역:")):
        return True
    header = strip_tags(page.html[:5000])
    if "역자:" in header and ("시편" in header or "시집" in header):
        return True
    return False


def extract_id_text(page_html: str, node_id: str) -> str:
    match = re.search(rf'<span[^>]+id="{re.escape(node_id)}"[^>]*>(.*?)</span>', page_html, re.S)
    return strip_tags(match.group(1)) if match else ""


def split_names(value: str) -> list[str]:
    value = re.sub(r"\[[^\]]+\]", "", value)
    parts = re.split(r",|/| 및 |와 |과 |ㆍ|·", value)
    return [p.strip() for p in parts if p.strip()]


def infer_meta(page: Page, cand: Candidate) -> tuple[str, list[str], str, str]:
    header_html = page.html.split('<div class="prp-pages-output"', 1)[0].split('<div class="poem"', 1)[0]
    header_text = html_to_text(header_html, skip_noexport=False)
    author = extract_id_text(page.html, "ws-저자")
    translator_text = extract_id_text(page.html, "ws-역자")

    if not author:
        m = re.search(r"저자:\s*([^\n,]+)", header_text)
        if m:
            author = m.group(1).strip()
    if not translator_text:
        m = re.search(r"역자:\s*([^\n]+)", header_text)
        if m:
            translator_text = m.group(1).strip()

    translator = split_names(translator_text)
    if page.title.startswith("번역:") and not translator:
        translator = ["위키문헌"]

    lang = ""
    m = re.search(r"([가-힣]+어)에서 번역", header_text)
    if m:
        lang = m.group(1)

    translator_type = cand.translator_type
    if page.title.startswith("번역:"):
        translator_type = "wikisource_user_translation"
    elif translator:
        translator_type = "named_historical_translator"
    return author, translator, translator_type, lang


def license_status(page: Page) -> str:
    cats = set(page.categories)
    text = strip_tags(page.html[-9000:])
    if "CC-BY-SA" in text or "GFDL" in text or "크리에이티브 커먼즈" in text:
        return "CC-BY-SA/GFDL or mixed; review_required"
    if any(c.startswith("PD-") for c in cats) or "퍼블릭 도메인" in text or "Public domain" in text:
        return "public_domain_claimed_by_source; review_required"
    return "review_required"


def title_from_label(label_html: str, fallback: str) -> str:
    label = strip_tags(label_html)
    label = re.sub(r"\s+", " ", label).strip(" -")
    if not label:
        return fallback
    if len(label) > 90:
        return fallback
    return label


def poem_blocks(page: Page) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    pattern = re.compile(
        r"<div\b[^>]*class=\"[^\"]*\bpoem\b[^\"]*\"[^>]*>(?P<body>.*?)</div>",
        re.S,
    )
    fallback = page.title.replace("번역:", "").split("/")[-1]
    for match in pattern.finditer(page.html):
        before = page.html[max(0, match.start() - 1600) : match.start()]
        labels = re.findall(r"<dl\b.*?</dl>", before, flags=re.S)
        title = title_from_label(labels[-1] if labels else "", fallback)
        body = poem_html_to_text(match.group("body"))
        if body:
            blocks.append((title, body))
    merged: list[tuple[str, str]] = []
    for title, body in blocks:
        if merged and merged[-1][0] == title:
            merged[-1] = (title, clean_text(merged[-1][1] + "\n\n\n" + body))
        else:
            merged.append((title, body))
    blocks = merged
    if blocks:
        return blocks

    text = html_to_text(page.html)
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines[:20]):
        if "역자:" in line or "저자:" in line or line == fallback:
            start = i + 1
    end = len(lines)
    for i, line in enumerate(lines):
        if line in {"저작권", "라이선스"} or line.startswith(("이 저작물", "분류:", "원본 주소")):
            end = i
            break
    body = clean_text("\n".join(lines[start:end]))
    return [(fallback, body)] if body else []


def log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(message.rstrip() + "\n")


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def entry_from_dict(data: dict) -> Entry:
    fields = Entry.__dataclass_fields__
    return Entry(**{name: data.get(name, fields[name].default) for name in fields})


def load_existing_entries() -> list[Entry]:
    index_path = OUT / "index.json"
    if not index_path.exists():
        return []
    return [entry_from_dict(item) for item in json.loads(index_path.read_text(encoding="utf-8"))]


def collect(
    candidates: Iterable[Candidate],
    delay: float,
    limit: int | None,
    logs: Path,
    base_entries: list[Entry] | None = None,
) -> list[Entry]:
    base_entries = base_entries or []
    entries: list[Entry] = []
    used_paths: set[Path] = {
        OUT / entry.collection_dir / "raw" / entry.filename
        for entry in base_entries
        if entry.collection_dir and entry.filename
    }
    title_seen: dict[tuple[str, str], str] = {
        (entry.title, entry.original_author): entry.source_page_title
        for entry in base_entries
    }
    existing_sources = {entry.source_page_title for entry in base_entries if entry.source_page_title}
    processed = 0

    for cand in candidates:
        if limit is not None and processed >= limit:
            break
        processed += 1
        if processed == 1 or processed % 25 == 0:
            print(f"processing candidate {processed}", flush=True)
            if entries:
                write_indices(base_entries + entries)
        if cand.title in existing_sources:
            log(logs / "excluded.log", f"{page_url(cand.title)}\talready_collected\t{cand.discovery}")
            continue
        if is_obvious_non_poem(cand.title):
            log(logs / "excluded.log", f"{page_url(cand.title)}\tnon_poem_title_precheck\t{cand.discovery}")
            continue
        try:
            page = parse_page(cand.title)
        except Exception as exc:
            log(logs / "failures.log", f"page\t{cand.title}\t{exc}")
            continue

        if not is_poem_page(page, cand):
            log(logs / "excluded.log", f"{page_url(page.title)}\tnot_poem_candidate\t{cand.discovery}")
            time.sleep(delay)
            continue

        author, translator, translator_type, lang = infer_meta(page, cand)
        source_collection = cand.source_collection
        if page.title.startswith(source_collection + "/"):
            source_collection = cand.source_collection
        elif "/" in page.title:
            source_collection = page.title.rsplit("/", 1)[0]

        blocks = poem_blocks(page)
        if not blocks:
            log(logs / "excluded.log", f"{page_url(page.title)}\tno_body")
            time.sleep(delay)
            continue

        for block_title, body in blocks:
            line_count = len([ln for ln in body.splitlines() if ln.strip()])
            if line_count < 2:
                log(logs / "excluded.log", f"{page_url(page.title)}\ttoo_short\t{block_title}")
                continue

            relation_parts = [p for p in (cand.relation, cand.discovery) if p]
            if len(blocks) > 1:
                relation_parts.append(f"split_from_source_page:{page.title}")

            key = (block_title, author)
            if key in title_seen and title_seen[key] != page.title:
                relation_parts.append(f"duplicate_or_variant_of:{title_seen[key]}")
            title_seen.setdefault(key, page.title)

            group = safe_name(source_collection)
            raw_dir = OUT / "collections" / group / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)

            filename_base = safe_name(block_title)
            filename = filename_base + ".txt"
            raw_path = raw_dir / filename
            suffix = 2
            while raw_path in used_paths or raw_path.exists():
                relation_parts.append("duplicate_filename_variant")
                filename = f"{filename_base}_{suffix}.txt"
                raw_path = raw_dir / filename
                suffix += 1
            raw_path.write_text(body, encoding="utf-8")
            used_paths.add(raw_path)

            entry = Entry(
                title=block_title,
                filename=filename,
                source_url=page_url(page.title),
                source_collection=source_collection,
                original_title="",
                original_author=author,
                translator=translator,
                translator_type=translator_type,
                original_language=lang,
                license_or_status=license_status(page),
                relation="; ".join(dict.fromkeys(relation_parts)),
                source_page_title=page.title,
                collection_dir=str((OUT / "collections" / group).relative_to(OUT)).replace("\\", "/"),
            )
            entries.append(entry)
        time.sleep(delay)

    return base_entries + entries


def write_indices(entries: list[Entry]) -> None:
    by_collection: dict[str, list[dict]] = {}
    for entry in entries:
        data = asdict(entry)
        by_collection.setdefault(entry.collection_dir, []).append(data)
    for collection_dir, items in by_collection.items():
        write_json(OUT / collection_dir / "index.json", items)
    write_json(OUT / "index.json", [asdict(e) for e in entries])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="append", default=[], help="Wikisource collection or poem URL/title")
    ap.add_argument("--discover-prefix", action="store_true", help="scan the whole Translation namespace")
    ap.add_argument("--no-default-seeds", action="store_true", help="do not add built-in translated-poem seeds")
    ap.add_argument("--no-search", action="store_true", help="skip Wikisource search discovery")
    ap.add_argument("--delay", type=float, default=0.08, help="delay between page fetches")
    ap.add_argument("--limit", type=int, help="process only the first N candidates")
    ap.add_argument("--reset", action="store_true", help="delete previous data/wikisource_translated_poems before writing")
    ap.add_argument("--append-existing", action="store_true", help="append to existing index/raw data and skip collected source pages")
    args = ap.parse_args()

    if args.reset and OUT.exists():
        shutil.rmtree(OUT)
    logs = OUT / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    seeds = list(args.seed)
    if not args.no_default_seeds:
        seeds.extend(DEFAULT_SEEDS)

    candidates = discover(
        seeds,
        include_translation_namespace=args.discover_prefix,
        include_search=not args.no_search,
        logs=logs,
    )
    write_json(OUT / "candidate_manifest.json", [asdict(c) for c in candidates])

    base_entries = load_existing_entries() if args.append_existing and not args.reset else []
    entries = collect(candidates, args.delay, args.limit, logs, base_entries=base_entries)
    write_indices(entries)
    print(f"candidates: {len(candidates)}")
    print(f"collected poems: {len(entries)}")
    print(f"output: {OUT}")


if __name__ == "__main__":
    main()
