"""Playwright implementation of the Surface seam, frame-aware for legacy framesets."""

import hashlib
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page

from cua.redaction import DEFAULT_SENSITIVE_LABELS
from cua.surface.base import Element, FrameView, Resolved, Snapshot, SurfaceError, TargetNotFound
from cua.surface.targets import CellStrategy, CssStrategy, LabelStrategy, RoleStrategy, Strategy, Target

ACTION_TIMEOUT_MS = 5_000
SETTLE_TIMEOUT_S = 15.0
SETTLE_GRACE_S = 0.3   # give an action time to start its navigation/requests
SETTLE_QUIET_S = 0.5   # network must be quiet this long to count as settled
SETTLE_STALE_S = 2.0   # no network events at all for this long -> settled even if a request looks open

# Runs inside each frame. Tags every visible control and data cell with a
# data-cua-ref attribute and returns an accessibility-style description of it:
# role + accessible name, plus the visible label inferred from the adjacent
# table cell (legacy forms rarely use <label for>).
SNAPSHOT_JS = r"""
([prefix, sensitiveLabels]) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const stripColon = s => clean(s).replace(/:$/, '').trim();
  const visible = el => {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const isButtonInput = el => el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type);
  const roleOf = el => {
    const t = el.tagName.toLowerCase();
    if (el.getAttribute('role')) return el.getAttribute('role');
    if (t === 'a') return 'link';
    if (t === 'button' || isButtonInput(el)) return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'input' && el.type === 'checkbox') return 'checkbox';
    if (t === 'input' && el.type === 'radio') return 'radio';
    if (t === 'input' || t === 'textarea') return 'textbox';
    return t;
  };
  const nameOf = el => {
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    if (isButtonInput(el)) return clean(el.value);
    if (el.tagName === 'A' || el.tagName === 'BUTTON') return clean(el.innerText);
    if (el.getAttribute('title')) return clean(el.getAttribute('title'));
    return '';
  };
  const adjacentLabel = el => {
    if (el.labels && el.labels.length) return stripColon(el.labels[0].innerText);
    const td = el.closest('td,th');
    if (!td) return '';
    let prev = td.previousElementSibling;
    while (prev && !clean(prev.innerText)) prev = prev.previousElementSibling;
    return prev ? stripColon(prev.innerText) : '';
  };
  // First row is a column-header row only if it uses <th>, or it is a 3+ column
  // row with no digits in it. A 2-column key/value table has no column headers.
  const headerRow = table => {
    const first = table.rows[0];
    if (!first) return null;
    const cells = [...first.cells];
    if (cells.some(c => c.tagName === 'TH')) return first;
    if (cells.length >= 3 && cells.every(c => !/\d/.test(c.innerText))) return first;
    return null;
  };
  const cssPath = el => {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      let i = 1, s = el;
      while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      el = el.parentElement;
    }
    return 'html > ' + parts.join(' > ');
  };

  window.__cuaMarked = true;  // lets screenshot() tell a fresh, unmarked document apart
  document.querySelectorAll('[data-cua-ref]').forEach(e => e.removeAttribute('data-cua-ref'));
  document.querySelectorAll('[data-cua-sensitive]').forEach(e => e.removeAttribute('data-cua-sensitive'));

  const items = [];
  let n = 0;
  for (const el of document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea')) {
    if (!visible(el)) continue;
    const ref = prefix + (n++);
    el.setAttribute('data-cua-ref', ref);
    items.push({
      ref, kind: 'control', role: roleOf(el), name: nameOf(el), label: adjacentLabel(el),
      tag: el.tagName.toLowerCase(), input_type: el.type || '', attr_name: el.getAttribute('name') || '',
      form_action: el.form ? (el.form.getAttribute('action') || '') : '',
      form_method: el.form ? (el.form.getAttribute('method') || 'get').toLowerCase() : '',
      options: el.tagName === 'SELECT' ? [...el.options].map(o => clean(o.text)) : null,
      css_path: cssPath(el),
    });
  }
  for (const td of document.querySelectorAll('td, th')) {
    if (!visible(td) || td.querySelector('table, a, button, input, select, textarea')) continue;
    const text = clean(td.innerText);
    if (!text) continue;
    const cells = [...td.parentElement.cells];
    const idx = cells.indexOf(td);
    const table = td.closest('table');
    const hr = headerRow(table);
    const rowHeader = idx > 0 ? stripColon(cells[0].innerText) : '';
    const colHeader = hr && hr !== td.parentElement && hr.cells[idx] ? clean(hr.cells[idx].innerText) : '';
    const sensitive = sensitiveLabels.includes(rowHeader) || sensitiveLabels.includes(colHeader);
    const ref = prefix + (n++);
    td.setAttribute('data-cua-ref', ref);
    if (sensitive) td.setAttribute('data-cua-sensitive', '1');
    items.push({ref, kind: 'cell', role: 'cell', text, row_header: rowHeader, col_header: colHeader,
                tag: td.tagName.toLowerCase(), css_path: cssPath(td), sensitive});
  }
  return {title: document.title, url: location.pathname + location.search,
          text: document.body ? clean(document.body.innerText).slice(0, 3000) : '', items};
}
"""


