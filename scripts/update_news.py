#!/usr/bin/env python3
"""
Trend Tracker 뉴스 자동 수집 스크립트 v4
- 수동 큐레이션 기사 보호 (curated=true 플래그)
- 네이버 뉴스 검색 API로 핀테크/이커머스 뉴스 수집
- 6단계 필터링: 날짜→노이즈→브랜드(제목Only)→AI검증(브랜드명포함)→분석→최종검증
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
# ★ v4: 최근 14일 이내 기사만 수집 (과거 기사 유입 차단 강화)
MIN_DATE = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

# ── 타겟 키워드 (핀테크/이커머스 서비스 관련만) ──
KEYWORDS = [
    # 핀테크/결제 서비스
    "네이버페이", "카카오페이", "토스뱅크", "토스페이", "쿠팡페이",
    "간편결제", "모바일결제", "QR결제", "페이스페이", "안면인식결제",
    # 이커머스/플랫폼 서비스
    "쿠팡이츠", "쿠팡 로켓", "로켓배송", "배달의민족",
    "무신사", "올리브영", "컬리", "마켓컬리",
    "SSG닷컴", "쓱닷컴", "롯데온", "G마켓", "지마켓", "11번가",
    "당근마켓",
    # 카드사
    "신한카드", "삼성카드", "현대카드", "KB카드", "KB국민카드",
]

# ── 네이버 뉴스 검색 쿼리 ──
# ★ v4: 브랜드명 필수 + "출시/업데이트/개편" 등 서비스 변화 키워드 조합
# 광범위 쿼리 제거, 각 쿼리에 expect_brand 지정 (결과 검증용)
NAVER_SEARCH_QUERIES = [
    # 서비스 업데이트 — 브랜드+변화 키워드
    {"q": "네이버페이 출시", "type": "service", "expect_brand": "네이버페이"},
    {"q": "네이버페이 업데이트", "type": "service", "expect_brand": "네이버페이"},
    {"q": "카카오페이 출시", "type": "service", "expect_brand": "카카오페이"},
    {"q": "카카오페이 신기능", "type": "service", "expect_brand": "카카오페이"},
    {"q": "토스 출시 서비스", "type": "service", "expect_brand": "토스"},
    {"q": "토스뱅크 업데이트", "type": "service", "expect_brand": "토스"},
    {"q": "쿠팡이츠 서비스", "type": "service", "expect_brand": "쿠팡"},
    {"q": "쿠팡 로켓배송 개편", "type": "service", "expect_brand": "쿠팡"},
    {"q": "배달의민족 서비스 개편", "type": "service", "expect_brand": "배달의민족"},
    {"q": "배달의민족 신기능", "type": "service", "expect_brand": "배달의민족"},
    {"q": "신한카드 서비스", "type": "service", "expect_brand": "신한카드"},
    {"q": "삼성카드 결제 서비스", "type": "service", "expect_brand": "삼성카드"},
    {"q": "현대카드 결제 서비스", "type": "service", "expect_brand": "현대카드"},
    # 마케팅 이벤트 — 브랜드+캠페인 키워드
    {"q": "무신사 팝업 콜라보", "type": "marketing", "expect_brand": "무신사"},
    {"q": "올리브영 신규 서비스", "type": "marketing", "expect_brand": "올리브영"},
    {"q": "컬리 서비스 전략", "type": "marketing", "expect_brand": "컬리"},
    {"q": "당근마켓 광고 플랫폼", "type": "marketing", "expect_brand": "당근마켓"},
]

# ── 브랜드 매핑 (정밀 키워드만, 모호한 단어 배제) ──
BRAND_MAP = {
    "네이버페이": ("네이버페이", "b-naver"),
    "네이버쇼핑": ("네이버페이", "b-naver"),
    "네이버플러스": ("네이버페이", "b-naver"),
    "네이버플러스 스토어": ("네이버페이", "b-naver"),
    "카카오페이": ("카카오페이", "b-kakao"),
    "카카오톡 선물": ("카카오페이", "b-kakao"),
    "카카오툴즈": ("카카오페이", "b-kakao"),
    "토스뱅크": ("토스", "b-toss"),
    "토스페이": ("토스", "b-toss"),
    "토스증권": ("토스", "b-toss"),
    "쿠팡이츠": ("쿠팡", "b-coupang"),
    "쿠팡페이": ("쿠팡", "b-coupang"),
    "쿠팡 로켓": ("쿠팡", "b-coupang"),
    "로켓배송": ("쿠팡", "b-coupang"),
    "쿠팡 와우": ("쿠팡", "b-coupang"),
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
    "지그재그": ("지그재그", "b-zigzag"),
    "에이블리": ("에이블리", "b-ably"),
    "다이소몰": ("다이소", "b-daiso"),
    "G마켓": ("G마켓", "b-gmarket"),
    "지마켓": ("G마켓", "b-gmarket"),
    "옥션": ("G마켓", "b-gmarket"),
    "11번가": ("11번가", "b-11st"),
    "신한카드": ("신한카드", "b-shinhan"),
    "KB국민카드": ("KB국민", "b-kb"),
    "KB카드": ("KB국민", "b-kb"),
    "삼성카드": ("삼성카드", "b-samsung"),
    "현대카드": ("현대카드", "b-hyundai"),
    "우리카드": ("우리카드", "b-woori"),
    "하나카드": ("하나카드", "b-hana"),
}

# ── 브랜드 오인 방지 (false-positive 감지) ──
BRAND_FALSE_POSITIVES = {
    "삼성카드": {
        "false_indicators": [
            "삼성전자", "갤럭시", "반도체", "삼성디스플레이", "삼성 TV",
            "삼성 쇼핑", "삼성SDI", "삼성SDS", "삼성물산", "삼성생명",
            "삼성중공업", "삼성바이오",
        ],
        "true_indicator": "삼성카드",
    },
    "현대카드": {
        "false_indicators": [
            "현대자동차", "현대건설", "현대중공업", "현대모비스",
            "현대백화점", "현대홈쇼핑", "현대그린푸드",
        ],
        "true_indicator": "현대카드",
    },
}

# ── SSG 오매칭 방지 ──
SSG_FALSE_KEYWORDS = [
    "SSG랜더스", "SSG 랜더스", "신세계그룹", "신세계백화점", "신세계면세점",
    "이마트", "스타필드", "SSG 그룹", "신세계인터내셔날", "신세계프라퍼티",
]

# ── "당근" 오매칭 방지 ──
DANGGEUN_TRUE_KEYWORDS = [
    "당근마켓", "당근 앱", "당근 광고", "당근 플랫폼", "당근 서비스",
    "당근 비즈", "당근페이", "당근 중고", "당근 동네",
]

# ── 업권 매핑 ──
SECTOR_KEYWORDS = {
    "핀테크": ["간편결제", "핀테크", "페이", "결제", "금융", "대출", "인증", "생체", "토스뱅크", "토스페이", "토스증권", "카카오뱅크", "케이뱅크"],
    "전통금융": ["신한카드", "KB국민카드", "삼성카드", "현대카드", "우리은행", "하나은행", "농협"],
    "이커머스": ["쿠팡", "SSG닷컴", "롯데온", "11번가", "G마켓", "이커머스", "온라인쇼핑"],
    "버티컬커머스": ["무신사", "올리브영", "컬리", "배달", "당근마켓", "지그재그", "에이블리", "다이소"],
}

# ── 노이즈 제거: 제외 키워드 (대폭 확장) ──
EXCLUDE_KEYWORDS = [
    # === 정치/외교/시사 ===
    "트럼프", "바이든", "관세", "무역전쟁", "무역 전쟁", "통상 압력",
    "대통령", "국회", "여당", "야당", "정치", "외교", "안보",
    "탄핵", "선거", "국방", "군사", "북한", "미사일",
    "수출 규제", "수입 규제", "보복 관세", "반덤핑",

    # === 교육/채용/IR ===
    "교육 프로그램", "수강", "강의", "채용", "인턴", "공모전",
    "세미나", "컨퍼런스", "부트캠프", "워크숍", "아카데미",
    "실적 발표", "주가", "시가총액", "배당", "공시", "IR",
    "영업이익", "순이익", "분기 실적",

    # === 비관련 산업/기업 (삼성 계열) ===
    "삼성전자", "삼성 쇼핑", "삼성 TV", "삼성디스플레이", "삼성 갤럭시",
    "반도체", "갤럭시", "삼성바이오", "삼성SDI", "삼성SDS",
    "삼성물산", "삼성생명", "삼성중공업",

    # === 비관련 산업/기업 (현대 계열) ===
    "현대자동차", "현대건설", "현대중공업", "현대모비스", "현대백화점",

    # === 비관련 산업/기업 (LG/SK 계열) ===
    "LG전자", "LG화학", "LG에너지", "LG생활건강", "SK하이닉스",
    "LG유플러스", "SK텔레콤", "KT ",

    # === 비관련 산업/기업 (유통/제조) ===
    "코스맥스", "에이피알", "신세계인터내셔날", "코오롱FnC", "코오롱인더",
    "시몬스", "락앤락", "한샘", "이랜드", "CJ제일제당", "풀무원",
    "오뚜기", "농심", "빙그레",

    # === 비관련 해외 기업/서비스 ===
    "아마존", "Amazon", "테슬라", "애플 TV", "넷플릭스",
    "메타 중소기업", "Meta ", "구글 클라우드", "마이크로소프트",
    "알리익스프레스", "테무", "쉬인", "틱톡 커머스",

    # === 기업 일반/경영 기사 ===
    "[기업家]", "[기업가]", "기업 분석", "경영 전략", "CEO 인터뷰",
    "수수료 논쟁", "수수료 갈등", "수수료 부담",
    "직매입", "월 1억 셀러",

    # === 스포츠/엔터/연예 ===
    "SSG랜더스", "SSG 랜더스", "프로야구", "축구", "올림픽",
    "아이돌", "드라마", "영화 개봉", "콘서트 티켓",

    # === 단순 할인/쿠폰/프로모션 ===
    "최대 할인", "쿠폰 지급", "적립금 이벤트", "할인코드", "쿠폰 총정리",
    "할인 쿠폰 코드", "프로모션 코드", "캐시백 이벤트", "웰컴백 쿠폰",
    "1등찍기", "1등 찍기", "두근두근", "정답 공개", "퀴즈 정답",
    "% 할인", "원 쿠폰", "특가", "파격 세일", "핫딜",

    # === 단순 쇼핑 가이드 ===
    "인기 상품", "추천 상품", "쇼핑 리스트", "구매 가이드",
    "이 제품 써봤", "언박싱", "리뷰 모음",

    # === 뉴스 브리프/라운드업 (여러 기업 나열형) ===
    "[브리프]", "[N2 ", "N2 유통", "유통 브리프", "업계 동향",
    "[종합]", "[속보]", "外",
    "일제히", "줄줄이", "경쟁사",

    # === 수수료/규제/갈등 ===
    "수수료율", "수수료 인상", "수수료 인하", "PG 수수료",
    "공정거래", "독점", "불공정",

    # === 과거 이벤트 리마인드/회고 ===
    "돌아보", "회고", "1주년", "2주년", "3주년",
    "지난해", "작년", "올해 상반기 결산",

    # === 광고제/어워드 ===
    "광고제", "어워드 수상", "칸 라이언즈", "대한민국 광고대상",

    # === 증시/투자 ===
    "주식", "증시", "코스피", "코스닥", "ETF", "IPO",
    "상장", "기업공개", "투자 유치", "벤처캐피탈",

    # === 기타 비관련 ===
    "자동차", "부동산", "아파트", "재건축", "분양",
    "의료", "병원", "제약", "바이오",
    "AI 반도체", "AI 칩", "데이터센터",
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
    """★ v4: 제목(title) 전용 브랜드 매칭. 본문(desc) 매칭 완전 제거.

    핵심 원칙:
    - 기사 제목에 브랜드명이 명시적으로 등장해야만 매칭
    - 본문에만 브랜드가 언급된 기사는 오태깅 위험이 높으므로 무조건 제외
    - 제목에 2개 이상 브랜드가 있으면 '비교 기사'이므로 제외
    """
    # === 삼성/현대 false-positive 방지 ===
    for brand_key, rules in BRAND_FALSE_POSITIVES.items():
        if any(fi in title for fi in rules["false_indicators"]):
            if rules["true_indicator"] not in title:
                return None, None

    # === SSG 오매칭 방지 (SSG랜더스, 신세계그룹 등) ===
    if any(sf in title for sf in SSG_FALSE_KEYWORDS):
        if "SSG닷컴" not in title and "쓱닷컴" not in title:
            return None, None

    # === 제목에서 브랜드 매칭 (유일한 매칭 경로) ===
    found_brands = []
    for keyword, (brand, bc) in BRAND_MAP.items():
        if keyword in title:
            # "당근" 오매칭 방지
            if "당근" in keyword and not any(dk in title for dk in DANGGEUN_TRUE_KEYWORDS):
                continue
            if (brand, bc) not in found_brands:
                found_brands.append((brand, bc))

    # ★ 정확히 1개 브랜드만 매칭된 경우만 허용
    # 2개 이상이면 비교/나열 기사이므로 제외
    if len(found_brands) == 1:
        return found_brands[0]

    if len(found_brands) > 1:
        print(f"    [다중브랜드] 제목에 {len(found_brands)}개 브랜드 → 제외: {title[:50]}")

    return None, None


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


def is_recent_enough(date_str: str) -> bool:
    """기사 날짜가 최소 기준일 이후인지 확인"""
    try:
        return date_str >= MIN_DATE
    except Exception:
        return False


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

                # 날짜 필터
                if not is_recent_enough(date):
                    continue

                if is_relevant(title, desc) and not is_noise(title, desc):
                    articles.append({
                        "title": title,
                        "desc": desc[:120],
                        "url": link,
                        "source": feed_info["name"],
                        "date": date,
                    })
        except Exception as e:
            print(f"  [RSS] {feed_info['name']} 실패: {e}")

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
        print("  [Naver] API 키 미설정 -> 네이버 검색 건너뜀")
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
            url = f"https://openapi.naver.com/v1/search/news.json?query={quote(query)}&display=3&sort=date"
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                print(f"  [Naver] 검색 실패 [{query}]: {resp.status_code}")
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

                # 날짜 필터: 최근 14일 기사만
                if not is_recent_enough(date):
                    print(f"    [날짜제외] {date} {title[:30]}...")
                    continue

                # 1차 필터: 노이즈 제거
                if is_noise(title, desc):
                    print(f"    [노이즈] {title[:30]}...")
                    continue

                # ★ v4: 검색 쿼리의 expect_brand가 제목에 있는지 확인
                expect_brand = qinfo.get("expect_brand", "")
                if expect_brand:
                    brand_in_title = False
                    for kw, (bn, _) in BRAND_MAP.items():
                        if bn == expect_brand or kw == expect_brand:
                            if kw in title:
                                brand_in_title = True
                                break
                    if not brand_in_title:
                        print(f"    [브랜드불일치] 기대={expect_brand}, 제목={title[:40]}...")
                        continue

                articles.append({
                    "title": title,
                    "desc": desc[:120],
                    "url": link,
                    "source": "네이버뉴스",
                    "date": date,
                    "_qtype": qtype,
                })
        except Exception as e:
            print(f"  [Naver] 검색 실패 [{query}]: {e}")

    # 중복 제거 (bigram 유사도)
    unique = []
    for a in articles:
        title_clean = re.sub(r"[^가-힣a-zA-Z0-9]", "", a["title"])
        is_dup = False
        for existing_a in unique:
            existing_clean = re.sub(r"[^가-힣a-zA-Z0-9]", "", existing_a["title"])
            if title_clean[:20] == existing_clean[:20]:
                is_dup = True
                break
            words_a = set(title_clean[i:i+2] for i in range(len(title_clean)-1))
            words_b = set(existing_clean[i:i+2] for i in range(len(existing_clean)-1))
            if words_a and words_b:
                overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
                if overlap > 0.6:
                    is_dup = True
                    break
        if not is_dup:
            unique.append(a)
    return unique


def ai_validate_article(article: dict, detected_brand: str = None) -> bool:
    """★ v4: Claude API로 기사 관련성 검증 (브랜드 일치 여부 포함)
    - detected_brand: 제목에서 감지된 브랜드명을 함께 전달하여 교차 검증
    - 실패 시 기본값 False (보수적 필터링)
    """
    if not ANTHROPIC_API_KEY:
        return False

    is_mkt = article.get("_qtype") == "marketing"
    brand_context = ""
    if detected_brand:
        brand_context = f"\n감지된 브랜드: {detected_brand}"

    prompt = f"""당신은 "국내 핀테크/이커머스 트렌드 트래커" 편집자입니다. 아래 기사가 게시 기준에 부합하는지 엄격하게 판단하세요.

