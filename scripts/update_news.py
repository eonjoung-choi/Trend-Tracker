#!/usr/bin/env python3
"""
Trend Tracker 뉴스 자동 수집 스크립트
- RSS 피드에서 핀테크/이커머스 뉴스 수집
- Anthropic Claude API로 상세 분석 생성 (API 키 설정 시)
- news_data.json 업데이트
"""

import json
import os
import re
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import requests

# ── 설정 ──
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NEWS_DATA_PATH = Path(__file__).parent.parent / "news_data.json"
MAX_ITEMS = 20  # 최대 보관 뉴스 수
DAYS_TO_KEEP = 60  # 60일 이상 된 뉴스 삭제

# ── 타겟 키워드 (핀테크/이커머스) ──
KEYWORDS = [
    "간편결제", "핀테크", "네이버페이", "카카오페이", "토스", "쿠팡페이",
    "이커머스", "온라인쇼핑", "배달앱", "멤버십", "구독", "무신사",
    "올리브영", "컬리", "쿠팡", "배달의민족", "SSG", "롯데온",
    "디지털금융", "오픈뱅킹", "인터넷은행", "모바일결제",
    "당근마켓", "번개장터", "지그재그", "에이블리",
    "페이스페이", "생체인증", "안면인식", "QR결제",
]

# ── 브랜드 매핑 ──
BRAND_MAP = {
    "네이버": ("네이버페이", "b-naver"),
    "네이버페이": ("네이버페이", "b-naver"),
    "카카오": ("카카오페이", "b-kakao"),
    "카카오페이": ("카카오페이", "b-kakao"),
    "토스": ("토스", "b-toss"),
    "쿠팡": ("쿠팡", "b-coupang"),
    "배달의민족": ("배달의민족", "b-baemin"),
    "배민": ("배달의민족", "b-baemin"),
    "무신사": ("무신사", "b-musinsa"),
    "올리브영": ("올리브영", "b-musinsa"),
    "컬리": ("컬리", "b-kurly"),
    "SSG": ("SSG닷컴", "b-ssg"),
    "신세계": ("SSG닷컴", "b-ssg"),
    "롯데": ("롯데온", "b-lotte"),
    "당근": ("당근마켓", "b-carrot"),
    "지그재그": ("지그재그", "b-zigzag"),
    "신한": ("신한카드", "b-shinhan"),
    "KB": ("KB국민", "b-kb"),
    "삼성": ("삼성카드", "b-samsung"),
    "현대": ("현대카드", "b-hyundai"),
    "우리": ("우리은행", "b-woori"),
}

# ── 업권 매핑 ──
SECTOR_KEYWORDS = {
    "핀테크": ["간편결제", "핀테크", "페이", "결제", "금융", "대출", "은행", "인증", "생체", "페이스"],
    "전통금융": ["신한", "KB", "국민", "삼성카드", "현대카드", "우리", "하나", "농협"],
    "이커머스": ["쿠팡", "SSG", "롯데온", "11번가", "G마켓", "이커머스", "온라인쇼핑"],
    "버티컬커머스": ["무신사", "올리브영", "컬리", "배달", "당근", "지그재그", "에이블리", "다이소"],
}

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
    """기사 내용에서 브랜드 감지"""
    text = title + " " + desc
    for keyword, (brand, bc) in BRAND_MAP.items():
        if keyword in text:
            return brand, bc
    return "기타", "b-toss"


def detect_sector(title: str, desc: str) -> str:
    """기사 내용에서 업권 감지"""
    text = title + " " + desc
    scores = {}
    for sector, keywords in SECTOR_KEYWORDS.items():
        scores[sector] = sum(1 for kw in keywords if kw in text)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "핀테크"


def is_relevant(title: str, desc: str) -> bool:
    """핀테크/이커머스 관련 기사인지 판별"""
    text = (title + " " + desc).lower()
    return any(kw in text for kw in KEYWORDS)


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
                # HTML 태그 제거
                desc = re.sub(r"<[^>]+>", "", desc).strip()
                link = entry.get("link", "")
                date = parse_date(entry)

                if is_relevant(title, desc):
                    articles.append({
                        "title": title,
                        "desc": desc[:120],
                        "url": link,
                        "source": feed_info["name"],
                        "date": date,
                    })
        except Exception as e:
            print(f"  ⚠️ {feed_info['name']} RSS 실패: {e}")

    # 중복 제거 (제목 유사도 기반)
    seen_titles = set()
    unique = []
    for a in articles:
        short = a["title"][:30]
        if short not in seen_titles:
            seen_titles.add(short)
            unique.append(a)
    return unique


