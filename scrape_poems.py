"""
사용법: python scrape_poems.py <시인명> <위키소스_목차_URL>
예: python scrape_poems.py 김영랑 https://ko.wikisource.org/wiki/영랑시집
"""

import sys
import os
import json
import re
import time
import requests
import anthropic
from urllib.parse import urlparse, quote, unquote
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(".env.local")

_config = json.loads(Path("config.json").read_text(encoding="utf-8"))
COLLECTION = _config["active"]["collection"]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; educational-scraper/1.0)"}

# 수집에서 제외할 항목(발문 등)은 시인별 poems/raw/{author}/exclude.json로 관리한다.
# → load_excludes() 참고. 코드를 고치지 않고 시인별 파일만 두면 된다.

MODERNIZE_SYSTEM_PROMPT = """당신은 한국 근현대시 전문가입니다. 1900년대 초중반 구한글 표기의 시를 낭송에 적합한 형태로 변환합니다.

핵심 원칙: 표기 오류만 교정하고, 시어의 맛과 운율은 최대한 보존합니다.
변환이 망설여지면 원문을 유지하세요. 표기 교정이 목적이지 현대화가 목적이 아닙니다.

[반드시 변환해야 하는 것들의 예시]
- 과거 표기를 현대 표기로 변환: 잇슬→있을, 업서→없어, 꼿닙→꽃잎
- 이중피동 등 문법 오류: 씌워진→씌어진
- 본문 및 제목 한자: 五月→오월, 恐怖→공포, 序曲→서곡, 七夕→칠석
- 본문 및 제목 한자 병기 괄호: 불지암(佛地菴)→불지암
- 두음법칙 미적용 표기: 리별→이별, 련꽃→연꽃, 량심→양심
- 사이시옷 단독 자모 표기: 산ㅅ고개→산고개, 밤ㅅ비→밤비, 호롱ㅅ불→호롱불

[변환 금지]
- 시어로 굳어진 음역어: 와사등 (가스등으로 바꾸지 말 것)
- 방언으로 시적 효과를 내는 표현: 기둘리고 (기다리고로 바꾸지 말 것), 아즉 (아직으로 바꾸지 말 것)
- 시인 고유의 어투/문체: ~하오, ~이옵니다, ~하오리다 등 경어체
- 외래어 고유명사: 프랑시스 잠, 라이너 마리아 릴케 등
- 시어로 의도된 단독 자모: ㅎㅎㅎ, ㅋㅋ 등

[few-shot 예시]
원문: 와사등에 불을 혀놓고
변환: 와사등에 불을 혀놓고 (와사등 유지 — 시어로 굳어진 음역어)

원문: 기둘리고 기둘리어
변환: 기둘리고 기둘리어 (기다리고로 바꾸지 말 것 — 방언 질감)

원문: 詩가 이렇게 쉽게 씌워지는 것은
변환: 시가 이렇게 쉽게 씌어지는 것은 (한자→한글, 씌워진→씌어진 교정)

원문: 六疊房은 남의 나라
변환: 육첩방은 남의 나라 (한자 표기 교정)

원문: 나는 괴로워했다.
변환: 나는 괴로워했다. (이미 현대어 — 변환 불필요)

원문: 아아, 님은 갔습니다
변환: 아아, 님은 갔습니다  (임으로 바꾸지 말 것 — 한용운 고유 시어)

원문: 가지 마셔요
변환: 가지 마셔요  (마세요로 바꾸지 말 것 — 경어체 원형 보존)

원문: 날마다々々々 낡어감니다
변환: 날마다날마다 낡아갑니다 (々 반복 기호 풀어쓰기)

원문: 못 오시는 당신이 기루어요
변환: 못 오시는 당신이 기루어요  (그리워요로 바꾸지 말 것. 한용운 특유의 표현.)

누어서 → 누워서 (받침/모음 표기 교정)
우슴 → 웃음, 슯음 → 슬픔, 질거음 → 즐거움 (음절 교정)
그레서 → 그래서, 가마니 → 가만히 (부사 표기 교정)

[절대 금지]
- 제목을 본문 첫 행에 추가하는 것. 입력된 원문 행만 출력할 것.
- 원문에 없는 행을 추가하거나 삭제하는 것

형식:
- 행 구분: \\n, 연 구분: \\n\\n\\n (\\n\\n 사용 금지)
- 변환된 시 텍스트만 출력
- 행 추가/삭제/병합 금지, 표기만 변환"""

