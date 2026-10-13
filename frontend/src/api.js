// Thin client for the DataTraceMind API, plus the polling hook the pages use.
import { useCallback, useEffect, useRef, useState } from "react";

const ACCESS_CODE_KEY = "dtm-access-code";

export function getAccessCode() {
  try {
    return window.localStorage.getItem(ACCESS_CODE_KEY) || "";
  } catch {
    return "";
  }
}

export function setAccessCode(code) {
  try {
    if (code) window.localStorage.setItem(ACCESS_CODE_KEY, code);
    else window.localStorage.removeItem(ACCESS_CODE_KEY);
  } catch {
    // Storage can be unavailable (private window); the code then lasts for this page only.
  }
}

export async function api(path, { method = "GET", body, timeoutMs } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const code = getAccessCode();
  if (code) headers["X-Demo-Access-Code"] = code;
  const response = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: timeoutMs ? AbortSignal.timeout(timeoutMs) : undefined,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // A response without a JSON body; the status code carries the outcome.
  }
  if (!response.ok) {
    const detail = payload?.detail;
    const message = Array.isArray(detail)
      ? detail.map((d) => `${d.loc?.slice(1).join(".")}: ${d.msg}`).join("; ")
      : detail || `${response.status} ${response.statusText}`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return payload;
}

// A poll left unanswered this long is given up, so that the next one can be sent.
const POLL_TIMEOUT_MS = 30000;

// The last answer for each path, so a page that is opened again shows what it
// last knew while it loads the current state.
const lastAnswer = new Map();

// Keep `path` fresh. The next request goes out `interval` ms after the last one
// was answered, so a slow server is never asked twice at once, and nothing is
// sent while the tab is hidden. `interval` may be a function of the last answer
// (null before the first); when it is 0 or null the answer will not change by
// itself, and it is fetched once, on `refresh()`, and when the tab is shown again.
// The last good data is kept while a refresh is in flight or failing, so the
// page never flashes empty.
export function usePoll(path, interval = 5000) {
  const [answer, setAnswer] = useState({ path: null, data: null, error: null });
  const latestInterval = useRef(interval);
  latestInterval.current = interval;
  const load = useRef(null);
  // A new fixed interval restarts the poll; a function is read each time it is needed.
  const fixedInterval = typeof interval === "function" ? null : interval;

  useEffect(() => {
    if (!path) return undefined;
    let stopped = false;
    let timer = null;
    let sent = 0;
    let shown = 0;
    let pending = 0;
    let known = lastAnswer.get(path) ?? null;

    const schedule = () => {
      const every = typeof latestInterval.current === "function" ? latestInterval.current(known) : latestInterval.current;
      if (every && !document.hidden) timer = window.setTimeout(fetchNow, every);
    };
    const fetchNow = async () => {
      window.clearTimeout(timer);
      const id = ++sent;
      pending += 1;
      let data = null;
      let error = null;
      try {
        data = await api(path, { timeoutMs: POLL_TIMEOUT_MS });
      } catch (err) {
        error = err;
      }
      pending -= 1;
      if (stopped) return;
      // An answer overtaken by that of a later request is already out of date.
      if (id > shown) {
        shown = id;
        if (!error) {
          known = data;
          lastAnswer.set(path, data);
        }
        setAnswer({ path, data: known, error });
      }
      if (pending === 0) schedule();
    };
    const onVisibility = () => {
      if (document.hidden) window.clearTimeout(timer);
      else if (pending === 0) fetchNow();
    };

    load.current = fetchNow;
    fetchNow();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stopped = true;
      load.current = null;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [path, fixedInterval]);

  const refresh = useCallback(() => load.current?.(), []);
  const current = answer.path === path ? answer : { data: lastAnswer.get(path) ?? null, error: null };
  return { data: current.data, error: current.error, refresh };
}

// Run a mutating call and expose its busy and error state to a button.
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = useCallback(async (fn) => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      setError(err.message);
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, run, clearError: () => setError(null) };
}