def _xpath_literal(s: str) -> str:
    if '"' not in s:
        return f'"{s}"'
    if "'" not in s:
        return f"'{s}'"
    return "concat(" + ", '\"', ".join(f'"{part}"' for part in s.split('"')) + ")"


def _adjacent_label_xpath(label: str) -> str:
    t = _xpath_literal(label)
    return (f"//*[self::td or self::th][normalize-space(translate(., ':', ''))={t}]"
            "/following-sibling::td[1]//*[self::input[not(@type='hidden')] or self::select or self::textarea]")


def _cell_xpath(row_header: str, column_header: str | None) -> str:
    r = _xpath_literal(row_header)
    row = f"//tr[*[1][normalize-space(translate(., ':', ''))={r}]]"
    if column_header is None:
        return f"{row}/*[2]"
    c = _xpath_literal(column_header)
    header_index = f"count(ancestor::table[1]//tr[*[normalize-space()={c}]][1]/*[normalize-space()={c}][1]/preceding-sibling::*) + 1"
    return f"{row}/*[position() = {header_index}]"


class WebSurface:
    def __init__(self, page: Page, base_url: str, sensitive_labels: list[str] | None = None):
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.sensitive_labels = sensitive_labels or list(DEFAULT_SENSITIVE_LABELS)
        self._last: Snapshot | None = None
        self._inflight = 0
        self._last_net = time.monotonic()
        page.on("request", self._on_request_start)
        page.on("requestfinished", self._on_request_end)
        page.on("requestfailed", self._on_request_end)

    # ---- navigation -------------------------------------------------------

    def goto(self, route: str) -> None:
        self.page.goto(self.base_url + route)
        self._settle()

    def reload(self) -> None:
        self.page.reload()
        self._settle()

    def wait_ms(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    def _on_request_start(self, _request) -> None:
        self._inflight += 1
        self._last_net = time.monotonic()

    def _on_request_end(self, _request) -> None:
        self._inflight = max(0, self._inflight - 1)
        self._last_net = time.monotonic()

    def _settle(self) -> None:
        """Wait for the network to go quiet, then for every frame to finish loading.

        Playwright's wait_for_load_state("networkidle") returns at once when the page
        was already idle before the action, so it misses the frame navigation a click
        has only just started. Tracking requests ourselves closes that gap.
        """
        start = time.monotonic()
        deadline = start + SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(50)  # a polling tick; also lets Playwright deliver request events
            now = time.monotonic()
            quiet_for = now - self._last_net
            if now - start >= SETTLE_GRACE_S and quiet_for >= SETTLE_QUIET_S and (
                    self._inflight == 0 or quiet_for >= SETTLE_STALE_S):
                break
        for frame in self.page.frames:
            remaining_ms = max(0.0, deadline - time.monotonic()) * 1000
            try:
                frame.wait_for_load_state("load", timeout=remaining_ms)
            except PlaywrightError:
                pass  # still loading; the caller's next observation/checkpoint decides

    # ---- observation ------------------------------------------------------

    def observe(self) -> Snapshot:
        for attempt in range(3):
            try:
                return self._observe_once()
            except PlaywrightError:
                if attempt == 2:
                    raise
                self._settle()  # a frame navigated mid-snapshot; try again
        raise AssertionError("unreachable")

    def _observe_once(self) -> Snapshot:
        frames: list[FrameView] = []
        elements: dict[str, Element] = {}
        digest = hashlib.sha1()
        for idx, frame in enumerate(self.page.frames):
            data = frame.evaluate(SNAPSHOT_JS, [f"f{idx}-", self.sensitive_labels])
            if not data["items"] and not data["text"]:
                continue  # e.g. the <frameset> document itself
            name = frame.name or None
            frames.append(FrameView(name=name, url=data["url"], title=data["title"], text=data["text"]))
            digest.update(f"{name}|{data['url']}|{data['text']}".encode())
            for item in data["items"]:
                elements[item["ref"]] = Element(frame=name, **item)
        self._last = Snapshot(frames=frames, elements=elements, fingerprint=digest.hexdigest()[:12])
        return self._last

    # ---- targeting --------------------------------------------------------

    def _frame(self, name: str | None) -> Frame:
        if name is None:
            return self.page.main_frame
        frame = self.page.frame(name=name)
        if frame is None:
            raise SurfaceError(f"frame {name!r} not found")
        return frame

    def _element(self, ref: str) -> Element:
        if self._last is None or ref not in self._last.elements:
            raise SurfaceError(f"unknown ref {ref!r}; observe the screen again")
        return self._last.elements[ref]

    def handle_for_ref(self, ref: str) -> Locator:
        el = self._element(ref)
        return self._frame(el.frame).locator(f'[data-cua-ref="{ref}"]')

    def locator(self, frame: Frame, s: Strategy) -> Locator:
        match s:
            case RoleStrategy():
                return frame.get_by_role(s.role, name=s.name, exact=True)  # type: ignore[arg-type]
            case LabelStrategy():
                return frame.get_by_label(s.text, exact=True).or_(frame.locator(f"xpath={_adjacent_label_xpath(s.text)}"))
            case CellStrategy():
                return frame.locator(f"xpath={_cell_xpath(s.row_header, s.column_header)}")
            case CssStrategy():
                return frame.locator(s.value)
        raise TypeError(f"unsupported strategy {s!r}")

    def describe(self, ref: str) -> Target:
        """Record time: build every candidate strategy for a live element and keep
        only those that resolve uniquely back to that same element."""
        el = self._element(ref)
        frame = self._frame(el.frame)
        candidates: list[Strategy] = []
        if el.kind == "control":
            if el.name:
                candidates.append(RoleStrategy(role=el.role, name=el.name))
            if el.label and el.role in ("textbox", "combobox", "checkbox", "radio"):
                candidates.append(LabelStrategy(text=el.label))
            scope = f'form[action="{el.form_action}"] ' if el.form_action else ""
            if el.attr_name:
                candidates.append(CssStrategy(value=f'{scope}{el.tag}[name="{el.attr_name}"]'))
            elif el.input_type == "submit" and scope:
                candidates.append(CssStrategy(value=f'{scope}input[type="submit"]'))
        elif el.row_header:
            # Never address a cell by its own text: that is data and changes per run.
            candidates.append(CellStrategy(row_header=el.row_header, column_header=el.col_header or None))
        candidates.append(CssStrategy(value=el.css_path))

        valid = [s for s in candidates if self._resolves_to(frame, s, ref)]
        description = f'{el.role} "{el.name or el.label or (el.row_header + " / " + el.col_header).strip(" /")}"'
        return Target(frame=el.frame, strategies=valid or [CssStrategy(value=el.css_path)], description=description)

    def _resolves_to(self, frame: Frame, s: Strategy, ref: str) -> bool:
        loc = self.locator(frame, s)
        try:
            return loc.count() == 1 and loc.get_attribute("data-cua-ref", timeout=2_000) == ref
        except PlaywrightError:
            return False

    def resolve(self, target: Target, timeout_s: float = 0) -> Resolved:
        """Replay time: first strategy that matches exactly one element wins.
        Polls until timeout_s so slow pages are waited out, not slept through."""
        deadline = time.monotonic() + timeout_s
        while True:
            attempts: list[str] = []
            try:
                frame = self._frame(target.frame)
                for i, s in enumerate(target.strategies):
                    loc = self.locator(frame, s)
                    count = loc.count()
                    if count == 1:
                        return Resolved(handle=loc, strategy_index=i, strategy=s)
                    attempts.append(f"{s.by}: {count} matches")
            except (SurfaceError, PlaywrightError) as e:
                attempts.append(str(e).splitlines()[0])
            if time.monotonic() >= deadline:
                raise TargetNotFound(target, attempts)
            self.page.wait_for_timeout(250)

    # ---- actions ----------------------------------------------------------

    def click(self, handle: Locator) -> None:
        try:
            handle.click(timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError as e:
            raise SurfaceError(f"click failed: {str(e).splitlines()[0]}") from e
        self._settle()

    def fill(self, handle: Locator, text: str) -> None:
        try:
            handle.fill(text, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError as e:
            raise SurfaceError(f"type failed: {str(e).splitlines()[0]}") from e

    def select(self, handle: Locator, option: str) -> None:
        try:
            handle.select_option(label=option, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError as e:
            raise SurfaceError(f"select failed: {str(e).splitlines()[0]}") from e

    def read(self, handle: Locator) -> str:
        try:
            return handle.inner_text(timeout=ACTION_TIMEOUT_MS).strip()
        except PlaywrightError as e:
            raise SurfaceError(f"read failed: {str(e).splitlines()[0]}") from e

    def _is_marked(self, frame: Frame) -> bool:
        try:
            return frame.evaluate("() => window.__cuaMarked === true")
        except PlaywrightError:
            return False

    def screenshot(self, path: Path) -> None:
        """Screenshot with every sensitive cell blacked out. Fails closed: if any
        frame holds a document we have not scanned yet, scan it before capturing."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if not all(self._is_marked(f) for f in self.page.frames):
            self.observe()
        masks = [f.locator("[data-cua-sensitive]") for f in self.page.frames]
        self.page.screenshot(path=str(path), mask=masks, mask_color="#000000")