MODERNIZE_USER_TEMPLATE = (
    "다음 시를 현대 맞춤법으로 변환해주세요.\n\n제목: {title}\n\n원문:\n{text}"
)


# ── 유틸 ────────────────────────────────────────────────

def clean_title(title: str) -> str:
    return re.sub(r"\([^)]*[一-鿿][^)]*\)", "", title).strip()


def safe_filename(title: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", title) + ".txt"


def load_excludes(author: str) -> tuple[set[str], set[str]]:
    """poems/raw/{author}/exclude.json에서 제외 설정을 읽는다 (없으면 빈 집합).

    형식: {"exclude_paths": [...], "exclude_titles": [...]}
      - exclude_paths : 스크래핑 단계에서 건너뛸 위키 path (예: "/wiki/정지용_시집/발문")
      - exclude_titles: modern json 정리 단계에서 제거할 표시 제목 (예: "발 (박용철)")
    시인별로 이 파일만 두면 코드 수정 없이 제외 항목을 관리할 수 있다.
    """
    exclude_file = Path("poems/raw") / author / COLLECTION / "exclude.json"
    if not exclude_file.exists():
        return set(), set()
    cfg = json.loads(exclude_file.read_text(encoding="utf-8"))
    return set(cfg.get("exclude_paths", [])), set(cfg.get("exclude_titles", []))


# ── 1단계: 스크래핑 ──────────────────────────────────────

def fetch_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def get_poem_links(soup: BeautifulSoup) -> list[dict]:
    """mw-parser-output 안 ul/li의 본문 시 링크만 채택한다.

    위키소스 정지용 문서는 시 본문이 목차 제목이 아니라 공통 루트(/wiki/향수/)의
    서브페이지로 저장돼 있어, 목차 URL 접두로 거르면 대부분 누락된다. 그래서
    접두 필터를 버리고 본문 목록(ul/li)으로 스코프를 좁힌다.
      - 네임스페이스(:포함)·비위키 링크 제외
      - 동일 path 중복 제거(목차 내 같은 링크 반복)
      - 이본(異本) 정책(2-b): 같은 시의 '_(N장)' 판본이 여럿이면 장수(N)가 가장
        큰 판본만 수집하고 나머지는 버린다.
    동명이작(시 '밤' vs 산문 '밤_(산문)' 등)은 path가 달라 모두 보존된다.
    """
    content = soup.find("div", class_="mw-parser-output")
    links, seen = [], set()
    for li in content.select("ul li"):
        a = li.find("a", href=True)
        if a is None:
            continue
        href = a["href"]
        if not href.startswith("/wiki/") or ":" in href or href in seen:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        seen.add(href)
        links.append({"title": title, "path": href})

    # 이본 정책(2-b): 끝이 '_(N장)'인 판본은 base(장수 표기 제거)별로 묶어 N 최대만 남김
    jang_re = re.compile(r"^(.*)_\((\d+)장\)$")
    best = {}        # base path → (N, links 내 index)
    drop = set()
    for i, link in enumerate(links):
        m = jang_re.match(unquote(link["path"]))
        if not m:
            continue
        base, n = m.group(1), int(m.group(2))
        if base not in best:
            best[base] = (n, i)
        elif n > best[base][0]:
            drop.add(best[base][1])
            best[base] = (n, i)
        else:
            drop.add(i)
    return [link for i, link in enumerate(links) if i not in drop]


def extract_poem_text(soup: BeautifulSoup) -> str:
    pages_divs = soup.find_all("div", class_="prp-pages-output")
    if not pages_divs:
        return ""

    def _preceded_by_text(br) -> bool:
        prev = br.previous_sibling
        while isinstance(prev, str) and not prev.strip():  # 공백 텍스트 노드는 건너뜀
            prev = prev.previous_sibling
        return isinstance(prev, str) and bool(prev.strip())

    for pages_div in pages_divs:  # 여러 페이지에 걸친 시도 전부 합침
        # <p> 태그 경계를 연 구분(\x02)으로 처리하기 위해 앞뒤로 마커 삽입
        for p in pages_div.find_all("p"):
            p.insert_before("\x02")
            p.insert_after("\x02")

        # <br> 분류에 따라 마커 치환 (역순)
        for br in reversed(pages_div.find_all("br")):
            br.replace_with("\x01" if _preceded_by_text(br) else "\x02")

    raw_text = ""
    for pages_div in pages_divs:
        raw_text += pages_div.get_text("") + "\x02"

    # 1. 제로위스 스페이스 제거
    # 2. HTML 소스코드의 포맷팅용 줄바꿈을 공백으로 병합 (산문 형태 보존)
    raw_text = raw_text.replace("​", "").replace("\n", " ")
    
    # 3. 삽입해둔 마커를 실제 구분자로 복구 (\x02 = 연 구분, \x01 = 행 구분)
    raw_text = raw_text.replace("\x02", "\n\n\n").replace("\x01", "\n")

    clean_lines = []
    for line in raw_text.splitlines():
        stripped = line.strip(" \t\xa0")
        # 빈 줄은 연 구분을 위해 빈 문자열로 유지하되, 숫자만 있는 페이지번호는 제거
        if not stripped:
            clean_lines.append("")
        elif not re.fullmatch(r"\d+", stripped):
            clean_lines.append(stripped)

    # 행들을 이어붙이고, 연속된 빈 줄을 최종 연 구분(\n\n\n)으로 정규화
    text = "\n".join(clean_lines)
    text = re.sub(r"\n{2,}", "\n\n\n", text)
    return text.strip()


def extract_standalone_text(soup: BeautifulSoup) -> str:
    """판본 접두 없는 '단독' 시 페이지의 본문만 추출.

    extract_poem_text()가 다루는 prp-pages-output(스캔 교정본)이 아니라,
    div.poem / div.prose / 단락 <p> 구조를 쓰는 정본 페이지용이다.
    mw-parser-output 직계 자식 중 본문 블록만 모으고, 헤더 네비(← 제목 저자 →)·
    자매프로젝트 박스·라이선스 섹션은 버린다. 행 구분은 \\n, 블록(연/문단)
    구분은 \\n\\n\\n. 동음이의 문서 등 본문이 없으면 '' 반환."""
    if soup.find("table", id="disambigbox"):  # 동음이의 문서
        return ""
    cont = soup.find("div", class_="mw-parser-output")
    if cont is None:
        return ""

    def _preceded_by_text(br) -> bool:
        prev = br.previous_sibling
        while isinstance(prev, str) and not prev.strip():  # 공백 텍스트 노드는 건너뜀
            prev = prev.previous_sibling
        return isinstance(prev, str) and bool(prev.strip())

    def _markup_brs(el):
        # extract_poem_text와 동일 규칙: 텍스트 뒤 <br>는 행 구분(\x01),
        # 그 외(연속 <br>의 둘째 br 등)는 연 구분(\x02)으로 치환.
        for br in reversed(el.find_all("br")):
            br.replace_with("\x01" if _preceded_by_text(br) else "\x02")

    def _clean_block(raw_text: str, skip: int = 0) -> str:
        # 마커 복구 후 행 정리. skip: 본문 시작 전 건너뛸 '비어있지 않은' 줄 수
        # (원문/현대어 헤더 제거용). 연속 빈 줄은 연 구분(\n\n\n)으로 정규화.
        lines = raw_text.splitlines()
        if skip:
            i, seen = 0, 0
            while i < len(lines) and seen < skip:
                if lines[i].strip():
                    seen += 1
                i += 1
            lines = lines[i:]
        block = "\n".join(ln.strip(" \t\xa0") for ln in lines)
        return re.sub(r"\n{2,}", "\n\n\n", block).strip()

    blocks = []
    for child in cont.find_all(recursive=False):
        cls = set(child.get("class") or [])
        if child.name == "style":
            continue
        if child.name == "div" and ({"ws-header", "ws-noexport"} & cls):
            continue  # 헤더 네비·비표시 블록
        if child.name == "div" and ({"mw-heading", "licenseContainer"} & cls):
            break     # 라이선스 섹션 시작 → 본문 종료
        if child.name == "p" or (child.name == "div" and ({"poem", "prose"} & cls)):
            # 연속 <br><br>는 연 구분(\x02), 단독 <br>는 행 구분(\x01)으로 치환
            _markup_brs(child)

            # 텍스트 추출 후 HTML 소스의 줄바꿈을 공백으로 병합하고 마커를 복구
            raw_text = child.get_text().replace("\n", " ").replace("\x02", "\n\n\n").replace("\x01", "\n")
            block = _clean_block(raw_text)
            if block:
                blocks.append(block)

        elif child.name == "div" and not cls:
            # 연속 <br><br>는 연 구분(\x02), 단독 <br>는 행 구분(\x01)으로 치환
            _markup_brs(child)

            raw_text = child.get_text().replace("\n", " ").replace("\x02", "\n\n\n").replace("\x01", "\n")
            lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
            if not lines:
                continue
            
            first_line = lines[0].replace(" ", "")
            
            wonmun_idx = -1
            for idx, ln in enumerate(lines):
                if ln.replace(" ", "").startswith("원문"):
                    wonmun_idx = idx
                    break
            
            if first_line.startswith("현대어") and wonmun_idx == -1:
                continue
            elif first_line.startswith("원문"):
                block = _clean_block(raw_text, skip=2)
                if block:
                    blocks.append(block)
            elif wonmun_idx != -1:
                block = _clean_block(raw_text, skip=wonmun_idx + 2)
                if block:
                    blocks.append(block)
    text = "\n\n\n".join(blocks).strip()
    text = re.sub(r" +", " ", text)
    return text


def scrape(toc_urls: list[str], raw_dir: Path,
           exclude_paths: set[str] | None = None) -> list[dict]:
    exclude_paths = exclude_paths or set()
    all_links = []
    seen_paths = set()

    for toc_url in toc_urls:
        parsed = urlparse(toc_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        print(f"목차 로딩: {toc_url}")
        links = get_poem_links(fetch_soup(toc_url))
        print(f"총 {len(links)}편 발견\n")

        for link in links:
            if unquote(link["path"]) in exclude_paths:
                print(f"  제외: {link['title']} ({link['path']})")
                continue
            if link["path"] not in seen_paths:
                link["base_url"] = base_url
                all_links.append(link)
                seen_paths.add(link["path"])

    # ── incremental: 기존 index.json 로드 (url→기존 entry) ──
    # 이미 받은 시는 url 매칭 + raw 파일 존재로 판정해 재다운로드를 건너뛴다.
    # 기존 파일명으로 seen_filenames를 seed해, 신규 시(동명 시·산문 등)가 기존
    # 파일을 덮어쓰지 않고 _2 등 충돌 회피 이름을 받도록 한다.
    raw_dir.mkdir(parents=True, exist_ok=True)
    index_path = raw_dir / "index.json"
    old_index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    old_by_url = {e["url"]: e for e in old_index}
    seen_filenames = {e["filename"] for e in old_index}

    print(f"총 {len(all_links)}편 (기존 인덱스 {len(old_index)}편 로드)\n")
    index, n_new, n_skip = [], 0, 0

    for i, item in enumerate(all_links, 1):
        title = item["title"]
        url = item["base_url"] + item["path"]

        # (a) 기존 index의 url 매칭 + raw 파일 존재 → 다운로드 스킵
        old = old_by_url.get(url)
        if old and (raw_dir / old["filename"]).exists():
            index.append(old)
            n_skip += 1
            print(f"[{i:02d}/{len(all_links)}] {title} … 이미 있음(건너뜀)")
            continue

        # (b) 신규: 기존/이번 실행 파일명과 충돌 안 나는 이름 배정
        base_filename = safe_filename(title)
        filename = base_filename
        counter = 2
        while filename in seen_filenames:
            filename = f"{base_filename[:-4]}_{counter}.txt"
            counter += 1
        seen_filenames.add(filename)

        # (c) 파일명은 신규지만 디스크에 같은 이름 파일이 이미 있으면(인덱스 누락분) 재다운로드 안 함
        if (raw_dir / filename).exists():
            index.append({"title": title, "url": url, "filename": filename})
            n_skip += 1
            print(f"[{i:02d}/{len(all_links)}] {title} … 파일 존재(건너뜀)")
            continue

        print(f"[{i:02d}/{len(all_links)}] {title} ...", end=" ", flush=True)
        try:
            soup = fetch_soup(url)
            # 위키문헌 분류 태그로 시 외 장르 자동 제외
            cats = [a.get_text(strip=True) for a in soup.select('#mw-normal-catlinks ul li a')]
            NON_POEM_CATS = {'수필', '소설', '번역', '희곡', '평론'}
            if any(c in NON_POEM_CATS for c in cats):
                print(f"비시 장르({', '.join(c for c in cats if c in NON_POEM_CATS)}) — 건너뜀")
                continue
            text = extract_poem_text(soup)
            if not text:
                text = extract_standalone_text(soup)
            if not text:
                print("본문 없음 (건너뜀)")
                continue
            (raw_dir / filename).write_text(text, encoding="utf-8")
            index.append({"title": title, "url": url, "filename": filename})
            n_new += 1
            print(f"저장 ({len(text)}자)")
        except Exception as e:
            print(f"오류: {e}")
        time.sleep(0.5)

    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n스크래핑 완료: 신규 {n_new} / 건너뜀 {n_skip} / 총 {len(index)}편 → {raw_dir}\n")
    return index


def rescrape_one(title: str, url: str, author: str):
    """단일 시를 위키소스에서 다시 스크래핑해 raw txt를 덮어쓴다 (테스트/수정용).
    예: rescrape_one("오-매 단풍 들것네",
                     "https://ko.wikisource.org/wiki/영랑시집/오-매_단풍_들것네")"""
    raw_dir = Path("poems/raw") / author / COLLECTION
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_path = raw_dir / f"{title}.txt"

    print(f"재스크래핑: {title}\n  {url}")
    soup = fetch_soup(url)
    text = extract_poem_text(soup)
    if not text:
        text = extract_standalone_text(soup)
    if not text:
        print("  ❌ 본문 없음 (덮어쓰기 취소)")
        return

    out_path.write_text(text, encoding="utf-8")
    print(f"  ✅ 저장: {out_path} ({len(text)}자)")


def debug_page(url: str):
    """URL을 fetch해서 전체 HTML을 저장하고 prp-pages-output div 내용을 출력 (디버그용).
    저장 파일명은 URL 끝의 시 제목에서 추출 → debug_{제목}.html"""
    last = unquote(urlparse(url).path.rstrip("/").split("/")[-1])  # 황홀한_달빛 → 황홀한 달빛
    title = re.sub(r'[\\/*?:"<>|]', "_", last.replace("_", " "))
    author = json.loads(Path("config.json").read_text(encoding="utf-8"))["active"]["author"]
    out_path = Path("poems/raw") / author / COLLECTION / f"debug_{title}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    out_path.write_text(resp.text, encoding="utf-8")
    print(f"✅ 전체 HTML 저장: {out_path} ({len(resp.text)}자)")

    soup = BeautifulSoup(resp.text, "html.parser")
    pages_div = soup.find("div", class_="prp-pages-output")
    print("\n─── prp-pages-output ───")
    if pages_div is None:
        print("❌ prp-pages-output div 없음")
    else:
        print(pages_div.get_text("\n"))


# ── 2단계: 현대어 변환 ────────────────────────────────────

def modernize(index: list[dict], author: str, raw_dir: Path, modern_dir: Path):
    modern_dir.mkdir(parents=True, exist_ok=True)
    output_file = modern_dir / f"{author}.json"

    # incremental: 기존 변환 결과를 이어받아, 이미 있는 제목(clean_title 기준)은 건너뛴다.
    results = json.loads(output_file.read_text(encoding="utf-8")) if output_file.exists() else []
    done_titles = {e["title"] for e in results}

    todo = [e for e in index if clean_title(e["title"]) not in done_titles]
    total = len(todo)
    print(f"현대어 변환 시작 (claude-sonnet-4-6, 신규 {total}편 / 기존 {len(results)}편 유지)\n")
    if not todo:
        print("변환할 신규 편이 없습니다.")
        return

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    for i, entry in enumerate(todo, 1):
        modern_title = clean_title(entry["title"])
        if modern_title in done_titles:  # 같은 실행 내 동명이작은 1편만(밤·비 등) — 뒤 항목 건너뜀
            print(f"[{i:02d}/{total}] {modern_title:<30} 동명 존재(건너뜀)")
            continue
        raw_text = (raw_dir / entry["filename"]).read_text(encoding="utf-8").strip()

        print(f"[{i:02d}/{total}] {modern_title:<30}", end=" ", flush=True)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=MODERNIZE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": MODERNIZE_USER_TEMPLATE.format(
                title=modern_title, text=raw_text,
            )}],
        )
        modern_text = response.content[0].text.strip()
        results.append({"title": modern_title, "author": author, "text": modern_text})
        done_titles.add(modern_title)
        print(f"완료 ({len(modern_text)}자)")

        if i % 10 == 0:
            output_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  → 중간 저장 ({i}편)")

        time.sleep(0.2)

    output_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n변환 완료: 신규 {total}편 추가 → 총 {len(results)}편 → {output_file}")


