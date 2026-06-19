#!/usr/bin/env python3
"""
Trend Tracker 뉴스 자동 수집 스크립트 v5
- 대상: 핀테크 / 전통금융(카드사·은행)만 수집 (이커머스·버티컬커머스·마케팅이벤트 제외)
- 수동 큐레이션 기사 보호 (curated=true 플래그)
- 네이버 뉴스 검색 API로 금융 서비스 뉴스 수집
- 6단계 필터링: 날짜→노이즈→브랜드(제목Only)→AI검증(브랜드명포함)→분석→최종검증
- news_data.json 업데이트
"""

import json
import os
import re
import html
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests


# ── ★ v6: 텍스트 정제 유틸 ──
def clean_text(s: str) -> str:
    """HTML 태그 제거 + 엔티티 디코딩(&apos; &quot; &amp; 등) + 공백 정리.
    네이버/RSS 원문에 남는 &apos;, &quot;, &lt; 등이 화면에 그대로 노출되는 문제를 막는다.
    """
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)          # 태그 제거
    s = html.unescape(s)                     # 엔티티 → 실제 문자
    s = html.unescape(s)                     # 이중 인코딩(&amp;apos;) 대비 한 번 더
    s = s.replace("​", "").replace("﻿", "")
    s = re.sub(r"\s+", " ", s).strip()       # 연속 공백 정리
    return s


def clean_title(s: str) -> str:
    """수집 시 끝이 잘려 들어온 제목 꼬리를 정리.
    예: "...'페이 재테크' [S머니..." → "...'페이 재테크'"  (잘린 대괄호/말줄임표 제거)
    """
    s = clean_text(s)
    # 끝에 닫히지 않고 말줄임표로 잘린 [코너명…/(…) 조각 제거
    s = re.sub(r"\s*[\[\(][^\]\)]{0,25}(?:\.{2,}|…)\s*$", "", s)
    # 남은 말줄임표(…/...) 꼬리 제거
    s = re.sub(r"\s*(?:\.{2,}|…)\s*$", "", s).strip()
    # 단어 중간에서 잘린 끝(공백 뒤 1글자 한글, 종결부호 없음) → 잘린 조각 떼고 '…' 부착
    #   예) "...정산까지 디지털 인" → "...정산까지 디지털…"  (소스가 자른 'ㅍ프라 구축'은 복원 불가)
    m = re.search(r"\s[가-힣]$", s)
    if m and len(s) > 15 and not re.search(r"[.!?\"'’”」』)\]]$", s):
        s = s[:m.start()].rstrip() + "…"
    return s


def _extract_og_title(htmltext: str) -> str:
    """HTML에서 og:title(없으면 <title>) 추출 + 언론사명 꼬리 제거."""
    cand = ""
    for pat in (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
                r"<title[^>]*>([^<]+)</title>"):
        mm = re.search(pat, htmltext, re.I | re.S)
        if mm:
            cand = html.unescape(mm.group(1))
            break
    cand = re.sub(r"\s+", " ", cand).strip()
    # ' - 전자신문', ' | ZDNet', ' :: 매체' 같은 매체명 꼬리 제거
    cand = re.sub(r"\s*[\|\-–—:]{1,2}\s*[^|\-–—:]{1,15}$", "", cand).strip()
    return cand


def recover_full_title(title: str, url: str) -> str:
    """제목이 잘린 경우(끝이 …)만 원문 페이지 og:title로 전체 제목 복원 시도.
    수집(GitHub Actions)·로컬에서 실행되며, 실패하면 기존 title을 그대로 유지한다.
    """
    if not url or not title.endswith("…"):
        return title
    try:
        r = requests.get(url, timeout=8, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; TrendTrackerBot/1.0)"})
        if r.status_code != 200 or not r.text:
            return title
        cand = _extract_og_title(r.text)
        # 복원본이 잘린 제목보다 길고(=더 많은 내용) 합리적 길이면 채택
        if cand and len(cand) > len(title.rstrip("…")) and len(cand) <= 120:
            return cand
    except Exception as e:
        print(f"    [제목복원 실패] {e}")
    return title


def smart_truncate(s: str, limit: int = 150) -> str:
    """단어/문장 경계에서 자연스럽게 자르고 '…'을 붙인다.
    desc[:120]처럼 단어 중간에서 끊겨 '…15일 네'가 노출되던 문제를 개선.
    """
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    # 문장 부호(. 다/요/음 등) 우선, 없으면 마지막 공백에서 절단
    m = re.search(r"[.!?…](?!.*[.!?…])", cut)
    if m and m.end() >= limit * 0.6:
        return cut[:m.end()].strip()
    sp = cut.rfind(" ")
    if sp >= limit * 0.6:
        cut = cut[:sp]
    return cut.strip() + "…"


def first_sentence(text: str, max_len: int = 95) -> str:
    """문장의 첫 문장만 추출(카드용 한 줄 요약). 마침표/물음표/느낌표 기준.
    너무 길면 smart_truncate로 자연스럽게 자른다."""
    t = (text or "").strip()
    if not t:
        return ""
    parts = re.split(r"(?<=[.!?])\s", t)
    s = parts[0].strip() if parts else t
    if len(s) > max_len:
        s = smart_truncate(s, max_len)
    return s


def make_summary(enrichment: dict, article: dict) -> str:
    """카드에 보일 한 줄 요약. AI summary → detail 첫문장 → 원본 desc 순."""
    s = (enrichment.get("summary") or "").strip()
    if s:
        return s
    s = first_sentence(enrichment.get("detail") or "")
    if s:
        return s
    return article.get("desc", "")


# ── 설정 ──
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
NEWS_DATA_PATH = Path(__file__).parent.parent / "news_data.json"
MAX_ITEMS = 800  # ★ v6: 보관 상한(안전 ceiling). 실제 보관 기간은 DAYS_TO_KEEP가 결정
DAYS_TO_KEEP = 180  # ★ v6: 최근 180일(6개월) 보관, 이보다 오래된 비고정 기사는 삭제
# ★ v6: 최근 21일 이내 기사만 수집 (균형 완화 — 14→21일로 수집창 확대)
MIN_DATE = (datetime.now() - timedelta(days=21)).strftime("%Y-%m-%d")

# ── 타겟 키워드 (핀테크 / 전통금융 + 글로벌 결제·핀테크) ──
KEYWORDS = [
    # 핀테크 / 결제·인터넷은행 서비스
    "네이버페이", "카카오페이", "토스뱅크", "토스페이", "토스증권",
    "카카오뱅크", "케이뱅크", "페이코",
    "간편결제", "모바일결제", "QR결제", "안면인식결제",
    "오픈뱅킹", "마이데이터", "송금",
    # 전통금융 (카드사)
    "신한카드", "삼성카드", "현대카드", "KB카드", "KB국민카드",
    "우리카드", "하나카드", "롯데카드",
    # 전통금융 (은행) ★ v6
    "KB국민은행", "국민은행", "신한은행", "하나은행", "우리은행",
    "NH농협은행", "농협은행", "IBK기업은행", "기업은행",
    "BNK부산은행", "BNK경남은행", "BNK금융", "부산은행", "경남은행",
    # 글로벌 결제·핀테크 ★ v6 (해외 탭)
    "비자", "마스터카드", "페이팔", "스트라이프", "알리페이",
    "위챗페이", "클라르나", "레볼루트", "아멕스",
]

