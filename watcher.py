"""KORAIL official website first-page availability watcher (Railway + Telegram).

Read-only. No login, reservation, seat holding, CAPTCHA/queue bypass,
or rotating identities/IP addresses.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from browser import BrowserReader, AccessRestricted, SetupFailed, BrowserReadFailed, LIST
from parse_trains import BOOKABLE, WAITLIST, SOLD_OUT, STANDING, UNKNOWN, END_AT, Train

KST = ZoneInfo("Asia/Seoul")
BOT_VERSION = "v7-browser-naju-actual-calendar"


def log(message: str) -> None:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    print(f"[{now}] {message}", flush=True)


def get_setting(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


@dataclass(frozen=True)
class Config:
    token: str
    chat_id: str
    poll_min: float
    poll_max: float
    mode: str
    state_file: Path
    heartbeat_minutes: int

    @classmethod
    def load(cls) -> "Config":
        token = get_setting("TELEGRAM_BOT_TOKEN", "")
        chat_id = get_setting("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수 필요")
        a = float(get_setting("POLL_MIN_SECONDS", "20"))
        b = float(get_setting("POLL_MAX_SECONDS", "30"))
        if a < 15 or b < a or b > 3600:
            raise ValueError("감시 간격: 최소 15초 이상, 최대>=최소, 최대 3600초 이하")
        mode = get_setting("REFRESH_MODE", "reload")
        if mode not in ("reload", "button"):
            raise ValueError("REFRESH_MODE must be reload or button")
        return cls(token, chat_id, a, b, mode,
                   Path(get_setting("STATE_FILE", "korail_web_v7_naju_state.json")),
                   max(0, int(get_setting("HEARTBEAT_MINUTES", "60"))))


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    async def send(self, text: str) -> bool:
        def action():
            data = urlencode({"chat_id": self.chat_id, "text": text,
                              "disable_web_page_preview": "true"}).encode("utf-8")
            req = Request(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          data=data, method="POST")
            with urlopen(req, timeout=16) as r:
                payload = json.loads(r.read().decode("utf-8"))
            if not payload.get("ok"):
                raise RuntimeError("Telegram API returned ok=false")
        try:
            await asyncio.to_thread(action)
            return True
        except Exception as exc:
            log(f"Telegram 전송 오류: {type(exc).__name__} (토큰은 기록하지 않음)")
            return False


def read_state(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text("utf-8"))
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def save_state(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def group_events(trains: list[Train], previous: dict[str, str]) -> tuple[list[tuple[Train, str, str]], dict[str, str]]:
    """Return notifications and non-positive status updates separately.

    Positive status is recorded ONLY when Telegram sends successfully.
    """
    events = []
    closed = {}
    for train in trains:
        for grade in ("general", "special"):
            status = getattr(train, grade)
            key = f"{train.key}:{grade}"
            if status in (BOOKABLE, WAITLIST):
                if previous.get(key) != status:
                    events.append((train, grade, status))
            elif status in (SOLD_OUT, STANDING):
                closed[key] = status
            # UNKNOWN must never reset a valid baseline
    return events, closed


def format_alert(events: list[tuple[Train, str, str]]) -> str:
    lines = ["🚄 코레일 KTX 좌석/예약대기 발견!",
             "2026-10-24(토) 나주 → 서울·용산·수서 | 성인 1명", ""]
    for train, grade, status in events[:20]:
        cabin = "일반실" if grade == "general" else "특실"
        label = "✅ 즉시 예매 가능" if status == BOOKABLE else "🕓 예약대기 신청 가능"
        price = getattr(train, f"{grade}_price")
        lines.append(f"{train.kind} {train.number} | 나주 {train.departure} → {train.destination} {train.arrival}")
        lines.append(f"  {cabin}: {label}" + (f" · {price}" if price else ""))
    if len(events) > 20:
        lines.append(f"외 {len(events)-20}건")
    lines.extend(["", "※ 예약대기는 좌석 확정이 아니며, 금방 마감될 수 있습니다.",
                  "코레일에서 실제 상태를 직접 확인하세요.", LIST])
    return "\n".join(lines)


def summarize(trains: list[Train]) -> str:
    return " / ".join(
        f"{t.number} {t.departure}→{t.destination} "
        f"일반:{t.general} 특실:{t.special}" for t in trains
    )


async def main() -> int:
    cfg = Config.load()
    telegram = Telegram(cfg.token, cfg.chat_id)
    now = datetime.now(KST)
    if now >= END_AT:
        log("자동 종료 시각이 이미 지났음. 감시 실행하지 않음")
        return 0
    log(f"코레일 웹 감시 {BOT_VERSION} 시작 / 10월24일 나주→서울·용산·수서 KTX 첫 10개")
    log(f"설정: {cfg.poll_min:g}~{cfg.poll_max:g}초 랜덤, F5 방식={cfg.mode == 'reload'} / 456 제외 / 입석+좌석 제외 / 예약대기 포함")
    log("주의: Railway 첫 실행 시 네이버/CGV와 달리 코레일 검색 조건을 다시 설정해야 할 수 있음")
    log(f"자동 종료: {END_AT.isoformat()}")
    state = read_state(cfg.state_file)
    reader = BrowserReader()
    setup_errs = 0
    consecutive_block = 0
    consecutive_other = 0
    next_heartbeat = time.monotonic() + cfg.heartbeat_minutes * 60
    started = False
    completed = 0
    unknown_network_count = 0
    try:
        await reader.start()
        while datetime.now(KST) < END_AT:
            cycle_started = time.monotonic()
            try:
                if not started:
                    trains = await reader.initialize(datetime.now(KST))
                    started = True
                    log("브라우저 최초 검색 검증 성공")
                    await telegram.send("🔎 코레일 웹 감시 시작\n10/24 나주→서울·용산·수서 KTX 첫 화면\n일반실·특실의 즉시 예매/예약대기 알림 감시")
                else:
                    trains = await reader.refresh(datetime.now(KST), cfg.mode)
                consecutive_block = consecutive_other = 0
                setup_errs = 0
                completed += 1
                log(f"#{completed} 검증성공 KTX={len(trains)} 일정API요청={reader.calls_last_cycle} | {summarize(trains)}")
                if started and completed > 1:
                    if reader.calls_last_cycle == 0:
                        unknown_network_count += 1
                        if unknown_network_count in (3, 10):
                            log("주의: F5 시 시간표 API 요청 미관측 — 서버 최신 조회 보장 불가 (다른 경로 사용 가능)")
                    else:
                        unknown_network_count = 0
                events, closed = group_events(trains, state)
                state.update(closed)
                if events:
                    ok = await telegram.send(format_alert(events))
                    if ok:
                        for t, grade, status in events:
                            state[f"{t.key}:{grade}"] = status
                        log(f"텔레그램 감지 알림 전송 완료: {len(events)}개 상태")
                    else:
                        log("텔레그램 전송 실패 — 양성 상태는 저장하지 않아 재시도 가능")
                try:
                    save_state(cfg.state_file, state)
                except OSError as err:
                    log(f"상태 저장 오류: {err.__class__.__name__} (재배포 시 중복 알림 가능)")
                if cfg.heartbeat_minutes and time.monotonic() >= next_heartbeat:
                    await telegram.send(f"🟢 코레일 감시 정상\n{completed}회 확인 · 최근 KTX {len(trains)}편\n10/24 나주 출발 첫 화면 감시 중")
                    next_heartbeat = time.monotonic() + cfg.heartbeat_minutes * 60
                sleep_target = max(0, random.uniform(cfg.poll_min, cfg.poll_max) - (time.monotonic() - cycle_started))
            except AccessRestricted as err:
                consecutive_block += 1
                log(f"접근 제한 {consecutive_block}회: {str(err)[:180]}")
                if consecutive_block == 1:
                    await telegram.send("⚠️ 코레일 접근 제한/대기 화면 감지\n반복 조회를 중단하고 5분 대기합니다. 우회하지 않습니다.")
                if consecutive_block >= 3:
                    await telegram.send("🛑 코레일 접근 제한 3회 연속: 감시 자동 중단\nRailway Deploy Logs 확인 필요")
                    log("안전 종료: 접근 제한이 3회 연속 발생")
                    return 0
                sleep_target = min(600, 300 * consecutive_block)
            except SetupFailed as err:
                setup_errs += 1
                log(f"검색 조건 자동 설정 실패 {setup_errs}/3: {str(err)[:2800]}")
                log("진단: " + (await reader.small_diagnostics()))
                if setup_errs >= 3:
                    await telegram.send("🛑 코레일 감시 최초 설정 실패로 중단\nRailway 로그를 보내주세요. 날짜·역 선택 UI가 현재 코드와 다를 수 있습니다.")
                    return 0
                sleep_target = 90
            except (BrowserReadFailed, Exception) as err:
                if isinstance(err, (KeyboardInterrupt, SystemExit)):
                    raise
                consecutive_other += 1
                log(f"검색 결과 검증 실패 {consecutive_other}: {type(err).__name__} {str(err)[:220]}")
                log("진단: " + (await reader.small_diagnostics()))
                if consecutive_other == 3:
                    await telegram.send("⚠️ 코레일 감시 조회 실패 3회 연속\n예약 가능/매진을 판단하지 않고 대기 중입니다. Railway 로그 확인 필요.")
                    # Rebuild from scratch after three real failures, not rapid retries.
                    try:
                        await reader.stop()
                        reader = BrowserReader()
                        await reader.start()
                        started = False
                    except Exception as restart_err:
                        log(f"브라우저 재시작 실패: {type(restart_err).__name__}")
                sleep_target = min(900, 60 * (2 ** min(consecutive_other - 1, 4)))
            remaining = (END_AT - datetime.now(KST)).total_seconds()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, sleep_target))
        log("2026-10-25 00:00 KST 자동 종료")
        await telegram.send("⏹ 코레일 10/24 감시 종료\n설정된 날짜가 지나 자동 종료되었습니다.")
        return 0
    finally:
        await reader.stop()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except ValueError as exc:
        log(f"설정 오류: {exc}")
        sys.exit(2)
    except KeyboardInterrupt:
        log("사용자 종료")
        sys.exit(0)
