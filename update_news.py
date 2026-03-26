#!/usr/bin/env python3
"""
Trend Tracker 뉴스 자동 수집 스크립트
- RSS 피드에서 핀테크/이커머스 뉴스 수집
- 네이버 뉴스 검색 API로 마케팅/이커머스 뉴스 추가 수집
- Claude API로 관련성 검증 + 상세 분석 생성
- news_data.json 업데이트
"""

import json
import os
import re
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests

# ── 설정 ──
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
NEWS_DATA_PATH = Path(__file__).parent.parent / "news_data.json"
MAX_ITEMS = 20  # 최대 보관 뉴스 수
DAYS_TO_KEEP = 60  # 60일 이상 된 뉴스 삭제

# ── 타겟 키워드 (핀테크/이커머스) ──
KEYWORDS = [
    # 핀테크/결제
    "간편결제", "핀테크", "네이버페이", "카카오페이", "토스", "쿠팡페이",
    "디지털금융", "오픈뱅킹", "인터넷은행", "모바일결제",
    "페이스페이", "생체인증", "안면인식", "QR결제",
    # 이커머스/플랫폼
    "이커머스", "온라인쇼핑", "배달앱", "멤버십", "구독",
    "당근마켓", "번개장터",
]

# ── 네이버 뉴스 검색 쿼리 ──
# 각 쿼리에 type 지정: "service" = 서비스 업데이트, "marketing" = 마케팅 이벤트
NAVER_SEARCH_QUERIES = [
    # 서비스 업데이트
    {"q": "네이버페이 신규 서비스", "type": "service"},
    {"q": "카카오페이 업데이트", "type": "service"},
    {"q": "토스 신기능", "type": "service"},
    {"q": "쿠팡 서비스 변경", "type": "service"},
    {"q": "배달의민족 업데이트", "type": "service"},
    {"q": "간편결제 출시", "type": "service"},
    # 마케팅 이벤트 (구체적 브랜드 + 마케팅 행위)
    {"q": "무신사 콜라보 캠페인", "type": "marketing"},
    {"q": "올리브영 팝업스토어", "type": "marketing"},
    {"q": "컬리 브랜드 마케팅", "type": "marketing"},
    {"q": "쿠팡 마케팅 전략", "type": "marketing"},
    {"q": "이커머스 팝업스토어 콜라보", "type": "marketing"},
    {"q": "배달앱 프로모션 캠페인", "type": "marketing"},
]

# ── 브랜드 매핑 (정밀화: 모호한 단일 키워드 제거) ──
BRAND_MAP = {
    "네이버페이": ("네이버페이", "b-naver"),
    "네이버쇼핑": ("네이버페이", "b-naver"),
    "네이버플러스": ("네이버페이", "b-naver"),
    "카카오페이": ("카카오페이", "b-kakao"),
    "카카오톡 선물": ("카카오페이", "b-kakao"),
    "토스뱅크": ("토스", "b-toss"),
    "토스페이": ("토스", "b-toss"),
    "토스증권": ("토스", "b-toss"),
    "쿠팡이츠": ("쿠팡", "b-coupang"),
    "쿠팡페이": ("쿠팡", "b-coupang"),
    "로켓배송": ("쿠팡", "b-coupang"),
    "배달의민족": ("배달의민족", "b-baemin"),
    "배민": ("배달의민족", "b-baemin"),
    "무신사": ("무신사", "b-musinsa"),
    "올리브영": ("올리브영", "b-oliveyoung"),
    "컬리": ("컬리", "b-kurly"),
    "마켓컬리": ("컬리", "b-kurly"),
    "SSG닷컴": ("SSG닷컴", "b-ssg"),
    "쓱닷컴": ("SSG닷컴", "b-ssg"),
    "롯데온": ("롯데온", "b-lotte"),
    "당근마켓": ("당근마켓", "b-carrot"),
    "당근": ("당근마켓", "b-carrot"),
    "지그재그": ("지그재그", "b-zigzag"),
    "에이블리": ("에이블리", "b-ably"),
    "다이소몰": ("다이소", "b-daiso"),
    "신한카드": ("신한카드", "b-shinhan"),
    "KB국민카드": ("KB국민", "b-kb"),
    "삼성카드": ("삼성카드", "b-samsung"),
    "현대카드": ("현대카드", "b-hyundai"),
}