def modernize_one(title: str, author: str):
    """단일 시 하나만 현대어 변환 (테스트용). raw txt를 읽어 변환 후
    poems/modern/{author}/{author}.json의 같은 제목 항목을 갱신(없으면 추가)."""
    raw_dir = Path("poems/raw") / author / COLLECTION
    modern_dir = Path("poems/modern") / author / COLLECTION
    modern_dir.mkdir(parents=True, exist_ok=True)
    output_file = modern_dir / f"{author}.json"

    modern_title = clean_title(title)
    raw_path = raw_dir / f"{title}.txt"
    if not raw_path.exists():
        print(f"❌ raw 없음: {raw_path}")
        return
    raw_text = raw_path.read_text(encoding="utf-8").strip()

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    print(f"현대어 변환 (단일): {modern_title}")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=MODERNIZE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": MODERNIZE_USER_TEMPLATE.format(
            title=modern_title, text=raw_text,
        )}],
    )
    modern_text = response.content[0].text.strip()
    entry = {"title": modern_title, "author": author, "text": modern_text}

    # 기존 json 갱신 (같은 제목이 있으면 교체, 없으면 추가)
    results = json.loads(output_file.read_text(encoding="utf-8")) if output_file.exists() else []
    for i, e in enumerate(results):
        if e["title"] == modern_title:
            results[i] = entry
            break
    else:
        results.append(entry)
    output_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ 변환 완료 ({len(modern_text)}자) → {output_file}\n")
    print(modern_text)
    return entry