# ── 네이버 뉴스 검색 쿼리 ──
# ★ v4: 브랜드명 필수 + "출시/업데이트/개편" 등 서비스 변화 키워드 조합
# 광범위 쿼리 제거, 각 쿼리에 expect_brand 지정 (결과 검증용)
NAVER_SEARCH_QUERIES = [
    # ── ★ v6: 자사(현대카드) 앱·디지털 프로덕트 최우선 ──
    {"q": "현대카드 앱 개편", "type": "service", "expect_brand": "현대카드"},
    {"q": "현대카드 앱 리뉴얼", "type": "service", "expect_brand": "현대카드"},
    {"q": "현대카드 앱 업데이트", "type": "service", "expect_brand": "현대카드"},
    {"q": "현대카드 앱 신규 기능", "type": "service", "expect_brand": "현대카드"},
    {"q": "현대카드 UX 개선", "type": "service", "expect_brand": "현대카드"},
    {"q": "현대카드 M포인트 몰", "type": "service", "expect_brand": "현대카드"},
    {"q": "현대카드 디지털 서비스", "type": "service", "expect_brand": "현대카드"},
    # ── 동종 카드사 앱/디지털 (벤치마킹) ──
    {"q": "신한카드 앱 개편", "type": "service", "expect_brand": "신한카드"},
    {"q": "신한 쏠페이 앱", "type": "service", "expect_brand": "신한카드"},
    {"q": "삼성카드 앱 개편", "type": "service", "expect_brand": "삼성카드"},
    {"q": "KB페이 앱 개편", "type": "service", "expect_brand": "KB국민"},
    {"q": "우리카드 앱 리뉴얼", "type": "service", "expect_brand": "우리카드"},
    {"q": "하나카드 앱 개편", "type": "service", "expect_brand": "하나카드"},
    {"q": "롯데카드 앱 개편", "type": "service", "expect_brand": "롯데카드"},
    # ── 핀테크 앱/기능 (벤치마킹) ──
    {"q": "네이버페이 앱 업데이트", "type": "service", "expect_brand": "네이버페이"},
    {"q": "카카오페이 앱 개편", "type": "service", "expect_brand": "카카오페이"},
    {"q": "토스 앱 업데이트", "type": "service", "expect_brand": "토스"},
    {"q": "카카오뱅크 앱 신규 기능", "type": "service", "expect_brand": "카카오뱅크"},
    {"q": "케이뱅크 앱 개편", "type": "service", "expect_brand": "케이뱅크"},
    {"q": "페이코 앱 업데이트", "type": "service", "expect_brand": "페이코"},
    # ── 은행 앱/서비스 ★ v6 (국내) ──
    {"q": "KB국민은행 앱 개편", "type": "service", "expect_brand": "KB국민은행"},
    {"q": "신한은행 앱 개편", "type": "service", "expect_brand": "신한은행"},
    {"q": "하나은행 앱 서비스", "type": "service", "expect_brand": "하나은행"},
    {"q": "우리은행 앱 개편", "type": "service", "expect_brand": "우리은행"},
    {"q": "IBK기업은행 앱 서비스", "type": "service", "expect_brand": "IBK기업은행"},
    {"q": "NH농협은행 앱 서비스", "type": "service", "expect_brand": "NH농협은행"},
    # ── 글로벌 결제·핀테크 ★ v6 (해외 탭) ──
    {"q": "비자 결제 서비스", "type": "service", "expect_brand": "비자"},
    {"q": "마스터카드 결제", "type": "service", "expect_brand": "마스터카드"},
    {"q": "페이팔 서비스", "type": "service", "expect_brand": "페이팔"},
    {"q": "스트라이프 결제", "type": "service", "expect_brand": "스트라이프"},
    {"q": "알리페이 서비스", "type": "service", "expect_brand": "알리페이"},
    # ── 일반 서비스 변화 (앱 외 정책/신상품도 보조 수집) ──
    {"q": "현대카드 서비스 출시", "type": "service", "expect_brand": "현대카드"},
    {"q": "신한카드 서비스", "type": "service", "expect_brand": "신한카드"},
    {"q": "토스뱅크 서비스 도입", "type": "service", "expect_brand": "토스"},
    {"q": "네이버페이 도입", "type": "service", "expect_brand": "네이버페이"},
    # ── 시장·동향 (앱분석사·기관 발표) ★ v6 ──
    {"q": "와이즈앱 금융 앱 사용자", "type": "trend", "expect_brand": "와이즈앱"},
    {"q": "와이즈앱 간편결제 분석", "type": "trend", "expect_brand": "와이즈앱"},
    {"q": "모바일인덱스 금융 앱 MAU", "type": "trend", "expect_brand": "모바일인덱스"},
    {"q": "간편결제 시장 규모", "type": "trend", "expect_brand": ""},
    {"q": "핀테크 앱 사용자 순위", "type": "trend", "expect_brand": ""},
    {"q": "카드사 앱 MAU 순위", "type": "trend", "expect_brand": ""},
]

# ── 국내 브랜드 매핑 (정밀 키워드만, 모호한 단어 배제) ──
BRAND_MAP = {
    # ── 핀테크 / 결제·인터넷은행 ──
    "네이버페이": ("네이버페이", "b-naver"),
    "카카오페이": ("카카오페이", "b-kakao"),
    "카카오뱅크": ("카카오뱅크", "b-kakaobank"),
    "케이뱅크": ("케이뱅크", "b-kbank"),
    "토스뱅크": ("토스", "b-toss"),
    "토스페이": ("토스", "b-toss"),
    "토스증권": ("토스", "b-toss"),
    "페이코": ("페이코", "b-payco"),
    # ── 전통금융 (카드사) ──
    "신한카드": ("신한카드", "b-shinhan"),
    "KB국민카드": ("KB국민", "b-kb"),
    "KB카드": ("KB국민", "b-kb"),
    "삼성카드": ("삼성카드", "b-samsung"),
    "현대카드": ("현대카드", "b-hyundai"),
    "우리카드": ("우리카드", "b-woori"),
    "하나카드": ("하나카드", "b-hana"),
    "롯데카드": ("롯데카드", "b-lottecard"),
    # ── 전통금융 (은행) ★ v6 추가 ──
    "KB국민은행": ("KB국민은행", "b-kbbank"),
    "국민은행": ("KB국민은행", "b-kbbank"),
    "신한은행": ("신한은행", "b-shinhanbank"),
    "하나은행": ("하나은행", "b-hanabank"),
    "우리은행": ("우리은행", "b-wooribank"),
    "NH농협은행": ("농협은행", "b-nh"),
    "농협은행": ("농협은행", "b-nh"),
    "IBK기업은행": ("IBK기업은행", "b-ibk"),
    "기업은행": ("IBK기업은행", "b-ibk"),
    "BNK부산은행": ("BNK금융", "b-bnk"),
    "BNK경남은행": ("BNK금융", "b-bnk"),
    "BNK금융": ("BNK금융", "b-bnk"),
    "부산은행": ("BNK금융", "b-bnk"),
    "경남은행": ("BNK금융", "b-bnk"),
}