# ── 업권 매핑 ──
SECTOR_KEYWORDS = {
    "핀테크": ["간편결제", "핀테크", "페이", "결제", "금융", "대출", "은행", "인증", "생체"],
    "전통금융": ["신한카드", "KB국민", "삼성카드", "현대카드", "우리은행", "하나은행", "농협"],
    "이커머스": ["쿠팡", "SSG", "롯데온", "11번가", "G마켓", "이커머스", "온라인쇼핑"],
    "버티컬커머스": ["무신사", "올리브영", "컬리", "배달", "당근", "지그재그", "에이블리", "다이소"],
}

# ── 노이즈 제거: 제외 키워드 ──
EXCLUDE_KEYWORDS = [
    # 교육/채용/IR
    "교육", "수강", "강의", "채용", "인턴", "공모전", "세미나", "컨퍼런스",
    "실적 발표", "주가", "시가총액", "배당", "공시", "IR",
    # 비관련 산업
    "삼성전자", "반도체", "갤럭시", "자동차", "부동산", "아파트",
    "삼성바이오", "삼성SDI", "삼성SDS", "삼성물산", "삼성생명",
    "현대자동차", "현대건설", "현대중공업", "현대모비스",
    "LG전자", "LG화학", "LG에너지", "SK하이닉스",
    # 단순 할인/쿠폰 나열
    "최대 할인", "쿠폰 지급", "적립금 이벤트",
]

# ── RSS 피드 목록 ──
RSS_FEEDS = [
    {"url": "https://rss.etnews.com/Section901.xml", "name": "전자신문"},
    {"url": "https://rss.etnews.com/Section902.xml", "name": "전자신문 금융"},
    {"url": "https://www.zdnet.co.kr/rss/", "name": "ZDNet Korea"},
    {"url": "https://www.inews24.com/rss/news_it.xml", "name": "아이뉴스24"},
    {"url": "https://www.dt.co.kr/rss/today.xml", "name": "디지털타임스"},
    {"url": "https://www.bloter.net/rss", "name": "블로터"},
]


def generate_id(title: str) -> int:
    """제목 기반 고유 ID 생성"""
    h = hashlib.md5(title.encode()).hexdigest()[:8]
    return int(h, 16) % 90000 + 10000


def detect_brand(title: str, desc: str) -> tuple:
    """기사 내용에서 브랜드 감지 (정밀 매칭)"""
    text = title + " " + desc
    for keyword, (brand, bc) in BRAND_MAP.items():
        if keyword in text:
            return brand, bc
    return "기타", "b-etc"


def detect_sector(title: str, desc: str) -> str:
    """기사 내용에서 업권 감지"""
    text = title + " " + desc
    scores = {}
    for sector, keywords in SECTOR_KEYWORDS.items():
        scores[sector] = sum(1 for kw in keywords if kw in text)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "핀테크"


def is_relevant(title: str, desc: str) -> bool:
    """핀테크/이커머스 관련 기사인지 1차 판별"""
    text = title + " " + desc
    return any(kw in text for kw in KEYWORDS)


def is_noise(title: str, desc: str) -> bool:
    """노이즈 기사 필터링 (제외 대상이면 True)"""
    text = title + " " + desc
    return any(kw in text for kw in EXCLUDE_KEYWORDS)