제목: {article['title']}
요약: {article['desc']}{brand_context}

[핵심 판단 기준 — 아래 3가지를 모두 충족해야 "yes"]:
1. 기사의 주인공(subject)이 반드시 위 '감지된 브랜드'의 서비스여야 함
   - 해당 브랜드가 단순히 언급/비교되는 것은 "no"
   - 다른 기업이 주어이고 감지된 브랜드는 배경으로만 등장하면 "no"
2. 기사 내용이 해당 브랜드의 서비스 변화(신기능/업데이트/개편/전략적 캠페인)에 관한 것이어야 함
   - 단순 기업 소식(인사/실적/채용/IR)은 "no"
   - 업계 전반 동향이나 여러 기업 나열형 기사는 "no"
3. 기사 날짜가 최근 2주 이내여야 하고, 과거 이벤트의 재탕 기사가 아니어야 함

[무조건 "no"인 경우]:
- 트럼프/관세/무역/정치/외교/시사 (핀테크 언급이 곁들여져도 no)
- 단순 할인/쿠폰/적립금/캐시백 프로모션 안내
- 교육/채용/세미나/컨퍼런스/부트캠프
- 주가/실적/IR/투자/IPO/상장
- 해외 기업(아마존/메타/애플/구글) 중심
- 제조업/반도체/자동차/부동산
- 뉴스 브리프/라운드업 (여러 기업 나열)
- SSG랜더스(야구)/스포츠/연예
- 기사 제목에 브랜드명이 있으나 실제로는 다른 주제인 기사
- 2개 이상 브랜드가 비교·나열되는 기사"""

    if is_mkt:
        prompt += """

