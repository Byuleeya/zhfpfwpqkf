"""Read-only Playwright navigation and DOM snapshots for the official KORAIL site.

Never bypasses CAPTCHAs, NetFunnel, authentication, or access restrictions.
"""
from __future__ import annotations
import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright, TimeoutError as PWTimeout
from parse_trains import InvalidPage, parse_scan, Train
from general_form import GeneralForm, SetupUIError
from datetime import datetime

BASE = "https://www.korail.com"
GENERAL = BASE + "/ticket/search/general"
LIST = BASE + "/ticket/search/list"

DOM_SNAPSHOT_JS = """() => {
  const value = s => document.querySelector(s)?.value || '';
  const checked = s => Boolean(document.querySelector(s)?.checked);
  const filter = document.querySelector('#btn_tab_info2');
  return {
    meta: {date: value('#startDate'), origin: value('#labelstart'),
      destination: value('#labelend'), passengers: value('#labelple'),
      nearby_stations: checked('#adjStnScdlOfrFlg'),
      combined_destinations: checked('#adjStnScdlOfrFlg2'),
      ktx_filter: Boolean(filter?.classList.contains('active'))},
    rows: Array.from(document.querySelectorAll('li.tckList'), r => r.outerHTML),
    title: document.title,
    visibleText: (document.body?.innerText || '').slice(0, 450)
  };
}"""

class AccessRestricted(RuntimeError):
    pass

class SetupFailed(RuntimeError):
    pass

class BrowserReadFailed(RuntimeError):
    pass


