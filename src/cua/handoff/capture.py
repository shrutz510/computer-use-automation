"""Recording what the human does in the live session.

A listener script is injected into every frame of the automation's browser context; it reports
clicks and field changes through an exposed binding. The same DOM events fire for the
automation's own actions, so SessionControl keeps only those that happen while a human holds
control. Password fields and fields with a sensitive label never report their value.
"""

from playwright.sync_api import BrowserContext
from playwright.sync_api import Error as PlaywrightError

from cua.handoff.control import SessionControl
from cua.redaction import REDACTED, Redactor

LISTENER_JS = r"""
(() => {
  if (window.__cuaListening) return;
  window.__cuaListening = true;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const label = el => {
    const td = el.closest && el.closest('td,th');
    if (!td) return '';
    let prev = td.previousElementSibling;
    while (prev && !clean(prev.innerText)) prev = prev.previousElementSibling;
    return prev ? clean(prev.innerText).replace(/:$/, '') : '';
  };
  const describe = el => ({
    tag: el.tagName.toLowerCase(), input_type: el.type || '',
    name: clean(el.getAttribute('aria-label')
      || (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type) ? el.value : '')
      || ((el.tagName === 'A' || el.tagName === 'BUTTON') ? el.innerText : '')),
    label: label(el), frame: window.name || null, url: location.pathname,
  });
  const report = payload => { try { window.__cuaHuman && window.__cuaHuman(payload); } catch (e) {} };
  document.addEventListener('click', e => {
    const el = (e.target.closest && e.target.closest('a,button,input,select,textarea')) || e.target;
    report({kind: 'click', ...describe(el)});
  }, true);
  document.addEventListener('change', e => {
    const el = e.target;
    const value = el.type === 'password' ? null
      : (el.tagName === 'SELECT' ? (el.options[el.selectedIndex] || {}).text : el.value);
    report({kind: 'change', ...describe(el), value});
  }, true);
})();
"""


def install_human_capture(context: BrowserContext, control: SessionControl, sensitive_labels: list[str],
                          redactor: Redactor) -> None:
    def on_event(_source, payload: dict) -> None:
        if payload.get("kind") == "change":
            if payload.get("value") is None or payload.get("label") in sensitive_labels:
                payload["value"] = REDACTED
            else:
                payload["value"] = redactor.text(str(payload["value"]))
        control.record_human_action(payload)

    context.expose_binding("__cuaHuman", on_event)
    context.add_init_script(LISTENER_JS)
    for page in context.pages:  # documents that loaded before the init script was registered
        for frame in page.frames:
            try:
                frame.evaluate(LISTENER_JS)
            except PlaywrightError:
                pass
