"""Official KORAIL /ticket/search/general form navigator.

Based on user-provided rendered HTML dated 2026-10-17. Station and calendar
popovers are React-rendered *after* click, so any unsupported popup must fail
closed with useful diagnostics; do not guess values or bypass restrictions.
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup
from parse_trains import ORIGIN_STATION
from playwright.async_api import Page, TimeoutError as PWTimeout

TARGET_DATE = "2026-10-24"
TARGET_TIME = "00:00"
GENERAL_FIELD = {
    "origin": 'input[name="txtGoStart"]',
    "destination": 'input[name="txtGoEnd"]',
    "date": "#startDate",
    "passengers": '.tab-tck.type_by a[title="인원"]',
}
POPUP_ROOTS = (
    '.type_station_lookup-pop:visible', '.type_tranin-station-pop_wrap:visible',
    '.layerPopup:visible', '.layerWrap:visible', '[role="dialog"]:visible',
    '.popup:visible', '.popUp:visible', '.ReactModal__Content:visible'
)
CALENDAR_ROOTS = (
    '[role="dialog"]:visible', '.layerPopup:visible', '.layerWrap:visible',
    '.calendar:visible', '[class*="calendar"]:visible',
    '.datepicker:visible', '[class*="datePicker"]:visible',
    '[class*="date-picker"]:visible', '.popUp:visible',
    '.ReactModal__Content:visible', '.ReactModal__Overlay:visible'
)


class SetupUIError(RuntimeError):
    pass


def general_form_from_saved_html(html: str) -> dict[str, Any]:
    """Offline snapshot of general-form structure; never makes a live request."""
    soup = BeautifulSoup(html, "lxml")
    for n in soup(["script", "style"]):
        n.decompose()
    def val(selector: str) -> str:
        el = soup.select_one(selector)
        return str(el.get("value", "") if el else "")
    def selected_radio(name: str) -> str:
        el = soup.select_one(f'input[name="{name}"][checked]')
        return str(el.get("value") if el else "")
    return {
        "origin": val(GENERAL_FIELD["origin"]),
        "destination": val(GENERAL_FIELD["destination"]),
        "date": val(GENERAL_FIELD["date"]),
        "passengers": soup.select_one(GENERAL_FIELD["passengers"]).get_text(" ", strip=True)
            if soup.select_one(GENERAL_FIELD["passengers"]) else "",
        "nearby": selected_radio("adjStnScdlOfrFlg"),
        "combined": selected_radio("adjStnScdlOfrFlg2"),
        "station_openers": len(soup.select('.tab-tck.type_by .ticketSrchWrap .btn_pop-open')),
        "date_opener": bool(soup.select_one('a[title="출발일"]')),
        "lookup": bool(soup.select_one('.tab-tck.type_by button.btn_lookup')),
    }


class GeneralForm:
    def __init__(self, page: Page):
        self.page = page
        self.step = "초기"

    async def _value(self, selector: str) -> str:
        return (await self.page.locator(selector).first.input_value()).strip()

    async def _wait(self, milliseconds: int = 400) -> None:
        await self.page.wait_for_timeout(milliseconds)

    async def _visible_roots(self, selectors: tuple[str, ...]) -> list[Any]:
        result = []
        for sel in selectors:
            els = self.page.locator(sel)
            for i in range(min(await els.count(), 5)):
                el = els.nth(i)
                if await el.is_visible():
                    result.append(el)
        return result

    async def _station(self, *, origin: bool, station: str) -> None:
        field = GENERAL_FIELD["origin"] if origin else GENERAL_FIELD["destination"]
        self.step = f"{'출발역' if origin else '도착역'} {station} 선택"
        if await self._value(field) == station:
            return
        cls = "start" if origin else "end"
        opener = self.page.locator(f'.tab-tck.type_by .ticketSrchWrap .{cls} a.btn_pop-open').first
        if not await opener.count():
            raise SetupUIError(f"{self.step}: 열기 버튼 없음")
        await opener.click()
        await self._wait(500)

        # Choose only a visible, editable station-search field. Never input into
        # password, hidden, readonly or unrelated account fields.
        selectors = (
            '.type_station_lookup-pop input[type="text"]:visible:not([readonly])',
            '.layerPopup:visible input[type="text"]:visible:not([readonly])',
            '.layerWrap:visible input[type="text"]:visible:not([readonly])',
            'input[placeholder*="역명"]:visible:not([readonly])',
            'input[placeholder*="역"]:visible:not([readonly])',
            'input[placeholder*="검색"]:visible:not([readonly])',
            'input[id="schStr"]:visible:not([readonly])',
        )
        used = None
        for sel in selectors:
            loc = self.page.locator(sel)
            if await loc.count():
                used = loc.last
                break
        if used:
            await used.fill(station)
            await self._wait(650)

        # The list may be a button or a span inside a clickable list item.
        # Restrict to the newly opened popup before trying a document-wide
        # exact-text candidate; always verify the actual form value afterwards.
        roots = await self._visible_roots(POPUP_ROOTS)
        roots.append(self.page.locator("body"))
        for root in roots:
            matches = root.get_by_text(station, exact=True)
            for idx in range(min(await matches.count(), 25)):
                candidate = matches.nth(idx)
                try:
                    if not await candidate.is_visible():
                        continue
                    # Don't click text inside the unchanged search form itself.
                    is_form = await candidate.evaluate(
                        "e => !!e.closest('.tab-tck.type_by .ticketSrchWrap')")
                    if is_form:
                        continue
                    await candidate.click(timeout=1200)
                    await self._wait(450)
                    if await self._value(field) == station:
                        return
                except Exception:
                    continue
        raise SetupUIError(
            f"{self.step}: 팝업에서 역 선택 실패 (클릭 후 열린 역 선택창 HTML 필요)")

    async def _popup_day_candidates(self) -> list[dict[str, str | int]]:
        """Inspect only calendar candidates, never change React state directly."""
        return await self.page.evaluate(r"""() => {
          const modal = [...document.querySelectorAll('.ReactModal__Content, [role="dialog"], .layerPopup, .calendar, [class*="calendar"]')];
          const visible = e => { const r=e.getBoundingClientRect(),s=getComputedStyle(e);
            return r.width>2&&r.height>2&&s.visibility!=='hidden'&&s.display!=='none'; };
          const candidates = [];
          for (const root of modal.filter(visible)) {
            const list = root.querySelectorAll('button, a, td, abbr, [role="button"], [data-date], [aria-label], [title], span');
            for (const el of list) {
              if (!visible(el)) continue;
              const t=(el.innerText||el.textContent||'').trim().replace(/\s+/g,' ').slice(0,90);
              const a=(el.getAttribute('aria-label')||'')+' '+(el.getAttribute('title')||'')+' '+
                (el.getAttribute('datetime')||'')+' '+(el.getAttribute('data-date')||'');
              if (!(/^24(?:일)?$/.test(t) || /2026.{0,12}10.{0,12}24|2026-10-24|20261024/.test(a))) continue;
              if (el.matches('span') && el.querySelector('button,a,td')) continue;
              const closest=el.closest('button,a,[role="button"],td')||el;
              if (!visible(closest)) continue;
              if (closest.matches('[disabled],[aria-disabled="true"]')) continue;
              const p=closest.parentElement;
              candidates.push({text:t,attr:a.slice(0,100),html:closest.outerHTML.slice(0,240),
                outerText:(p?.innerText||'').slice(0,90)});
              if (candidates.length>=24) return candidates;
            }
          }
          return candidates;
        }""")

    async def _site_calendar(self):
        """Return the opened genuine Korail date picker, or None for other UIs."""
        modal = self.page.locator('.ReactModal__Content:visible .type_date-pop_wrap').first
        if not await modal.count():
            return None
        return modal

    async def _select_day(self) -> bool:
        """Choose Oct 24 from the 2026.10 calendar, never a hidden Nov/Dec 24."""
        site = await self._site_calendar()
        if site is not None:
            month_nodes = site.locator('.datepk_wrap .slick-slide .datepicker')
            candidates = []
            for i in range(await month_nodes.count()):
                calendar = month_nodes.nth(i)
                label = (await calendar.locator('p.date').first.inner_text()).strip()
                if re.fullmatch(r'2026\.\s*10\.?', label):
                    candidates.append(calendar)
            if len(candidates) != 1:
                raise SetupUIError(f'달력의 2026년 10월 찾기 실패: 후보={len(candidates)}')
            month = candidates[0]
            # Real HTML: <td><a aria-disabled="false"><span class="day">24</span></a></td>
            # November and December also contain a 24 but are explicitly disabled.
            anchors = month.locator(
                'td:not(.disabled) a[aria-disabled="false"]:has(> span.day:text-is("24"))'
            )
            if await anchors.count() != 1:
                raise SetupUIError(f'10월 24일 활성 날짜 링크 개수={await anchors.count()}')
            await anchors.first.click(timeout=4000)
            await self._wait(220)
            return True

        # Retain a small, safe fallback for date pickers other than the captured one.
        # Do not guess when more than one 24-day control is visible.
        controls = []
        for root in await self._visible_roots(CALENDAR_ROOTS):
            for label in ('24', '24일'):
                loc = root.get_by_role('button', name=label, exact=True)
                for i in range(min(await loc.count(), 10)):
                    candidate = loc.nth(i)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        bb = await candidate.bounding_box()
                        if bb:
                            controls.append((tuple(round(bb[k]) for k in ('x','y','width','height')),candidate))
        unique = dict(controls)
        if len(unique) == 1:
            await next(iter(unique.values())).click(timeout=1600)
            await self._wait(260)
            return True
        return False

    async def _set_zero_time(self) -> bool:
        """Select the actual 00시 slider item; does not manipulate React internals."""
        site = await self._site_calendar()
        if site is not None:
            slider = site.locator('.timeSelect .slideWrap').first
            if await slider.count() != 1:
                raise SetupUIError('달력 시간선택 .timeSelect .slideWrap 없음')
            # The slider can open scrolled to a later hour. Use its own Previous
            # control to bring 00시 into view, rather than clicking a hidden slide.
            for _ in range(25):
                current = slider.locator('.slick-slide.slick-active a[aria-disabled="false"]').filter(
                    has_text=re.compile(r'^00시$')
                )
                if await current.count():
                    item = current.first
                    if await item.is_visible():
                        if await item.get_attribute('title') != '선택한 시간':
                            await item.click(timeout=3000)
                            await self._wait(180)
                        return True
                prev = slider.locator('button.slick-prev').first
                if not await prev.count() or (await prev.get_attribute('aria-disabled')) == 'true':
                    break
                try:
                    await prev.click(timeout=1800)
                    await self._wait(120)
                except Exception:
                    break
            raise SetupUIError('00시 항목을 시간 슬라이더에서 선택할 수 없음')

        if TARGET_TIME in await self._value(GENERAL_FIELD['date']):
            return True
        for root in await self._visible_roots(CALENDAR_ROOTS):
            for phrase in ('00:00','0:00','00시','0시'):
                loc = root.get_by_role('button', name=phrase, exact=True)
                for i in range(min(await loc.count(), 6)):
                    el = loc.nth(i)
                    try:
                        if await el.is_visible() and await el.is_enabled():
                            await el.click(timeout=1400)
                            await self._wait(150)
                            return True
                    except Exception:
                        pass
        return TARGET_TIME in await self._value(GENERAL_FIELD['date'])

    async def _confirm_calendar(self) -> bool:
        """The real date modal commits its day/hour state on the '적용' button."""
        site = await self._site_calendar()
        if site is not None:
            apply = site.locator('.type_date-pop .btn_wrap button.btn_bn-blue').filter(
                has_text=re.compile(r'^적용$')
            )
            if await apply.count() != 1:
                raise SetupUIError(f'달력 적용 버튼 개수={await apply.count()}')
            await apply.click(timeout=3500)
            await self._wait(480)
            return TARGET_DATE in await self._value(GENERAL_FIELD['date'])
        for root in await self._visible_roots(CALENDAR_ROOTS):
            for label in ('적용', '확인', '완료'):
                btns = root.get_by_role('button', name=label, exact=True)
                for i in range(min(await btns.count(), 3)):
                    b = btns.nth(i)
                    try:
                        if await b.is_visible() and await b.is_enabled():
                            await b.click(timeout=1400)
                            await self._wait(270)
                            if TARGET_DATE in await self._value(GENERAL_FIELD['date']):
                                return True
                    except Exception:
                        pass
        return TARGET_DATE in await self._value(GENERAL_FIELD['date'])

    async def _date(self) -> None:
        self.step = '출발일 10월 24일 00:00 선택'
        current = await self._value(GENERAL_FIELD['date'])
        if TARGET_DATE in current and TARGET_TIME in current:
            return
        opener = self.page.locator('.tab-tck.type_by a[title="출발일"]').first
        if not await opener.count():
            raise SetupUIError('출발일 달력 열기 버튼 없음')
        await opener.click()
        await self._wait(600)
        if not await self._select_day():
            await self._wait(400)
            if not await self._select_day():
                raise SetupUIError(f'2026-10-24 날짜 선택 실패; 후보={await self._popup_day_candidates()}')
        if not await self._set_zero_time():
            raise SetupUIError('00시 선택 실패')
        if not await self._confirm_calendar():
            raise SetupUIError(f'달력 적용 후 날짜 미반영: {await self._value(GENERAL_FIELD["date"])}')
        actual = await self._value(GENERAL_FIELD['date'])
        if TARGET_DATE not in actual or TARGET_TIME not in actual:
            raise SetupUIError(f'날짜·시간 불일치: {actual!r} (요구 2026-10-24 00:00)')
        self.step = '출발일 검증'

    async def _radio(self, name: str) -> None:
        self.step = f"옵션 {name}=Y"
        button = self.page.locator('.tab-tck.type_by .optionWrap button.btnToggle').first
        panel = self.page.locator('.tab-tck.type_by .optionWrap .selectAreaWrap.inline').first
        if not await panel.count():
            raise SetupUIError("검색 옵션 패널 없음")
        if not await panel.is_visible():
            await button.click()
            await self._wait(200)
        loc = self.page.locator(f'input[name="{name}"][value="Y"]')
        if await loc.count() != 1:
            raise SetupUIError(f"검색 옵션 {name} 포함 라디오 없음")
        if not await loc.is_checked():
            label = self.page.locator(f'label[for="{await loc.get_attribute("id")}"]')
            if await label.count():
                await label.click()
            else:
                await loc.check()
        if not await loc.is_checked():
            raise SetupUIError(f"검색 옵션 {name} 적용 실패")

    async def configure(self) -> None:
        self.step = "첫 검색 화면 로딩"
        try:
            await self.page.locator(GENERAL_FIELD["origin"]).wait_for(
                state="visible", timeout=14000)
        except PWTimeout as exc:
            raise SetupUIError("일반 검색 화면의 txtGoStart 필드가 로딩되지 않음") from exc
        await self._station(origin=True, station=ORIGIN_STATION)
        await self._station(origin=False, station="서울")
        await self._date()
        for radio_name in ("adjStnScdlOfrFlg", "adjStnScdlOfrFlg2"):
            await self._radio(radio_name)

        self.step = "검색 조건 최종 검증"
        for field, expected in (("origin", ORIGIN_STATION), ("destination", "서울")):
            value = await self._value(GENERAL_FIELD[field])
            if value != expected:
                raise SetupUIError(f"{field} 불일치: {value}")
        date_value = await self._value(GENERAL_FIELD["date"])
        if TARGET_DATE not in date_value or TARGET_TIME not in date_value:
            raise SetupUIError(f"날짜/출발시각 불일치: {date_value}")
        people = await self.page.locator(GENERAL_FIELD["passengers"]).first.inner_text()
        if not re.fullmatch(r"\s*총\s*1\s*명\s*", people):
            raise SetupUIError(f"인원 1명 아님: {people[:70]}")
        for name in ("adjStnScdlOfrFlg", "adjStnScdlOfrFlg2"):
            if not await self.page.locator(f'input[name="{name}"][value="Y"]').is_checked():
                raise SetupUIError(f"검색 옵션 {name} 검증 실패")

        self.step = "열차 조회 버튼 누르기"
        lookup = self.page.locator('.tab-tck.type_by button.btn_lookup').first
        if await lookup.count() != 1:
            raise SetupUIError("최초 검색 화면의 열차 조회 버튼 없음")
        await lookup.click()
        try:
            await self.page.wait_for_url('**/ticket/search/list', timeout=20000)
        except PWTimeout as exc:
            raise SetupUIError(f"열차 조회 후 결과 페이지 이동 실패, 현재 URL={self.page.url}") from exc

    async def popup_diagnostics(self) -> str:
        """Log non-sensitive popup DOM metadata, never cookies or tokens."""
        try:
            result = await self.page.evaluate(r"""() => {
              const vis=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                return r.width>1 && r.height>1 && s.visibility!=='hidden' && s.display!=='none';};
              const inputs=[...document.querySelectorAll('.tab-tck.type_by input')]
                .filter(e=>e.type!=='password').map(e=>({name:e.name,id:e.id,
                   value:(e.value||'').slice(0,36)})).slice(0,12);
              const mods=[...document.querySelectorAll('.ReactModal__Content,[role="dialog"],.layerPopup,.layerWrap,[class*="calendar"],iframe')]
                .filter(vis).slice(-12).map(e=>({tag:e.tagName,
                  cls:String(e.className||'').slice(0,95),
                  html:e.outerHTML.slice(0,650).replace(/<script[^>]*>[\s\S]*?<\/script>/g,'[script removed]'),
                  text:(e.innerText||'').trim().slice(0,180)}));
              const actions=[...document.querySelectorAll('.ReactModal__Content button,.ReactModal__Content a,.ReactModal__Content [role=button]')]
                .filter(vis).slice(0,35).map(e=>({text:(e.innerText||e.textContent||'').trim().slice(0,45),
                    title:(e.title||'').slice(0,70),aria:(e.getAttribute('aria-label')||'').slice(0,70),
                    cls:String(e.className||'').slice(0,60)}));
              return {inputs,mods,actions,frameCount:document.querySelectorAll('iframe').length};
            }""")
            return str(result)[:3500]
        except Exception as exc:
            return f'팝업진단불가:{type(exc).__name__}'