def fix_modern_stanza(title: str, author: str):
    """raw txt의 연 구분 구조를 기준으로 modern json의 text 구분자만 교정.
    Claude 호출 없음, 내용(단어/글자) 변경 없음 — \\n\\n을 \\n으로 바꾸고
    raw의 연 경계에만 \\n\\n\\n을 삽입한다."""
    raw_path = Path("poems/raw") / author / COLLECTION / f"{title}.txt"
    modern_file = Path("poems/modern") / author / COLLECTION / f"{author}.json"

    if not raw_path.exists():
        print(f"❌ raw 없음: {raw_path}")
        return "no_raw"
    if not modern_file.exists():
        print(f"❌ modern json 없음: {modern_file}")
        return "no_modern"

    # 1) raw에서 연별 본문 행 수 파악 (\n\n\n = 연 구분, \n = 행 구분)
    raw_text = raw_path.read_text(encoding="utf-8").strip()
    stanza_counts = [
        len([ln for ln in stanza.split("\n") if ln.strip()])
        for stanza in raw_text.split("\n\n\n")
    ]
    stanza_counts = [c for c in stanza_counts if c]  # 빈 연 제거

    # 2) modern json에서 해당 시 항목 찾기 (modern 제목은 clean_title 적용본)
    modern_title = clean_title(title)
    data = json.loads(modern_file.read_text(encoding="utf-8"))
    target = next((e for e in data if e["title"] == modern_title), None)
    if target is None:
        print(f"❌ '{modern_title}' 항목이 {modern_file}에 없음")
        return "not_found"

    old_text = target["text"]
    # 구분자 무시하고 본문 행만 순서대로 추출 (내용은 그대로 보존)
    modern_lines = [ln for ln in re.split(r"\n+", old_text.strip()) if ln.strip()]

    # 3) 행 수 검증 — 내용 보존을 위해 raw 본문 행 수와 반드시 일치해야 함
    if len(modern_lines) != sum(stanza_counts):
        print(f"❌ 행 수 불일치: raw 본문 {sum(stanza_counts)}행 vs modern {len(modern_lines)}행")
        print("   (raw에 제목 줄이 따로 있거나 raw가 아직 신규 포맷이 아닐 수 있음) — 중단")
        return "mismatch"

    # 4) raw 연 구조대로 재구성 (행 사이 \n, 연 사이 \n\n\n)
    new_stanzas, cursor = [], 0
    for count in stanza_counts:
        new_stanzas.append("\n".join(modern_lines[cursor:cursor + count]))
        cursor += count
    new_text = "\n\n\n".join(new_stanzas)

    target["text"] = new_text
    modern_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ 연 구분 교정 → {modern_file} ({len(stanza_counts)}연 {sum(stanza_counts)}행)\n")
    print("─── BEFORE ───")
    print(old_text)
    print("\n─── AFTER ───")
    print(new_text)
    return "ok"