# ── 해외(글로벌) 결제·핀테크 브랜드 매핑 ★ v6 — '해외' 탭용 ──
# 한글 표기만 매칭(영문 전용 기사는 한글판과 중복되므로 제외 → 중복 방지)
FOREIGN_BRAND_MAP = {
    "비자": ("Visa", "b-visa"),
    "마스터카드": ("Mastercard", "b-mc"),
    "페이팔": ("PayPal", "b-paypal"),
    "스트라이프": ("Stripe", "b-stripe"),
    "알리페이": ("Alipay", "b-alipay"),
    "위챗페이": ("WeChat Pay", "b-wechat"),
    "클라르나": ("Klarna", "b-klarna"),
    "레볼루트": ("Revolut", "b-revolut"),
    "아멕스": ("Amex", "b-amex"),
    "아메리칸 익스프레스": ("Amex", "b-amex"),
}
# 해외로 분류할 브랜드 표시명 집합 (region 판정용)
FOREIGN_BRANDS = {v[0] for v in FOREIGN_BRAND_MAP.values()}

# ── 시장·동향 (업계 판세/통계) ★ v6 — 와이즈앱 등 앱분석사·기관 발표 ──
# 특정 브랜드 기사가 아닌 '시장 규모·점유율·MAU' 같은 판세 기사를 잡는 경로.
# 제목에 아래 분석사/기관명이 있으면 그 출처를 브랜드로, 없으면 일반 '시장 동향'으로 분류.
TREND_SOURCES = {
    "와이즈앱": ("와이즈앱", "b-wiseapp"),
    "와이즈앱·리테일·굿즈": ("와이즈앱", "b-wiseapp"),
    "모바일인덱스": ("모바일인덱스", "b-mobileindex"),
    "아이지에이웍스": ("모바일인덱스", "b-mobileindex"),
    "닐슨": ("닐슨코리아", "b-nielsen"),
    "한국은행": ("한국은행", "b-bok"),
    "여신금융협회": ("여신금융협회", "b-bok"),
}
# 시장·동향 시그널 키워드 (제목 기준, 1개만 있어도 판세 기사로 간주)
TREND_KEYWORDS = [
    # 정량 통계
    "MAU", "월간 활성", "활성 사용자 수", "시장 규모", "시장 점유율", "점유율",
    "이용 규모", "이용 현황", "거래액", "4강 구도", "3강 구도", "양강 구도",
    "과점", "판도", "전자지급", "사용자 순위", "앱 순위", "이용자 순위",
    # ★ v6 균형완화: 업계 판세·전략·소비 트렌드 분석형 기사도 포함
    "경쟁 심화", "플랫폼 경쟁", "각축", "지각변동", "돌파구", "수익성 악화",
    "업계 동향", "트렌드 부상", "페이 재테크", "소비 트렌드",
    # ★ v6.1: 슈퍼앱·머니무브·선불 등 업계 판세 기사 추가 포착
    "슈퍼앱 경쟁", "슈퍼앱 전쟁", "슈퍼앱 시장", "머니무브",
    "선불 결제 시장", "선불·포인트", "플랫폼으로 탈바꿈", "결제 인프라 확산",
    "앱 통합 경쟁", "전쟁 2라운드",
]
# 시장·동향으로 강제 분류할 브랜드 표시명 집합
TREND_BRANDS = {v[0] for v in TREND_SOURCES.values()} | {"시장 동향"}


def detect_trend(title: str, desc: str = "") -> tuple:
    """시장·동향(판세/통계) 기사 감지. (브랜드, bc) 또는 (None, None).
    - 제목에 분석사/기관명이 있으면 그 출처를 브랜드로
    - 없으면 시장 통계 키워드가 1개 이상일 때 일반 '시장 동향'
    """
    for kw, (b, bc) in TREND_SOURCES.items():
        if kw in title:
            return (b, bc)
    if any(k in title for k in TREND_KEYWORDS):
        return ("시장 동향", "b-market")
    return (None, None)

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

# ── 업권 매핑 (핀테크 / 전통금융 2개만) ──
SECTOR_KEYWORDS = {
    "핀테크": ["간편결제", "핀테크", "페이", "결제", "송금", "인증", "생체", "오픈뱅킹", "마이데이터",
              "네이버페이", "카카오페이", "페이코", "토스뱅크", "토스페이", "토스증권", "카카오뱅크", "케이뱅크",
              "비자", "마스터카드", "페이팔", "스트라이프", "알리페이", "위챗페이"],
    "전통금융": ["신한카드", "KB국민카드", "KB카드", "삼성카드", "현대카드", "우리카드", "하나카드", "롯데카드",
               "신한은행", "국민은행", "KB국민은행", "우리은행", "하나은행", "농협은행", "기업은행", "카드사", "은행"],
}

# ── ★ v6: 앱/디지털 프로덕트 관점 키워드 ──
# 우리 조직(현대카드 앱·M포인트몰·고트럭)의 관심사: 앱의 기능 변화, 신규 업데이트, 리뉴얼, UX 개선.
# 아래 키워드가 제목/요약에 많을수록 '앱 관련성' 점수가 높아지고, 수집·노출에서 우선순위를 갖는다.
APP_KEYWORDS_STRONG = [   # 앱 변화임을 강하게 시사 (가중치 3)
    "앱 개편", "앱 리뉴얼", "앱 업데이트", "앱 출시", "앱 새단장", "앱 전면 개편",
    "어플 개편", "애플리케이션 개편", "UI 개편", "UX 개선", "화면 개편", "인터페이스 개편",
    "사용성 개선", "디자인 개편", "전면 리뉴얼", "리뉴얼 오픈", "앱 리뉴",
    "신규 기능", "기능 추가", "기능 업데이트", "메인 화면 개편", "홈 화면 개편",
    "간편로그인", "간편 로그인", "생체인증", "얼굴인식", "안면인식", "개인화 추천",
]
APP_KEYWORDS_WEAK = [     # 디지털/앱 맥락 신호 (가중치 1)
    "앱", "어플", "애플리케이션", "모바일 앱", "모바일앱", "웹", "홈페이지", "웹사이트",
    "온라인", "디지털", "플랫폼", "서비스 개편", "리뉴얼", "개편", "업데이트", "새단장",
    "UI", "UX", "사용성", "화면", "인터페이스", "디자인", "베타", "사전예약", "오픈",
    "로그인", "인증", "알림", "위젯", "홈 화면", "메인 화면", "개인화", "맞춤",
    "멤버십", "포인트몰", "M포인트", "엠포인트", "구독", "적립", "월렛", "지갑",
    "간편결제", "간편송금", "오픈뱅킹", "마이데이터", "API", "연동", "탑재", "론칭",
]