def parse_date(entry) -> str:
    """RSS 엔트리에서 날짜 추출"""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        from time import mktime
        dt = datetime.fromtimestamp(mktime(entry.published_parsed))
        return dt.strftime("%Y-%m-%d")
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        from time import mktime
        dt = datetime.fromtimestamp(mktime(entry.updated_parsed))
        return dt.strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def fetch_rss_news() -> list:
    """RSS 피드에서 뉴스 수집"""
    articles = []
    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:20]:
                title = entry.get("title", "").strip()
                desc = entry.get("summary", entry.get("description", "")).strip()
                desc = re.sub(r"<[^>]+>", "", desc).strip()
                link = entry.get("link", "")
                date = parse_date(entry)

                if is_relevant(title, desc) and not is_noise(title, desc):
                    articles.append({
                        "title": title,
                        "desc": desc[:120],
                        "url": link,
                        "source": feed_info["name"],
                        "date": date,
                    })
        except Exception as e:
            print(f"  ⚠️ {feed_info['name']} RSS 실패: {e}")

    # 중복 제거
    seen_titles = set()
    unique = []
    for a in articles:
        short = a["title"][:30]
        if short not in seen_titles:
            seen_titles.add(short)
            unique.append(a)
    return unique


def fetch_naver_news() -> list:
    """네이버 뉴스 검색 API로 뉴스 수집 (노이즈 1차 필터링 포함)"""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print("  ⚠️ 네이버 API 키 미설정 → 네이버 검색 건너뜀")
        return []

    articles = []
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    for qinfo in NAVER_SEARCH_QUERIES:
        query = qinfo["q"]
        qtype = qinfo["type"]
        try:
            url = f"https://openapi.naver.com/v1/search/news.json?query={quote(query)}&display=5&sort=date"
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                print(f"  ⚠️ 네이버 검색 실패 [{query}]: {resp.status_code}")
                continue

            items = resp.json().get("items", [])
            for item in items:
                title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                desc = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()
                link = item.get("originallink") or item.get("link", "")
                pub_date = item.get("pubDate", "")
                try:
                    dt = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %z")
                    date = dt.strftime("%Y-%m-%d")
                except Exception:
                    date = datetime.now().strftime("%Y-%m-%d")

                # 1차 필터: 노이즈 제거
                if is_noise(title, desc):
                    continue

                articles.append({
                    "title": title,
                    "desc": desc[:120],
                    "url": link,
                    "source": "네이버뉴스",
                    "date": date,
                    "_qtype": qtype,  # 검색 쿼리 타입 전달
                })
        except Exception as e:
            print(f"  ⚠️ 네이버 검색 실패 [{query}]: {e}")

    # 중복 제거
    seen_titles = set()
    unique = []
    for a in articles:
        short = a["title"][:30]
        if short not in seen_titles:
            seen_titles.add(short)
            unique.append(a)
    return unique