def fix_all_stanzas(author: str):
    """index.json의 모든 시를 순회하며 fix_modern_stanza 일괄 적용.
    기존 raw txt를 그대로 사용한다(재스크래핑 없음 — raw가 최신이라는 전제).
    행 수 불일치 등으로 교정 못한 시는 마지막에 따로 출력해 수동 확인하게 한다."""
    index_path = Path("poems/raw") / author / COLLECTION / "index.json"
    if not index_path.exists():
        print(f"❌ index 없음: {index_path}")
        return

    index = json.loads(index_path.read_text(encoding="utf-8"))
    total = len(index)
    skipped = []  # (title, reason)

    print(f"일괄 연 구분 교정 시작: {total}편 ({author})\n")
    for i, entry in enumerate(index, 1):
        title = entry["title"]
        print(f"\n{'='*60}\n[{i:02d}/{total}] {title}\n{'='*60}")
        status = fix_modern_stanza(title, author)       # modern 연 구분 교정 (raw 그대로 사용)
        if status != "ok":
            skipped.append((title, status))

    done = total - len(skipped)
    print(f"\n🎉 완료: {done}/{total} 교정, {len(skipped)} 스킵")
    if skipped:
        print("\n⚠️ 수동 확인 필요:")
        for title, reason in skipped:
            print(f"  - {title}  ({reason})")


