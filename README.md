# koreanpoemsbycodex

한국어 위키문헌의 시 텍스트를 원문 보존용 raw 텍스트와 JSON 메타데이터로 수집하는 저장소입니다.

## 이번 수집 범위

이 저장소는 기존 `scrape_poems.py`, 루트 `index.json`, `감사.txt`를 분석한 뒤 번역시 전용 수집 구조를 추가했습니다.

* 기존 `감사.txt`의 형식을 기준으로 행 구분은 `\n`, 연 구분은 정확히 빈 줄 두 줄이 보이는 `\n\n\n`으로 저장합니다.
* 현대어 변환, 맞춤법 보정, 번역 보정, Anthropic API 호출은 사용하지 않습니다.
* 로컬 네트워크에서 한국어 위키문헌 API를 직접 조회해 seed 컬렉션, 검색 후보, 번역 이름공간 후보를 수집했습니다.
* 2026-06-18 실행 기준 712개 후보를 검토했고, 번역시 384편을 저장했습니다.
* 현대어 변환, 맞춤법 보정, 번역 보정, Anthropic API 호출은 사용하지 않습니다.

## 폴더 구조

```text
data/wikisource_translated_poems/
  index.json                         # 전체 통합 인덱스
  collections/
    <collection_slug>/
      index.json                     # 묶음별 메타데이터
      raw/
        <title>.txt                  # 작품별 raw 텍스트
  logs/
    excluded.log                     # 비시/본문 없음/제외 항목 로그
    failures.log                     # 네트워크/API/존재하지 않는 페이지 실패 로그
    review_required.log              # 저작권, 중복, 네트워크, 판정 보류 로그
```

`collection_slug`는 재수집과 확장을 위해 출처 묶음을 안전한 파일명으로 바꾼 값입니다. 예를 들어 `번역:악의 꽃`은 `translation_악의 꽃`로 저장했습니다.

## 명명 규칙

* TXT 파일명은 가능한 한 위키문헌의 작품 표시 제목을 유지합니다.
* 파일 시스템에서 문제가 되는 문자(`\\ / * ? : " < > |`)는 `_`로 치환합니다.
* 같은 제목의 중복 번역이나 이본은 삭제하지 않고, 재수집 스크립트가 `_2`, `_3` 접미사를 붙이며 `relation` 필드에 관계를 기록합니다.
* 사용자 번역과 역사적 역자 번역은 `translator_type`으로 구분합니다.

## 메타데이터 필드

각 `index.json` 항목은 가능한 범위에서 다음 필드를 포함합니다.

* `title`
* `filename`
* `source_url`
* `source_collection`
* `original_title`
* `original_author`
* `translator`
* `translator_type`: `named_historical_translator`, `wikisource_user_translation`, `unknown`
* `original_language`
* `license_or_status`
* `relation`

저작권 또는 라이선스 상태가 명확하지 않거나 출처 표기를 별도 검토해야 하면 `review_required`를 남깁니다.

## 수집 스크립트

`scrape_poems.py`는 표준 라이브러리만 사용합니다. 기존 스크립트의 현대어 변환 및 외부 LLM 호출 흐름은 제거했습니다.

예시:

```bash
python scrape_poems.py \
  --discover-prefix \
  --reset
```

기존 결과를 유지하고 누락 후보만 이어 붙일 때는 다음처럼 실행합니다.

```bash
python scrape_poems.py \
  --discover-prefix \
  --append-existing
```

스크립트는 다음을 수행합니다.

1. seed 컬렉션과 번역 이름공간 후보를 탐색합니다.
2. 시가 아닌 장르, 너무 짧은 본문, 본문 추출 실패 항목을 로그로 남깁니다.
3. 각 묶음의 `raw/*.txt`와 `index.json`을 작성합니다.
4. 전체 `data/wikisource_translated_poems/index.json`을 갱신합니다.
5. 긴 실행 중에도 25개 후보마다 중간 인덱스를 체크포인트로 저장합니다.

## 현재 수집 결과

* `박용철 번역시집`: 371편 — 역사적 역자 박용철 번역.
* `번역:악의 꽃`: 8편 — 위키문헌 사용자 번역.
* `번역 namespace standalone`: 5편 — 위키문헌 사용자 번역 standalone 작품.

번역 이름공간 전체 스캔 중 소설, 법령, 경전, 수학/음악 교재, 산문 등은 제외했습니다. 소설 내부의 노래/시형 블록도 별도 번역시 corpus에는 넣지 않았습니다.