[마케팅 이벤트 추가 기준]:
- 반드시 해당 브랜드의 전략적 캠페인이어야 함 (팝업/콜라보/문화마케팅)
- 단순 "~% 할인", "~원 쿠폰"은 절대 "no"
- 이미 종료된 과거 캠페인의 후기/회고 기사도 "no" """

    prompt += """

의심스러우면 반드시 "no"를 선택하세요. 확실한 경우에만 "yes"입니다.
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
        print(f"    [AI검증] 실패: {e}")

    return False  # ★ 실패 시 제외 (보수적)


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
        print(f"  [AI분석] 실패: {e}")

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
    print("=" * 60)
    print("Trend Tracker v4 - 뉴스 업데이트 (강화 필터링)")
    print("=" * 60)
    print(f"  실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  AI 분석: {'활성' if ANTHROPIC_API_KEY else '비활성'}")
    print(f"  네이버 검색: {'활성' if NAVER_CLIENT_ID else '비활성'}")
    print(f"  최소 날짜: {MIN_DATE}")

    # 1. 기존 데이터 로드 → curated 기사 보호
    existing = load_existing()
    curated = [item for item in existing if item.get("curated")]
    non_curated = [item for item in existing if not item.get("curated")]
    existing_titles = {item["title"][:30] for item in existing}
    print(f"\n[1] 기존 뉴스: {len(existing)}건 (큐레이션 {len(curated)}건 보호)")

    # 2. RSS에서 뉴스 수집
    rss_articles = fetch_rss_news()
    print(f"\n[2] RSS 수집: {len(rss_articles)}건")

    # 3. 네이버 뉴스 검색
    naver_articles = fetch_naver_news()
    print(f"\n[3] 네이버 검색: {len(naver_articles)}건")

    # 4. 합산 후 중복 제거 (제목 유사도 기반)
    all_raw = rss_articles + naver_articles
    raw_articles = []
    for a in all_raw:
        title_clean = re.sub(r"[^가-힣a-zA-Z0-9]", "", a["title"])
        is_dup = False
        for existing_a in raw_articles:
            existing_clean = re.sub(r"[^가-힣a-zA-Z0-9]", "", existing_a["title"])
            if title_clean[:20] == existing_clean[:20]:
                is_dup = True
                break
            words_a = set(title_clean[i:i+2] for i in range(len(title_clean)-1))
            words_b = set(existing_clean[i:i+2] for i in range(len(existing_clean)-1))
            if words_a and words_b:
                overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
                if overlap > 0.6:
                    is_dup = True
                    break
        if not is_dup:
            raw_articles.append(a)

    # 5. 기존 뉴스와 중복 제거
    new_articles = [a for a in raw_articles if a["title"][:30] not in existing_titles]
    print(f"\n[4] 신규 기사 후보: {len(new_articles)}건 (중복 제거 후)")

    # 6. ★ v4: 제목 전용 브랜드 매칭 (본문 매칭 완전 제거)
    branded = []
    for a in new_articles:
        brand, bc = detect_brand(a["title"], a["desc"])
        if brand is not None:
            branded.append(a)
        else:
            print(f"  [브랜드X] {a['title'][:50]}...")
    print(f"\n[5] 브랜드 매칭: {len(branded)}건 통과 ({len(new_articles)-len(branded)}건 제외)")

    # 7. ★ v4: AI 관련성 검증 (브랜드명 포함하여 교차 검증)
    validated = []
    for i, article in enumerate(branded[:15]):
        brand, _ = detect_brand(article["title"], article["desc"])
        print(f"  [AI검증 {i+1}/{min(len(branded),15)}] [{brand}] {article['title'][:35]}...", end="")
        if ai_validate_article(article, detected_brand=brand):
            validated.append(article)
            print(" -> 통과")
        else:
            print(" -> 제외")
    print(f"\n[6] AI 검증: {len(validated)}건 통과")

    # 8. 상세 분석 생성 (최대 5건만 - 품질 우선)
    new_items = []
    for i, article in enumerate(validated[:5]):
        is_event = article.get("_qtype") == "marketing"
        print(f"  [분석 {i+1}/{min(len(validated),5)}] {'마케팅' if is_event else '서비스'}: {article['title'][:40]}...")
        enrichment = enrich_with_ai(article, is_event=is_event)
        item = build_news_item(article, enrichment, is_event=is_event)

        # ★ 최종 안전장치: brand가 None이면 절대 추가하지 않음
        if item["brand"] is None:
            print(f"    -> 브랜드 없음, 최종 제외")
            continue

        new_items.append(item)

    print(f"\n[7] 신규 추가: {len(new_items)}건")

    # 9. 병합: 큐레이션 기사 우선 보호
    auto_items = new_items + non_curated

    # 오래된 뉴스 삭제 (큐레이션 기사는 제외)
    cutoff = (datetime.now() - timedelta(days=DAYS_TO_KEEP)).strftime("%Y-%m-%d")
    auto_items = [item for item in auto_items if item["date"] >= cutoff]

    # 날짜순 정렬
    auto_items.sort(key=lambda x: x["date"], reverse=True)

    # 큐레이션 기사 + 자동수집 기사 합산 (MAX_ITEMS 이내)
    remaining_slots = MAX_ITEMS - len(curated)
    all_items = curated + auto_items[:max(0, remaining_slots)]
    all_items.sort(key=lambda x: x["date"], reverse=True)

    # 10. 저장
    save_data(all_items)
    event_count = sum(1 for item in all_items if item.get("isEvent"))
    curated_count = sum(1 for item in all_items if item.get("curated"))
    print(f"\n{'=' * 60}")
    print(f"완료! 총 {len(all_items)}건 저장")
    print(f"  큐레이션: {curated_count}건 (보호)")
    print(f"  서비스 업데이트: {len(all_items)-event_count}건")
    print(f"  마케팅 이벤트: {event_count}건")
    print(f"  경로: {NEWS_DATA_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    main()