def ai_validate_article(article: dict) -> bool:
    """Claude API로 기사 관련성 검증 (True=통과, False=제외)"""
    if not ANTHROPIC_API_KEY:
        return True  # API 없으면 전부 통과

    prompt = f"""다음 뉴스 기사가 아래 기준에 부합하는지 판단해주세요.

제목: {article['title']}
요약: {article['desc']}

[포함 대상] 아래 중 하나라도 해당하면 "yes":
- 핀테크/간편결제 서비스의 신규 기능, 업데이트, 정책 변화
- 이커머스/배달 플랫폼(쿠팡, 네이버쇼핑, SSG, 배민 등)의 서비스 변경
- 버티컬커머스(무신사, 올리브영, 컬리 등)의 전략적 마케팅 캠페인, 콜라보, 팝업스토어
- PM/서비스 기획자에게 인사이트를 줄 수 있는 플랫폼 전략 뉴스

[제외 대상] 아래 중 하나라도 해당하면 "no":
- 단순 할인/쿠폰 나열 (예: "최대 50% 할인", "적립금 이벤트")
- 교육/채용/세미나/컨퍼런스 소식
- 주가/실적/IR/투자 리포트
- 금융사 이름만 들어갔지 실제 금융서비스와 무관한 기사 (예: 삼성전자를 삼성카드로 오인)
- 특정 플랫폼의 개별 상품 리뷰나 추천

"yes" 또는 "no"만 답해주세요."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        if resp.status_code == 200:
            answer = resp.json()["content"][0]["text"].strip().lower()
            return "yes" in answer
    except Exception as e:
        print(f"    ⚠️ AI 검증 실패: {e}")

    return True  # 실패 시 통과 처리


def enrich_with_ai(article: dict, is_event: bool = False) -> dict:
    """Claude API로 상세 분석 생성"""
    if not ANTHROPIC_API_KEY:
        return {
            "detail": article["desc"],
            "impact_text": "이 소식이 사용자 경험에 미치는 영향을 확인해보세요.",
            "why": "핀테크/이커머스 시장의 최신 동향으로, 서비스 기획 시 참고할 만한 사례입니다.",
            "tags": [],
            "type": "마케팅 이벤트" if is_event else "정책 변화",
            "tc": "t-event" if is_event else "t-policy",
            "imp": 3,
            "il": "보통",
        }

    event_hint = ""
    if is_event:
        event_hint = "\n이 기사는 마케팅 이벤트/캠페인 관련 뉴스입니다. type은 '마케팅 이벤트', tc는 't-event'로 설정해주세요."

    prompt = f"""다음 뉴스를 PM/서비스 기획자 관점에서 분석해주세요.{event_hint}

제목: {article['title']}
요약: {article['desc']}

아래 JSON 형식으로만 응답해주세요 (다른 텍스트 없이):
{{
  "detail": "3-4문장, 해요체로 핵심 내용 설명",
  "impact_text": "2-3문장, 사용자에게 어떤 영향이 있는지",
  "why": "2-3문장, 실무에서 참고할 포인트",
  "tags": ["키워드1", "키워드2", "키워드3"],
  "type": "신규 기능|기능 업데이트|정책 변화|UX 개선|마케팅 이벤트",
  "tc": "t-new|t-update|t-policy|t-ux|t-event",
  "imp": 3~5,
  "il": "높음|보통"
}}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.status_code == 200:
            text = resp.json()["content"][0]["text"]
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                return json.loads(match.group())
    except Exception as e:
        print(f"  ⚠️ AI 분석 실패: {e}")

    return {
        "detail": article["desc"],
        "impact_text": "이 소식이 사용자 경험에 미치는 영향을 확인해보세요.",
        "why": "핀테크/이커머스 시장의 최신 동향으로, 서비스 기획 시 참고할 만한 사례입니다.",
        "tags": [],
        "type": "마케팅 이벤트" if is_event else "정책 변화",
        "tc": "t-event" if is_event else "t-policy",
        "imp": 3,
        "il": "보통",
    }


def build_news_item(article: dict, enrichment: dict, is_event: bool = False) -> dict:
    """뉴스 아이템 JSON 구조 생성"""
    brand, bc = detect_brand(article["title"], article["desc"])
    sector = detect_sector(article["title"], article["desc"])

    tags = enrichment.get("tags", [])
    if not tags:
        text = article["title"] + " " + article["desc"]
        tags = [kw for kw in KEYWORDS if kw in text][:3]

    return {
        "id": generate_id(article["title"]),
        "title": article["title"],
        "desc": article["desc"],
        "detail": enrichment.get("detail", article["desc"]),
        "impact_text": enrichment.get("impact_text", ""),
        "why": enrichment.get("why", ""),
        "brand": brand,
        "bc": bc,
        "sector": sector,
        "type": enrichment.get("type", "마케팅 이벤트" if is_event else "정책 변화"),
        "tc": enrichment.get("tc", "t-event" if is_event else "t-policy"),
        "tags": tags[:3],
        "imp": enrichment.get("imp", 3),
        "il": enrichment.get("il", "보통"),
        "date": article["date"],
        "isEvent": is_event or enrichment.get("type") == "마케팅 이벤트",
        "src": [{"t": article["source"], "url": article["url"], "ty": "기사"}],
    }