# 자사 브랜드 — 수집·우선순위에서 추가 가중치 부여
OWN_BRAND = "현대카드"


def app_relevance(title: str, desc: str) -> int:
    """앱/디지털 프로덕트 관점의 관련성 점수.
    강한 신호(앱 개편/UX 개선/신규 기능 등)는 3점, 약한 신호는 1점으로 합산.
    """
    text = title + " " + desc
    score = 0
    for kw in APP_KEYWORDS_STRONG:
        if kw in text:
            score += 3
    for kw in APP_KEYWORDS_WEAK:
        if kw in text:
            score += 1
    # 제목에 신호가 있으면 본문보다 비중 ↑
    for kw in APP_KEYWORDS_STRONG:
        if kw in title:
            score += 2
            break
    return score


def priority_key(article: dict):
    """수집 후보 정렬용 키: (유효 앱점수, 자사 여부, 공식 여부, 날짜) 내림차순.
    앱 관점 소식을 가장 먼저 AI 검증·분석·노출시키되, 자사(현대카드) 앱 소식에는
    소폭 가중치를 더해 경쟁사 기사에 밀려 묻히지 않도록 한다.
    """
    title = article.get("title", "")
    desc = article.get("desc", "")
    app_s = app_relevance(title, desc)
    is_own = 1 if OWN_BRAND in title else 0
    is_official = 1 if article.get("_official") else 0
    # 자사 + 앱 관련 소식이면 앱점수에 보너스(+4)로 상단 유지
    effective = app_s + (4 if (is_own and app_s > 0) else 0)
    return (effective, is_own, is_official, article.get("date", ""))

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

    # === 공식 보도자료 노이즈: 인사/조달/실적 (뉴스룸 피드 대응) ===
    "정기인사", "임원 인사", "임원인사", "임부서장", "승진 인사", "조직 개편 인사",
    "포모사본드", "변동금리부채권", "ABS 발행", "회사채 발행", "후순위채",
    "신종자본증권", "유상증자", "자산유동화증권", "지속가능채권",
    "사회공헌", "봉사활동", "헌혈", "임직원 봉사",

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
    # ★ v6 균형완화: '부동산' 제외 (마이데이터 부동산청약 등 정상 서비스가 걸리던 문제) — 아파트/재건축/분양은 유지
    "자동차", "아파트", "재건축", "분양",
    "의료", "병원", "제약", "바이오",
    "AI 반도체", "AI 칩", "데이터센터",
]

# ── RSS 피드 목록 (언론사) ──
RSS_FEEDS = [
    {"url": "https://rss.etnews.com/Section901.xml", "name": "전자신문"},
    {"url": "https://rss.etnews.com/Section902.xml", "name": "전자신문 금융"},
    {"url": "https://www.zdnet.co.kr/rss/", "name": "ZDNet Korea"},
    {"url": "https://www.inews24.com/rss/news_it.xml", "name": "아이뉴스24"},
    {"url": "https://www.dt.co.kr/rss/today.xml", "name": "디지털타임스"},
    {"url": "https://www.bloter.net/rss", "name": "블로터"},
]

# ── 공식 뉴스룸/보도자료 피드 ──
# ★ v5: 금융사 공식 보도자료(뉴스룸) 소스 추가.
#   뉴스와이어(Korea Newswire)의 '분야별 RSS'를 사용하면 카드사·은행·핀테크 등
#   각 금융사가 직접 배포한 보도자료를 회사별 ID 없이 한 번에 수집할 수 있다.
#   (산업 분류 번호: 카드=202, 핀테크=207, 은행과 금융=201, 금융 전체=200)
#   이 피드의 기사는 '공식 발표'이므로 _official=True로 표시해 출처를 '○○ 뉴스룸'으로 라벨링하고,
#   언론사 RSS보다 우선순위를 둔다. (단, 인사/실적/IR 등은 AI 검증 단계에서 동일하게 걸러짐)
NEWSROOM_FEEDS = [
    {"url": "https://api.newswire.co.kr/rss/industry/202", "name": "뉴스와이어 카드"},
    {"url": "https://api.newswire.co.kr/rss/industry/207", "name": "뉴스와이어 핀테크"},
    {"url": "https://api.newswire.co.kr/rss/industry/201", "name": "뉴스와이어 은행·금융"},
]
# 특정 금융사만 콕 집어 구독하고 싶을 때 사용하는 회사별 RSS (선택).
#   주소 형식: https://www.newswire.co.kr/companyNews?content=rss&no=<회사ID>
#   회사ID는 newswire.co.kr에서 '○○ 보도자료' 페이지의 URL(no=) 값.
#   예) 신한카드=669. 필요하면 아래에 추가하면 NEWSROOM_FEEDS와 동일하게 처리된다.
NEWSROOM_COMPANY_FEEDS = [
    # {"url": "https://www.newswire.co.kr/companyNews?content=rss&no=669", "name": "신한카드 뉴스룸"},
]


def generate_id(title: str) -> int:
    """제목 기반 고유 ID 생성"""
    h = hashlib.md5(title.encode()).hexdigest()[:8]
    return int(h, 16) % 90000 + 10000


