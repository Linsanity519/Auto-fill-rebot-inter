"""Bounded read requests inside the already authenticated platform page."""
from __future__ import annotations

READ_BATCH = """
async ([api, ids, requestMs, budgetMs]) => {
  const deadline = Date.now() + budgetMs;
  const out = {};
  for (const id of ids) {
    if (Date.now() >= deadline) { out[id] = 'ERR preflight deadline'; continue; }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(),
      Math.max(1, Math.min(requestMs, deadline - Date.now())));
    try {
      const res = await fetch(api + encodeURIComponent(id),
        {credentials: 'include', signal: controller.signal});
      out[id] = res.ok ? await res.text() : 'ERR HTTP ' + res.status;
    } catch (_) { out[id] = 'ERR request failed or timed out'; }
    finally { clearTimeout(timer); }
  }
  return out;
}
"""


def read_status(page, api: str, ids: list[str], timeout: int) -> dict:
    return page.evaluate(READ_BATCH, [api, ids, min(timeout, 15000), 60000])