def enrich_with_ai(article: dict) -> dict:
    """Claude API로 상세 분석 생성"""
    if not ANTHROPIC_API_KEY:
        return {
            "detail": article["desc"],
            "impact_text": "이 소식이 사용자 경험에 미치는 영향을 확인해보세요.",
            "why": "핀테크/이커머스 시장의 최신 동향으로, 서비스 기획 시 참고할 만한 사례입니다.",
            "tags": [],
            "type": "정책 변화",
            "tc": "t-policy",
            "imp": 3,
            "il": "보통",
        }

    prompt = f"""다음 뉴스를 PM/서비스 기획자 관점에서 분석해주세요.

제목: {article['title']}
요약: {article['desc']}

아래 JSON 형식으로만 응답해주세요 (다른 텍스트 없이):
{{
  "detail": "3-4문장, 해요체로 핵심 내용 설명",
  "impact_text": "2-3문장, 사용자에게 어떤 영향이 있는지",
  "why": "2-3문장, 실무에서 참고할 포인트",
  "tags": ["키워드1", "키워드2", "키워드3"],
  "type": "신규 기능|기능 업데이트|정책 변화|UX 개선|이벤트",
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
            # JSON 파싱
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                return json.loads(match.group())
    except Exception as e:
        print(f"  ⚠️ AI 분석 실패: {e}")

    # fallback
    return {
        "detail": article["desc"],
        "impact_text": "이 소식이 사용자 경험에 미치는 영향을 확인해보세요.",
        "why": "핀테크/이커머스 시장의 최신 동향으로, 서비스 기획 시 참고할 만한 사례입니다.",
        "tags": [],
        "type": "정책 변화",
        "tc": "t-policy",
        "imp": 3,
        "il": "보통",
    }


def build_news_item(article: dict, enrichment: dict) -> dict:
    """뉴스 아이템 JSON 구조 생성"""
    brand, bc = detect_brand(article["title"], article["desc"])
    sector = detect_sector(article["title"], article["desc"])

    tags = enrichment.get("tags", [])
    if not tags:
        # 키워드에서 자동 태그 추출
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
        "type": enrichment.get("type", "정책 변화"),
        "tc": enrichment.get("tc", "t-policy"),
        "tags": tags[:3],
        "imp": enrichment.get("imp", 3),
        "il": enrichment.get("il", "보통"),
        "date": article["date"],
        "isEvent": False,
        "src": [{"t": article["source"], "url": article["url"], "ty": "기사"}],
    }


def load_existing() -> list:
    """기존 news_data.json 로드"""
    if NEWS_DATA_PATH.exists():
        with open(NEWS_DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_data(items: list):
    """news_data.json 저장"""
    with open(NEWS_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def main():
    print("🔄 Trend Tracker 뉴스 업데이트 시작...")
    print(f"  📅 실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  🔑 AI 분석: {'활성' if ANTHROPIC_API_KEY else '비활성 (기본 모드)'}")

    # 1. 기존 데이터 로드
    existing = load_existing()
    existing_titles = {item["title"][:30] for item in existing}
    print(f"  📦 기존 뉴스: {len(existing)}건")

    # 2. RSS에서 새 뉴스 수집
    raw_articles = fetch_rss_news()
    print(f"  📡 RSS 수집: {len(raw_articles)}건 관련 기사")

    # 3. 중복 제거
    new_articles = [a for a in raw_articles if a["title"][:30] not in existing_titles]
    print(f"  🆕 신규 기사: {len(new_articles)}건")

    # 4. 상세 분석 생성
    new_items = []
    for i, article in enumerate(new_articles[:10]):  # 최대 10건
        print(f"  ✍️ [{i+1}/{min(len(new_articles),10)}] {article['title'][:40]}...")
        enrichment = enrich_with_ai(article)
        item = build_news_item(article, enrichment)
        new_items.append(item)

    # 5. 병합 (신규 + 기존)
    all_items = new_items + existing

    # 6. 오래된 뉴스 삭제
    cutoff = (datetime.now() - timedelta(days=DAYS_TO_KEEP)).strftime("%Y-%m-%d")
    all_items = [item for item in all_items if item["date"] >= cutoff]

    # 7. 최대 개수 제한
    all_items.sort(key=lambda x: x["date"], reverse=True)
    all_items = all_items[:MAX_ITEMS]

    # 8. 저장
    save_data(all_items)
    print(f"  ✅ 완료! 총 {len(all_items)}건 저장")
    print(f"  📄 경로: {NEWS_DATA_PATH}")


if __name__ == "__main__":
    main()