def is_duplicate_article(title_a: str, title_b: str) -> bool:
    """★ v4: 강화된 중복 감지 — 제목 유사도 + 핵심명사 비교

    3가지 기준 중 하나라도 통과하면 중복으로 판단:
    1. 제목 앞 20자 일치
    2. bigram 유사도 0.5 이상 (기존 0.6에서 강화)
    3. 같은 브랜드 + 핵심명사 3개 이상 겹침 (신규)
    """
    clean_a = re.sub(r"[^가-힣a-zA-Z0-9]", "", title_a)
    clean_b = re.sub(r"[^가-힣a-zA-Z0-9]", "", title_b)

    # 기준 1: 앞 20자 일치
    if clean_a[:20] == clean_b[:20]:
        return True

    # 기준 2: bigram 유사도 (0.6 → 0.5로 강화)
    bigrams_a = set(clean_a[i:i+2] for i in range(len(clean_a)-1))
    bigrams_b = set(clean_b[i:i+2] for i in range(len(clean_b)-1))
    if bigrams_a and bigrams_b:
        overlap = len(bigrams_a & bigrams_b) / min(len(bigrams_a), len(bigrams_b))
        if overlap > 0.5:
            return True

    # 기준 3: 핵심 명사(2글자 이상 한글 단어) 3개 이상 겹침
    nouns_a = set(re.findall(r"[가-힣]{2,}", title_a))
    nouns_b = set(re.findall(r"[가-힣]{2,}", title_b))
    # 불용어 제거 (너무 흔한 단어)
    stopwords = {"서비스", "출시", "도입", "기능", "업데이트", "확대", "개편", "론칭",
                 "국내", "최초", "올해", "이번", "진행", "발표", "시작", "예정"}
    nouns_a -= stopwords
    nouns_b -= stopwords
    common_nouns = nouns_a & nouns_b
    if len(common_nouns) >= 3:
        return True

    return False


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

    # === 0) 시장·동향 우선 — 분석사/기관(와이즈앱 등) 발표는 브랜드보다 우선 ===
    #     특정 브랜드가 1위로 언급돼도 '판세 리포트'이므로 시장·동향으로 분류
    for kw, (b, bc) in TREND_SOURCES.items():
        if kw in title:
            return (b, bc)

    # === 1) 국내 브랜드 우선 매칭 ===
    # 국내 브랜드가 주어이면(예: '신한카드, 비자와 제휴') 해외 브랜드가 함께 있어도 국내로 본다
    found = []
    for keyword, (brand, bc) in BRAND_MAP.items():
        if keyword in title and (brand, bc) not in found:
            found.append((brand, bc))
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        # 다중 브랜드라도 분석사(와이즈앱 등) 발표면 시장·동향으로 허용
        tb = detect_trend(title, desc)
        if tb[0]:
            return tb
        print(f"    [다중브랜드] 제목에 국내 {len(found)}개 브랜드 → 제외: {title[:50]}")
        return None, None

    # === 2) 국내 브랜드가 없을 때만 해외(글로벌) 브랜드 매칭 ===
    foreign = []
    for keyword, (brand, bc) in FOREIGN_BRAND_MAP.items():
        if keyword in title and (brand, bc) not in foreign:
            foreign.append((brand, bc))
    if len(foreign) == 1:
        return foreign[0]
    if len(foreign) > 1:
        print(f"    [다중브랜드] 제목에 해외 {len(foreign)}개 브랜드 → 제외: {title[:50]}")
        return None, None

    # === 3) 브랜드가 없을 때 — 시장·동향(판세/통계) 기사 감지 ===
    return detect_trend(title, desc)


