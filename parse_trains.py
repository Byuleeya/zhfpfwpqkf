"""Pure, fail-closed parser for the KORAIL rendered search-result DOM.

Only the first ten result cards are monitored; no click on 'more'.
This module never makes network requests.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from typing import Any, Mapping, Sequence
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
WATCH_DATE = date(2026, 10, 24)
END_AT = datetime(2026, 10, 25, 0, 0, tzinfo=KST)
VALID_DESTINATIONS = {"서울", "용산", "수서"}
ORIGIN_STATION = "나주"
FARE_RE = re.compile(r"(?<![\d])(?:\d{1,3}(?:,\d{3})+|\d{4,7})\s*원")
TIMES_RE = re.compile(r"(\d{2}:\d{2})\s*[~～\-]\s*(\d{2}:\d{2})")
STOPWORDS = ("입석", "자유석", "매진", "예약불가", "예약마감", "발매중지", "환승")

BOOKABLE = "BOOKABLE"
WAITLIST = "WAITLIST"
SOLD_OUT = "SOLD_OUT"
STANDING = "STANDING"
UNKNOWN = "UNKNOWN"


class InvalidPage(ValueError):
    """Required search criteria or result structure not verified."""


@dataclass(frozen=True)
class Train:
    number: str
    kind: str
    origin: str
    destination: str
    departure: str
    arrival: str
    general: str
    special: str
    general_text: str
    special_text: str
    general_price: str | None
    special_price: str | None

    @property
    def key(self) -> str:
        return f"20261024:{self.number}:{self.departure}:{self.destination}"

    def is_excluded(self) -> bool:
        return self.number == "456"

    def alerts(self) -> list[tuple[str, str, str | None]]:
        outputs = []
        for grade, label in (("general", "일반실"), ("special", "특실")):
            status = getattr(self, grade)
            if status in (BOOKABLE, WAITLIST):
                outputs.append((grade, status, getattr(self, f"{grade}_price")))
        return outputs


def clean_text(text: str) -> str:
    return " ".join(str(text or "").split())


def classify_box(text: str, classes: Sequence[str] = ()) -> tuple[str, str | None]:
    """Classify *one cabin cell*, not the whole train/page.

    A price is accepted only if explicitly displayed INSIDE its own price cell.
    """
    raw = clean_text(text)
    compact = re.sub(r"\s+", "", raw)
    cls = set(classes)
    if not raw:
        return UNKNOWN, None
    if "입석" in compact or "자유석" in compact or "환승" in compact:
        return STANDING, None
    if "예약대기" in compact:
        if any(marker in compact for marker in ("예약대기불가", "예약대기마감", "예약대기종료", "예약대기없음")):
            return SOLD_OUT, None
        return WAITLIST, None
    if any(marker in compact for marker in ("매진", "예약불가", "예약마감", "발매중지")):
        return SOLD_OUT, None
    price_match = FARE_RE.search(raw)
    if price_match:
        if "sold_out" in cls or "disabled" in cls or "btn_disabled" in cls:
            return UNKNOWN, None
        return BOOKABLE, price_match.group().replace(" ", "")
    return UNKNOWN, None


def verify_context(meta: Mapping[str, Any]) -> None:
    selected_date = str(meta.get("date") or "")
    if "2026-10-24" not in selected_date and "2026.10.24" not in selected_date and "20261024" not in selected_date:
        raise InvalidPage(f"검색 날짜 불일치: {selected_date[:40]!r}")
    if str(meta.get("origin") or "").strip() != ORIGIN_STATION:
        raise InvalidPage(f"출발역이 {ORIGIN_STATION}가 아님")
    if str(meta.get("destination") or "").strip() != "서울":
        raise InvalidPage("기준 도착역이 서울이 아님")
    passenger = re.sub(r"\s+", "", str(meta.get("passengers") or ""))
    if passenger not in ("총1명", "성인1명", "1명"):
        raise InvalidPage(f"인원 확인 실패: {passenger[:30]!r}")
    if not bool(meta.get("combined_destinations")):
        raise InvalidPage("서울·용산·수서 함께 보기 미선택")
    if not bool(meta.get("nearby_stations")):
        raise InvalidPage("인접역 보기 미선택")
    if not bool(meta.get("ktx_filter")):
        raise InvalidPage("KTX 필터 미선택")


def parse_row(row_html: str, now: datetime) -> Train | None:
    soup = BeautifulSoup(row_html, "lxml")
    item = soup.select_one("li.tckList")
    if item is None:
        raise InvalidPage("열차 카드 li.tckList 구조가 아님")
    num = item.select_one(".flag_wrap .num")
    h3 = item.select_one(".data_box h3")
    if not num or not h3:
        raise InvalidPage("열차번호/역·시각 요소 누락")
    number = num.get_text(strip=True)
    if not re.fullmatch(r"\d{1,5}", number):
        raise InvalidPage("열차번호 분석 실패")
    kind_node = item.select_one(".flag_wrap .train_sancheon_ticket, .flag_wrap .train_ktx_ticket")
    if kind_node is None:
        return None  # no KTX trains should be considered
    kind = "KTX-산천" if "train_sancheon_ticket" in kind_node.get("class", []) else "KTX"
    parts = h3.select("span")
    if len(parts) < 3:
        raise InvalidPage("출발역·도착역·시각 구조 누락")
    origin = parts[0].get_text(strip=True)
    dest = parts[1].get_text(strip=True)
    if origin != ORIGIN_STATION:
        return None  # 인접역 포함 결과라도 나주에서 실제 탑승 가능한 열차만 감시
    if dest not in VALID_DESTINATIONS:
        return None  # 서울·용산·수서 외 도착역은 감시 대상이 아님
    times = TIMES_RE.search(clean_text(parts[2].get_text(" ", strip=True)))
    if not times:
        raise InvalidPage("열차 출발/도착 시각 분석 실패")
    dep, arr = times.groups()
    if number == "456":  # 목포 출발 11:43의 동일 편명은 나주에서도 제외
        return None
    try:
        departure_dt = datetime.combine(WATCH_DATE, time.fromisoformat(dep), tzinfo=KST)
    except ValueError as exc:
        raise InvalidPage("출발 시각 범위 오류") from exc
    if now.astimezone(KST) >= departure_dt:
        return None  # departed trains do not trigger stale alerts

    boxes = item.select(".tck_inner > .price_box")
    if len(boxes) != 2:
        raise InvalidPage(f"{number}편: 일반실·특실 두 셀을 찾지 못함 ({len(boxes)})")
    states = [classify_box(x.get_text(" ", strip=True), x.get("class", ())) for x in boxes]
    return Train(
        number=number, kind=kind, origin=origin, destination=dest,
        departure=dep, arrival=arr,
        general=states[0][0], special=states[1][0],
        general_text=clean_text(boxes[0].get_text(" ", strip=True))[:160],
        special_text=clean_text(boxes[1].get_text(" ", strip=True))[:160],
        general_price=states[0][1], special_price=states[1][1],
    )


def parse_scan(meta: Mapping[str, Any], rows: Sequence[str], now: datetime) -> list[Train]:
    verify_context(meta)
    if not rows:
        raise InvalidPage("열차 목록 없음 (로딩/차단 가능성)")
    if len(rows) > 10:
        # User requested the initial screen, not expanded results. Detect, don't
        # silently read extra cards that may be from an earlier session.
        raise InvalidPage(f"더보기로 확장된 화면 또는 비정상 행 수 ({len(rows)})")
    trains = []
    found = set()
    for raw in rows:
        train = parse_row(raw, now)
        if train is None:
            continue
        if train.key in found:
            raise InvalidPage(f"중복 열차 카드: {train.key}")
        found.add(train.key)
        trains.append(train)
    if not trains:
        # On the actual travel day after all departures, it's okay; the watcher
        # must not invent an all-sold-out result from zero matching rows.
        raise InvalidPage("대상 KTX가 한 건도 확인되지 않음")
    return trains


def snapshot_from_saved_html(html: str) -> tuple[dict[str, Any], list[str]]:
    """Offline test helper. This does NOT fetch new KORAIL availability."""
    soup = BeautifulSoup(html, "lxml")
    def value(css: str) -> str:
        obj = soup.select_one(css)
        return str(obj.get("value", "") if obj else "")
    ktx = soup.select_one("#btn_tab_info2")
    meta = {
        "date": value("#startDate"), "origin": value("#labelstart"),
        "destination": value("#labelend"), "passengers": value("#labelple"),
        "nearby_stations": bool(soup.select_one("#adjStnScdlOfrFlg:checked")),
        "combined_destinations": bool(soup.select_one("#adjStnScdlOfrFlg2:checked")),
        "ktx_filter": bool(ktx and "active" in ktx.get("class", [])),
    }
    return meta, [str(r) for r in soup.select("li.tckList")]