def modernize_titles(author: str):
    """poems/modern/{author}/{author}.json을 읽어 한자가 포함된 title을
    Claude API로 현대어 한글로 변환 후 저장. 제목 목록을 한 번에 넘겨 JSON 배열로 받음.
    - 한자 제목 → 한글 (序詩 → 서시)
    - 한자+한글 혼용 → 한자만 한글로 (太初의 아침 → 태초의 아침)
    - 이미 한글인 제목은 그대로 둠
    - clean_title()로 한자 병기 괄호 제거 후 판별"""
    poems_file = Path("poems/modern") / author / COLLECTION / f"{author}.json"
    poems = json.loads(poems_file.read_text(encoding="utf-8"))

    has_hanja = re.compile(r"[一-鿿]")
    # clean_title 적용 후 한자가 남아 있는 제목만 변환 대상
    cleaned = [clean_title(p["title"]) for p in poems]
    targets = [t for t in cleaned if has_hanja.search(t)]

    if not targets:
        print("변환할 한자 제목이 없습니다.")
        return

    print("── 변환 전 ──")
    for t in targets:
        print(f"  {t}")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system_prompt = (
        "당신은 한국 근현대 시 제목을 현대어 한글로 변환하는 전문가입니다.\n"
        "규칙:\n"
        "1. 한자로 된 제목은 한글로 변환합니다 (예: 序詩 → 서시, 自畵像 → 자화상, 八福 → 팔복).\n"
        "2. 한자와 한글이 섞인 제목은 한자 부분만 한글로 변환하고 나머지는 그대로 둡니다 "
        "(예: 看板없는 거리 → 간판없는 거리, 太初의 아침 → 태초의 아침).\n"
        "3. 띄어쓰기와 한글 부분은 원문 그대로 유지합니다.\n"
        "입력은 제목 문자열의 JSON 배열입니다. 같은 순서, 같은 길이의 변환된 제목 "
        "JSON 배열만 출력하세요. 설명이나 코드블록 없이 JSON 배열만 반환하세요."
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": json.dumps(targets, ensure_ascii=False)}],
    )
    converted = json.loads(response.content[0].text.strip())
    if len(converted) != len(targets):
        raise ValueError(f"변환 결과 개수 불일치: 요청 {len(targets)}, 응답 {len(converted)}")

    title_map = dict(zip(targets, converted))

    print("\n── 변환 후 ──")
    for src, dst in title_map.items():
        print(f"  {src}  →  {dst}")

    # clean_title 적용본 기준으로 매핑하여 저장
    for p, clean in zip(poems, cleaned):
        p["title"] = title_map.get(clean, clean)

    poems_file.write_text(json.dumps(poems, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장 완료: {len(title_map)}개 제목 변환 → {poems_file}")


# ── 메인 ────────────────────────────────────────────────

def purge_excluded(modern_file: Path, exclude_titles: set[str]):
    """modern json에서 제외 대상 제목(발문 등)을 확실히 제거한다 (멱등)."""
    if not modern_file.exists():
        return
    data = json.loads(modern_file.read_text(encoding="utf-8"))
    kept = [e for e in data if e["title"] not in exclude_titles]
    if len(kept) != len(data):
        modern_file.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"제외 정리: modern {len(data) - len(kept)}편 제거 → {modern_file}")


def main():
    if len(sys.argv) < 3:
        print("사용법: python scrape_poems.py <시인명> <위키소스_목차_URL_1> [위키소스_목차_URL_2 ...]")
        print("예: python scrape_poems.py 김영랑 https://ko.wikisource.org/wiki/영랑시집")
        sys.exit(1)

    author = sys.argv[1]
    toc_urls = sys.argv[2:]
    raw_dir = Path("poems/raw") / author / COLLECTION
    modern_dir = Path("poems/modern") / author / COLLECTION

    exclude_paths, exclude_titles = load_excludes(author)
    index = scrape(toc_urls, raw_dir, exclude_paths)
    purge_excluded(modern_dir / f"{author}.json", exclude_titles)
    if index:
        modernize(index, author, raw_dir, modern_dir)
    else:
        print("스크래핑된 시가 없어 변환을 건너뜁니다.")


if __name__ == "__main__":
    main()