def region_of(brand: str) -> str:
    """브랜드 표시명으로 국내/해외 판정"""
    return "해외" if brand in FOREIGN_BRANDS else "국내"


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
                title = clean_title(entry.get("title", ""))
                desc = clean_text(entry.get("summary", entry.get("description", "")))
                link = entry.get("link", "")
                date = parse_date(entry)

                # 날짜 필터
                if not is_recent_enough(date):
                    continue

                if is_relevant(title, desc) and not is_noise(title, desc):
                    articles.append({
                        "title": title,
                        "desc": smart_truncate(desc, 150),
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


def fetch_newsroom_news() -> list:
    """★ v5: 금융사 공식 뉴스룸/보도자료(뉴스와이어 RSS)에서 수집.

    - 언론사 RSS와 달리 is_relevant(키워드) 게이트를 적용하지 않음
      (분야 RSS 자체가 카드/핀테크/은행이므로 금융 관련성은 이미 보장됨)
    - 브랜드 매칭은 이후 공통 파이프라인(detect_brand, 제목 기준)에서 처리
    - _official=True 로 표시해 출처를 '○○ 뉴스룸'으로 라벨링하고 우선순위 부여
    """
    articles = []
    for feed_info in NEWSROOM_FEEDS + NEWSROOM_COMPANY_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:30]:
                title = clean_title(entry.get("title", ""))
                desc = clean_text(entry.get("summary", entry.get("description", "")))
                link = entry.get("link", "")
                date = parse_date(entry)

                # 날짜 필터
                if not is_recent_enough(date):
                    continue

                # 노이즈만 1차 제거 (키워드 게이트는 생략)
                if is_noise(title, desc):
                    continue

                articles.append({
                    "title": title,
                    "desc": smart_truncate(desc, 150),
                    "url": link,
                    "source": feed_info["name"],
                    "date": date,
                    "_qtype": "service",
                    "_official": True,
                })
        except Exception as e:
            print(f"  [뉴스룸] {feed_info['name']} 실패: {e}")

    # 제목 기준 중복 제거
    seen = set()
    unique = []
    for a in articles:
        short = a["title"][:30]
        if short not in seen:
            seen.add(short)
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
                title = clean_title(item.get("title", ""))
                desc = clean_text(item.get("description", ""))
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

                # ★ v6 균형완화: 기대 브랜드 '정확히'가 아니라, 제목에 타겟 브랜드(국내·해외·분석사)가
                #   하나라도 있으면 통과시킨다. (단일 브랜드 여부는 이후 detect_brand가 다시 검증)
                expect_brand = qinfo.get("expect_brand", "")
                if expect_brand:
                    all_keys = list(BRAND_MAP.keys()) + list(FOREIGN_BRAND_MAP.keys()) + list(TREND_SOURCES.keys())
                    has_brand = any(kw in title for kw in all_keys)
                    is_trend = detect_trend(title, desc)[0] is not None  # 업계 동향형도 통과
                    if not (has_brand or is_trend):
                        print(f"    [타겟브랜드없음] 제목={title[:40]}...")
                        continue

                articles.append({
                    "title": title,
                    "desc": smart_truncate(desc, 150),
                    "url": link,
                    "source": "네이버뉴스",
                    "date": date,
                    "_qtype": qtype,
                })
        except Exception as e:
            print(f"  [Naver] 검색 실패 [{query}]: {e}")

    # ★ v4: 강화된 중복 제거
    unique = []
    for a in articles:
        is_dup = any(is_duplicate_article(a["title"], ex["title"]) for ex in unique)
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

    brand_context = ""
    if detected_brand:
        brand_context = f"\n감지된 브랜드: {detected_brand}"

    prompt = f"""당신은 "금융/핀테크 트렌드 트래커" 편집자입니다(국내 카드·은행·간편결제 + 글로벌 결제·핀테크 기업 Visa·Mastercard·PayPal·Stripe·Alipay 등 포함). 아래 기사가 게시 기준에 부합하는지 판단하세요. 기본 태도는 '명백한 노이즈만 걸러내고, 특정 브랜드의 실제 변화면 통과'입니다.

제목: {article['title']}
요약: {article['desc']}{brand_context}

[핵심 판단 기준 — 아래 3가지를 모두 충족해야 "yes"]:
1. 기사의 주인공(subject)이 반드시 위 '감지된 브랜드'의 서비스여야 함
   - 해당 브랜드가 단순히 언급/비교되는 것은 "no"
   - 다른 기업이 주어이고 감지된 브랜드는 배경으로만 등장하면 "no"
2. 기사 내용이 해당 브랜드의 서비스 변화에 관한 것 — 신규 카드·포인트·멤버십·금융상품·앱 기능의 출시·추가·개편·정책 변경은 모두 "yes" 대상
   - 단순 기업 소식(인사/실적/채용/IR)만 "no"
   - (단, 아래 '시장·동향' 예외에 해당하면 여러 기업이 등장해도 "yes")
3. 기사 날짜가 최근 3주 이내여야 하고, 과거 이벤트의 재탕 기사가 아니어야 함

[무조건 "no"인 경우]:
- 트럼프/관세/무역/정치/외교/시사 (핀테크 언급이 곁들여져도 no)
- 단순 할인/쿠폰/적립금/캐시백 프로모션 안내
- 교육/채용/세미나/컨퍼런스/부트캠프
- 주가/실적/IR/투자/IPO/상장
- 금융·결제와 무관한 해외 빅테크(아마존/메타/애플 제품/구글/넷플릭스/테슬라) 중심 → "no"
  단, Visa·Mastercard·PayPal·Stripe·Alipay 등 '글로벌 결제·핀테크 기업'의 결제/금융 서비스 변화는 "yes" 가능
- 제조업/반도체/자동차/부동산
- 이커머스·쇼핑·배달·유통 플랫폼 중심 기사 (쿠팡/네이버쇼핑/배달의민족/무신사/올리브영/컬리/11번가 등) — 결제·금융 서비스가 아닌 쇼핑/물류 소식이면 "no"
- 단순 마케팅·프로모션·팝업·콜라보 캠페인 (서비스/정책 변화가 아닌 홍보성 이벤트)
- 단순 뉴스 브리프/라운드업 (맥락 없이 여러 기업 단신 나열)
- 스포츠/연예
- 기사 제목에 브랜드명이 있으나 실제로는 다른 주제인 기사
- 단순히 2개 브랜드를 비교·나열만 하는 기사
- ★ 앱·결제·사용자 접점이 전혀 없는 순수 B2B 백오피스 기술 기사 (매출채권·기업 유동성·정산 인프라 관리 도구 등) → "no"
  (단, 스테이블코인 결제·토큰화 결제처럼 일반 소비자 결제의 미래와 연결되는 기술은 "yes")

[예외 — '시장·동향'으로 "yes" 가능 (여러 기업이 등장해도 OK)]
- (a) 와이즈앱·모바일인덱스·닐슨·한국은행·여신금융협회 등 분석사·기관의 시장 규모·점유율·MAU·이용 통계 등 정량 리포트
- (b) 금융·결제·카드·핀테크 업계의 의미 있는 판세·전략 흐름을 짚는 동향 기사
      (예: '플랫폼 경쟁 심화', '수익성 악화 속 ○○로 돌파구', '○○ 트렌드 부상', '페이 재테크' 등 업계 흐름·소비 트렌드 분석)
- 단, 단순 할인·쿠폰 홍보, 특정 상품 광고성 기사, 맥락 없는 단신 나열은 "no"

[수집 대상 = 국내 카드·은행·간편결제 + 글로벌 결제·핀테크의 '서비스·정책 변화', 그리고 위 '시장·동향' 기사]
- 결제/송금/인증/금융상품/앱 기능의 출시·개편·정책 변경, 또는 위 '시장·동향'(통계+업계 흐름)에 해당하면 "yes"

명백한 노이즈·무관 기사만 "no"로 거르세요. 특정 브랜드의 실제 변화이거나 위 '시장·동향' 예외에 해당하면, 애매하더라도 "yes"로 통과시키세요.
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


def _extract_json(text: str):
    """모델 응답에서 JSON 객체를 안전하게 추출.
    ```json 코드펜스 제거 + 중괄호 균형을 맞춰 첫 번째 완결 객체를 파싱한다.
    (기존엔 greedy 정규식 하나라 앞뒤 잡텍스트가 있으면 깨질 수 있었음)
    """
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except Exception:
                        return None
    return None


def _enrich_fallback(article: dict, ok: bool = False) -> dict:
    """AI 분석 실패 시의 폴백.
    detail은 단어 중간에서 잘리지 않도록 원문을 문장 경계로 정리해서 사용한다.
    _ok=False 이면 호출부에서 게시를 건너뛴다(무의미한 placeholder 노출 방지).
    """
    base = smart_truncate(clean_text(article.get("desc", "")), 180)
    return {
        "summary": first_sentence(base) or article.get("title", ""),
        "detail": base or article.get("title", ""),
        "impact_text": "",
        "why": "",
        "tags": [],
        "type": "정책 변화",
        "tc": "t-policy",
        "app": app_relevance(article.get("title", ""), article.get("desc", "")) >= 3,
        "imp": 3,
        "il": "보통",
        "_ok": ok,
    }


def enrich_with_ai(article: dict) -> dict:
    """Claude API로 상세 분석 생성. 실패 시 _ok=False 폴백을 반환한다."""
    if not ANTHROPIC_API_KEY:
        # 키가 없으면 분석 불가 → 폴백(_ok=False). 게시 단계에서 건너뜀.
        return _enrich_fallback(article, ok=False)

    # ★ v6: 시장·동향(업계 전반) 기사는 한 회사로 좁히지 말고 전체 흐름을 요약하도록 안내
    _b, _ = detect_brand(article.get("title", ""), article.get("desc", ""))
    trend_note = ""
    if _b in TREND_BRANDS:
        trend_note = ("\n\n[★ 이 기사는 특정 브랜드가 아닌 '업계 동향/시장' 기사입니다]\n"
                      "- summary·detail·impact_text·why·tags 모두 기사가 다루는 '전체 흐름·여러 플레이어의 움직임'을 요약하세요.\n"
                      "- 본문에 특정 회사(예: 현대카드 ○○) 예시가 있어도, 그 회사 개별 상품 소개로 빠지지 마세요. 기사 주제는 업계 트렌드입니다.\n"
                      "- why는 '이 흐름이 우리(현대카드 앱·M포인트몰)에게 주는 시사점' 수준으로만 담백하게.")

    prompt = f"""당신은 현대카드의 디지털 프로덕트를 만드는 팀의 PM/프로덕트 기획자입니다.
아래 뉴스를 '앱/디지털 프로덕트 관점'에서 분석해주세요.{trend_note}

[자사 현황 — 반드시 정확히 반영, 사실과 다른 추정 금지]
- 우리 팀이 운영·관리하는 앱은 정확히 3개입니다: ① 현대카드 앱 ② M포인트몰 ③ 고트럭(커머셜).
- '현대페이' 또는 '현대Pay' 같은 별도 결제 앱은 존재하지 않습니다. 절대 언급하지 마세요.
- QR·바코드·앱카드 기반 결제는 '현대카드 앱'에서 지원하며, 애플페이 연동까지 지원합니다.
- why/impact_text에서 자사를 예로 들 때는 위 3개 앱(특히 현대카드 앱·M포인트몰)만 언급하세요.

[★ 억지 시사점 금지 — 매우 중요]
- 현대카드 앱·M포인트몰·고트럭은 서로 목적이 다른 별개 프로덕트입니다. 이들을 '통합'하거나, 현대캐피탈·현대커머셜 등 그룹 계열사와 엮을 필요·계획이 있다고 가정하지 마세요. (현대카드 앱↔M포인트몰의 자연스러운 연계 정도만 언급 가능)
- 관련 없는 회사·서비스를 끌어와 인위적으로 연결고리를 만들지 마세요.
- 자사에 직접 적용할 진짜 시사점이 없으면, 무리하게 지어내지 말고 일반적인 UX·프로덕트 교훈 수준으로 담백하게 작성하세요.

제목: {article['title']}
요약: {article['desc']}

분석 지침:
- impact_text와 why는 앱/웹 사용자 경험과 프로덕트 기획 관점에 초점을 둡니다
  (예: 화면·플로우 변화, 신규 기능, 로그인/인증/결제 UX, 멤버십·포인트, 개인화, 접근성).
- type은 아래 6가지 중 정확히 하나로 분류합니다(가장 핵심적인 성격 하나만):
  · "신규 서비스"(t-new): 기존에 없던 새 서비스·상품·앱을 처음 출시·론칭
  · "기능 업데이트"(t-update): 기존 서비스·기능의 개선·고도화·개편 (이미 있는 것을 더 좋게)
  · "정책 변화"(t-policy): 약관·수수료·혜택·제도·정책의 변경
  · "제휴·협력"(t-partner): 타사·기관과의 제휴·파트너십·콜라보·공동사업·MOU
  · "기술·인프라"(t-tech): AI 에이전트 결제·스테이블코인·토큰화·블록체인 등 차세대 결제기술/인프라
  · "시장·동향"(t-trend): 시장 규모·점유율·MAU·이용 통계 등 업계 판세 (와이즈앱·모바일인덱스·한국은행 등 분석 발표)
  ※ '신규 서비스'와 '기능 업데이트' 구분이 핵심: 없던 걸 새로 내놓으면 신규 서비스, 있던 걸 개선하면 기능 업데이트.
- "app" 필드: 기사가 앱/웹/디지털 프로덕트의 변화(기능·화면·UX·멤버십몰 등)와 관련되면 true, 아니면 false.

아래 JSON 형식으로만 응답해주세요 (다른 텍스트 없이):
{{
  "summary": "1문장(60자 내외, 해요체). 카드 목록에 보일 핵심 한 줄 요약 — 무엇을/누가 했는지 또렷하게",
  "detail": "3-4문장, 해요체. ★summary 문장을 그대로 반복하지 말 것. '무엇이 구체적으로 달라졌는지'(추가·변경된 기능, 작동 방식, 적용 범위, 수치, 이전 대비 차이)를 중심으로 설명",
  "impact_text": "2-3문장, 앱/디지털 사용자에게 어떤 영향이 있는지",
  "why": "2-3문장, 현대카드 앱·M포인트몰·고트럭 기획 시 참고할 포인트 (없는 앱/서비스를 지어내지 말 것)",
  "tags": ["키워드1", "키워드2", "키워드3"],
  "type": "신규 서비스|기능 업데이트|정책 변화|제휴·협력|기술·인프라|시장·동향",
  "tc": "t-new|t-update|t-policy|t-partner|t-tech|t-trend",
  "app": true,
  "imp": 3~5,
  "il": "높음|보통"
}}"""

    # ★ v6: 최대 2회 시도 + 견고한 JSON 파싱 + 결과 검증
    for attempt in range(2):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            if resp.status_code != 200:
                print(f"    [AI분석] HTTP {resp.status_code} (시도 {attempt+1}/2)")
                continue
            text = resp.json()["content"][0]["text"]
            data = _extract_json(text)
            # 핵심 3개 필드(변경점/사용자 영향/실무 포인트)가 모두 의미있게 채워졌는지 검증
            if data and all(str(data.get(k, "")).strip() for k in ("detail", "impact_text", "why")):
                data["_ok"] = True
                return data
            print(f"    [AI분석] 필수 필드 누락/파싱 실패 (시도 {attempt+1}/2)")
        except Exception as e:
            print(f"    [AI분석] 예외: {e} (시도 {attempt+1}/2)")

    # 2회 모두 실패 → 게시 건너뛸 폴백
    return _enrich_fallback(article, ok=False)


def build_news_item(article: dict, enrichment: dict) -> dict:
    """뉴스 아이템 JSON 구조 생성"""
    brand, bc = detect_brand(article["title"], article["desc"])
    sector = detect_sector(article["title"], article["desc"])

    tags = enrichment.get("tags", [])
    if not tags:
        text = article["title"] + " " + article["desc"]
        tags = [kw for kw in KEYWORDS if kw in text][:3]

    # ★ v5: 공식 뉴스룸 출처는 '○○ 보도자료(뉴스룸)'으로 라벨링
    if article.get("_official"):
        src_t = f"{brand} 보도자료" if brand else article["source"]
        src_ty = "뉴스룸"
    else:
        src_t = article["source"]
        src_ty = "기사"

    # ★ v6: 앱/디지털 프로덕트 관련성 — AI 판단(app)과 키워드 점수를 함께 사용
    app_score = app_relevance(article["title"], article["desc"])
    ai_app = enrichment.get("app")
    is_app = bool(ai_app) if ai_app is not None else (app_score >= 3)

    # ★ v6: 6종 타입 정규화 (레거시/이상치 보정)
    #   신규 서비스 / 기능 업데이트 / 정책 변화 / 제휴·협력 / 기술·인프라 / 시장·동향
    TYPE_MAP = {
        "신규 서비스": ("신규 서비스", "t-new"),
        "신규 기능": ("신규 서비스", "t-new"),       # 레거시 → 신규 서비스
        "기능 업데이트": ("기능 업데이트", "t-update"),
        "UX 개선": ("기능 업데이트", "t-update"),     # 폐기 → 기능 업데이트로 흡수
        "정책 변화": ("정책 변화", "t-policy"),
        "제휴·협력": ("제휴·협력", "t-partner"),
        "제휴": ("제휴·협력", "t-partner"),
        "기술·인프라": ("기술·인프라", "t-tech"),
        "시장·동향": ("시장·동향", "t-trend"),
        "마케팅 이벤트": ("기능 업데이트", "t-update"),  # 폐기 카테고리 보정
    }
    e_type, e_tc = TYPE_MAP.get(enrichment.get("type", ""), ("기능 업데이트", "t-update"))

    # ★ 시장·동향 출처(와이즈앱 등)로 잡힌 기사는 무조건 '시장·동향'으로
    if brand in TREND_BRANDS:
        e_type, e_tc = "시장·동향", "t-trend"

    # type이 앱 변화 계열이면 앱으로 간주(우선순위용)
    if e_tc in ("t-new", "t-update") and app_score > 0:
        is_app = True

    # ★ v6.1: 잘린 제목(…)이면 원문 og:title로 전체 제목 복원 (게시 대상에만 적용 → 호출 최소)
    full_title = recover_full_title(article["title"], article.get("url", ""))

    return {
        "id": generate_id(article["title"]),
        "title": full_title,
        "desc": article["desc"],
        "summary": make_summary(enrichment, article),
        "detail": enrichment.get("detail", article["desc"]),
        "impact_text": enrichment.get("impact_text", ""),
        "why": enrichment.get("why", ""),
        "brand": brand,
        "bc": bc,
        "sector": sector,
        "region": region_of(brand),
        "type": e_type,
        "tc": e_tc,
        "tags": tags[:3],
        "imp": enrichment.get("imp", 3),
        "il": enrichment.get("il", "보통"),
        "date": article["date"],
        "isEvent": False,
        "official": bool(article.get("_official")),
        "app": is_app,
        "app_score": app_score,
        "src": [{"t": src_t, "url": article["url"], "ty": src_ty}],
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
    print("Trend Tracker v6 - 뉴스 업데이트 (핀테크/전통금융 + 공식 뉴스룸, 앱 관점 우선)")
    print("=" * 60)
    print(f"  실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  AI 분석: {'활성' if ANTHROPIC_API_KEY else '비활성'}")
    print(f"  네이버 검색: {'활성' if NAVER_CLIENT_ID else '비활성'}")
    print(f"  공식 뉴스룸 피드: {len(NEWSROOM_FEEDS) + len(NEWSROOM_COMPANY_FEEDS)}개")
    print(f"  최소 날짜: {MIN_DATE}")

    # 1. 기존 데이터 로드 → curated 기사 보호
    existing = load_existing()
    curated = [item for item in existing if item.get("curated")]
    non_curated = [item for item in existing if not item.get("curated")]
    existing_titles = {item["title"][:30] for item in existing}
    print(f"\n[1] 기존 뉴스: {len(existing)}건 (큐레이션 {len(curated)}건 보호)")

    # 2. 금융사 공식 뉴스룸(보도자료) 수집  ★ v5 신규
    newsroom_articles = fetch_newsroom_news()
    print(f"\n[2] 공식 뉴스룸 수집: {len(newsroom_articles)}건")

    # 3. RSS에서 뉴스 수집 (언론사)
    rss_articles = fetch_rss_news()
    print(f"\n[3] 언론 RSS 수집: {len(rss_articles)}건")

    # 4. 네이버 뉴스 검색
    naver_articles = fetch_naver_news()
    print(f"\n[4] 네이버 검색: {len(naver_articles)}건")

    # 5. ★ v4: 강화된 중복 제거 (수집 기사 간)
    #    공식 뉴스룸을 맨 앞에 두어 동일 사안일 때 공식 보도자료가 우선 채택되도록 함
    all_raw = newsroom_articles + naver_articles + rss_articles
    raw_articles = []
    for a in all_raw:
        is_dup = any(is_duplicate_article(a["title"], ex["title"]) for ex in raw_articles)
        if not is_dup:
            raw_articles.append(a)

    # 5. ★ v4: 기존 뉴스와 중복 제거 (강화된 비교)
    new_articles = []
    for a in raw_articles:
        is_dup = any(is_duplicate_article(a["title"], ex["title"]) for ex in existing)
        if not is_dup:
            new_articles.append(a)
        else:
            print(f"  [중복] 기존 기사와 유사: {a['title'][:40]}...")
    print(f"\n[5] 신규 기사 후보: {len(new_articles)}건 (중복 제거 후)")

    # 6. ★ v4: 제목 전용 브랜드 매칭 (본문 매칭 완전 제거)
    branded = []
    for a in new_articles:
        brand, bc = detect_brand(a["title"], a["desc"])
        if brand is not None:
            branded.append(a)
        else:
            print(f"  [브랜드X] {a['title'][:50]}...")
    print(f"\n[6] 브랜드 매칭: {len(branded)}건 통과 ({len(new_articles)-len(branded)}건 제외)")

    # ★ v6: 앱 관점 우선순위 정렬 — 앱/UX/리뉴얼·자사(현대카드) 소식이 먼저 검증·분석·노출되도록
    branded.sort(key=priority_key, reverse=True)
    app_cnt = sum(1 for a in branded if app_relevance(a["title"], a["desc"]) > 0)
    print(f"    └ 앱 관점 우선정렬 적용 (앱 관련 후보 {app_cnt}건 상단 배치)")

    # 7. ★ v4: AI 관련성 검증 (브랜드명 포함하여 교차 검증)
    #    공식 뉴스룸 기사도 동일하게 검증해 인사/실적/IR 보도자료를 걸러냄
    # ★ v6: 회당 게시 상한 10건 (검증 풀은 더 넉넉히 둠)
    ADD_PER_RUN = 10
    VALIDATE_POOL = 25
    validated = []
    for i, article in enumerate(branded[:VALIDATE_POOL]):
        brand, _ = detect_brand(article["title"], article["desc"])
        tag = "공식" if article.get("_official") else "기사"
        print(f"  [AI검증 {i+1}/{min(len(branded),VALIDATE_POOL)}] [{brand}/{tag}] {article['title'][:32]}...", end="")
        if ai_validate_article(article, detected_brand=brand):
            validated.append(article)
            print(" -> 통과")
        else:
            print(" -> 제외")
    print(f"\n[7] AI 검증: {len(validated)}건 통과")

    # 8. 상세 분석 생성 (회당 최대 ADD_PER_RUN건 - 품질 우선)
    #    ★ v6: 분석 실패(_ok=False) 기사는 게시하지 않음 → 잘린 원문/placeholder 노출 방지
    new_items = []
    for i, article in enumerate(validated[:ADD_PER_RUN + 4]):
        print(f"  [분석 {i+1}/{min(len(validated),ADD_PER_RUN+4)}] {article['title'][:40]}...")
        enrichment = enrich_with_ai(article)

        if not enrichment.get("_ok"):
            print(f"    -> AI 분석 미완성(상세/영향/포인트 누락), 게시 제외")
            continue

        item = build_news_item(article, enrichment)

        # ★ 최종 안전장치: brand가 None이면 절대 추가하지 않음
        if item["brand"] is None:
            print(f"    -> 브랜드 없음, 최종 제외")
            continue

        new_items.append(item)
        if len(new_items) >= ADD_PER_RUN:
            break

    print(f"\n[8] 신규 추가: {len(new_items)}건")

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
    curated_count = sum(1 for item in all_items if item.get("curated"))
    print(f"\n{'=' * 60}")
    print(f"완료! 총 {len(all_items)}건 저장")
    print(f"  큐레이션: {curated_count}건 (보호)")
    print(f"  서비스 업데이트: {len(all_items)}건")
    print(f"  경로: {NEWS_DATA_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    main()