@dataclass
class BrowserReader:
    headless: bool = True
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    playwright: Playwright | None = None
    calls_total: int = 0
    calls_last_cycle: int = 0
    blocked_response: bool = False

    async def start(self) -> None:
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        self.context = await self.browser.new_context(
            locale="ko-KR", timezone_id="Asia/Seoul", viewport={"width": 1440, "height": 970})
        self.page = await self.context.new_page()
        self.page.set_default_timeout(9000)
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)

    def _on_request(self, request: Any) -> None:
        url = request.url
        if ("/ebizweb/global/trains/schedule" in url or
                "/classes/com.korail.mobile.seatMovie.ScheduleView" in url):
            self.calls_total += 1
            self.calls_last_cycle += 1

    def _on_response(self, response: Any) -> None:
        if response.status in (403, 429) and "korail.com" in response.url:
            self.blocked_response = True

    async def stop(self) -> None:
        if self.browser is not None:
            await self.browser.close()
        if self.playwright is not None:
            await self.playwright.stop()
        self.page = self.context = self.browser = self.playwright = None

    async def _check_block(self) -> None:
        assert self.page
        if self.blocked_response:
            raise AccessRestricted("Korail HTTP 403/429 응답 감지")
        if await self.page.locator("#cdn-error-overlay.show").count():
            raise AccessRestricted("코레일 서비스 이용 안내/리소스 제한 화면")
        # Only *visible* challenge / queue text, not hidden CSS/source text.
        text = (await self.page.locator("body").inner_text(timeout=3500))[:1500]
        if any(marker in text for marker in (
            "자동화된 접근", "비정상적인 접근", "접속이 제한", "CAPTCHA", "캡차", "로봇이 아닙니다"
        )):
            raise AccessRestricted("접근 제한/인증 화면")
        for selector in ("[data-nf-vwr-page]", ".netfunnel_waiting", "#NetFunnel_Skin_Top"):
            loc = self.page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                raise AccessRestricted("NetFunnel 대기 화면: 우회 없이 대기 필요")

    async def _snapshot(self) -> dict[str, Any]:
        assert self.page
        await self._check_block()
        return await self.page.evaluate(DOM_SNAPSHOT_JS)

    async def _ready(self, now: datetime) -> list[Train] | None:
        assert self.page
        try:
            await self.page.locator("li.tckList").first.wait_for(timeout=14000)
            await self.page.wait_for_timeout(650)
            data = await self._snapshot()
            return parse_scan(data["meta"], data["rows"], now)
        except (InvalidPage, PWTimeout):
            await self._check_block()
            return None

    async def _select_ktx(self) -> None:
        assert self.page
        li = self.page.locator("#btn_tab_info2")
        try:
            await li.wait_for(state="attached", timeout=13000)
        except PWTimeout as exc:
            raise SetupFailed("KTX 필터 버튼 없음 (결과 화면이 완전히 로딩되지 않았을 수 있음)") from exc
        if "active" not in ((await li.get_attribute("class")) or "").split():
            await li.locator("button").click()
            await self.page.wait_for_timeout(550)
        if "active" not in ((await li.get_attribute("class")) or "").split():
            raise SetupFailed("KTX 필터 선택 실패")

    async def setup_search(self, now: datetime) -> list[Train]:
        assert self.page
        self.calls_last_cycle = 0
        self.blocked_response = False
        response = await self.page.goto(GENERAL, wait_until="domcontentloaded", timeout=30000)
        if response and response.status in (403, 429):
            raise AccessRestricted(f"최초 검색 화면 HTTP {response.status}")
        await self.page.wait_for_timeout(1000)
        await self._check_block()
        form = GeneralForm(self.page)
        try:
            await form.configure()
        except SetupUIError as exc:
            await self._check_block()
            diag = await form.popup_diagnostics()
            raise SetupFailed(f"{form.step}: {exc}; UI={diag}") from exc
        await self._check_block()
        await self._select_ktx()
        trains = await self._ready(now)
        if trains is None:
            snap = await self._snapshot()
            try:
                parse_scan(snap["meta"], snap["rows"], now)
            except InvalidPage as exc:
                raise SetupFailed(f"열차 조회 뒤 결과 검증 실패: {exc}") from exc
            raise SetupFailed("열차 조회 뒤 결과 목록 로딩에 실패")
        return trains

    async def initialize(self, now: datetime) -> list[Train]:
        # A Railway container begins with a fresh browser. The result URL does
        # not encode 10/24, Naju, the two flags, or KTX; configure the official
        # general form once, instead of wasting one failed /list navigation.
        return await self.setup_search(now)

    async def refresh(self, now: datetime, mode: str = "reload") -> list[Train]:
        assert self.page
        self.calls_last_cycle = 0
        self.blocked_response = False
        if mode == "button":
            lookup = self.page.locator(".ticketSrchWrap button.btn_bn-blue").filter(has_text=re.compile(r"열차\s*조회")).first
            if await lookup.count() == 0:
                raise BrowserReadFailed("열차조회 버튼 없음")
            await lookup.click()
            await self.page.wait_for_timeout(1200)
        else:
            resp = await self.page.reload(wait_until="domcontentloaded", timeout=30000)
            if resp is not None and resp.status in (403, 429):
                raise AccessRestricted(f"메인 페이지 HTTP {resp.status}")
            await self.page.wait_for_timeout(1400)
        await self._check_block()
        if await self.page.locator("#btn_tab_info2").count():
            await self._select_ktx()
        result = await self._ready(now)
        if result is None:
            snapshot = await self._snapshot()
            try:
                parse_scan(snapshot["meta"], snapshot["rows"], now)
            except InvalidPage as exc:
                raise BrowserReadFailed(str(exc)) from exc
            raise BrowserReadFailed("열차 목록 확인 실패")
        return result

    async def small_diagnostics(self) -> str:
        assert self.page
        try:
            snap = await self._snapshot()
            text = f"URL={self.page.url} title={snap['title'][:65]!r} meta={snap['meta']} rows={len(snap['rows'])}"
            if "/search/general" in self.page.url:
                text += " UI=" + await GeneralForm(self.page).popup_diagnostics()
            return text
        except Exception as e:
            return f"URL={self.page.url} diagnostics={type(e).__name__}:{str(e)[:120]}"