def load_existing() -> list:
    """기존 news_data.json 로드 (배열 또는 {items:[]} 구조 모두 지원)"""
    if NEWS_DATA_PATH.exists():
        with open(NEWS_DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "items" in data:
                return data["items"]
    return []


def save_data(items: list):
    """news_data.json 저장 ({items:[]} 구조)"""
    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "items": items,
    }
    with open(NEWS_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


def main():
    print("🔄 Trend Tracker 뉴스 업데이트 시작...")
    print(f"  📅 실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  🔑 AI 분석: {'활성' if ANTHROPIC_API_KEY else '비활성 (기본 모드)'}")
    print(f"  🔍 네이버 검색: {'활성' if NAVER_CLIENT_ID else '비활성'}")

    # 1. 기존 데이터 로드
    existing = load_existing()
    existing_titles = {item["title"][:30] for item in existing}
    print(f"  📦 기존 뉴스: {len(existing)}건")

    # 2. RSS에서 뉴스 수집
    rss_articles = fetch_rss_news()
    print(f"  📡 RSS 수집: {len(rss_articles)}건 (노이즈 제거 후)")

    # 3. 네이버 뉴스 검색으로 추가 수집
    naver_articles = fetch_naver_news()
    print(f"  🔍 네이버 검색: {len(naver_articles)}건 (노이즈 제거 후)")

    # 4. 합산 후 중복 제거
    all_raw = rss_articles + naver_articles
    seen = set()
    raw_articles = []
    for a in all_raw:
        short = a["title"][:30]
        if short not in seen:
            seen.add(short)
            raw_articles.append(a)

    # 5. 기존 뉴스와 중복 제거
    new_articles = [a for a in raw_articles if a["title"][:30] not in existing_titles]
    print(f"  🆕 신규 기사 후보: {len(new_articles)}건")

    # 6. AI 관련성 검증 (Claude Haiku로 빠르게 필터링)
    validated = []
    for i, article in enumerate(new_articles[:20]):  # 최대 20건 검증
        print(f"  🔎 [{i+1}/{min(len(new_articles),20)}] 검증: {article['title'][:40]}...", end="")
        if ai_validate_article(article):
            validated.append(article)
            print(" ✅")
        else:
            print(" ❌ 제외")
    print(f"  ✅ AI 검증 통과: {len(validated)}건")

    # 7. 상세 분석 생성 (검증 통과한 기사만)
    new_items = []
    for i, article in enumerate(validated[:10]):  # 최대 10건 분석
        is_event = article.get("_qtype") == "marketing"
        print(f"  ✍️ [{i+1}/{min(len(validated),10)}] {'🎯' if is_event else '📰'} {article['title'][:40]}...")
        enrichment = enrich_with_ai(article, is_event=is_event)
        item = build_news_item(article, enrichment, is_event=is_event)
        new_items.append(item)

    # 8. 병합 (신규 + 기존)
    all_items = new_items + existing

    # 9. 오래된 뉴스 삭제
    cutoff = (datetime.now() - timedelta(days=DAYS_TO_KEEP)).strftime("%Y-%m-%d")
    all_items = [item for item in all_items if item["date"] >= cutoff]

    # 10. 최대 개수 제한
    all_items.sort(key=lambda x: x["date"], reverse=True)
    all_items = all_items[:MAX_ITEMS]

    # 11. 저장
    save_data(all_items)
    event_count = sum(1 for item in all_items if item.get("isEvent"))
    print(f"  ✅ 완료! 총 {len(all_items)}건 저장 (서비스 업데이트 {len(all_items)-event_count}건, 마케팅 이벤트 {event_count}건)")
    print(f"  📄 경로: {NEWS_DATA_PATH}")


if __name__ == "__main__":
    main()
