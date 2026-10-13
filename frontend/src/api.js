// Thin client for the DataTraceMind API, plus the snapshot hook the pages poll with.
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

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

// The newest value of every snapshot part this page has received, whichever
// request brought it. A part stays on screen while the page around it changes,
// and a page that is opened again shows what it last knew while it loads.
const partValues = new Map();
let partsVersion = 0;
const partListeners = new Set();
const subscribeToParts = (listener) => {
  partListeners.add(listener);
  return () => partListeners.delete(listener);
};
const partKey = (name, incident) => (name === "incident" ? `incident:${incident}` : name);
const readParts = (names, incident) => Object.fromEntries(names.map((name) => [name, partValues.get(partKey(name, incident))]));

// Keep the named parts of the page's snapshot fresh with one request for all
// of them (GET /api/snapshot lists the names; `incident` is the ID that the
// "incident" part is for). The next request goes out `interval` ms after the
// last one was answered, so a slow server is never asked twice at once, and
// nothing is sent while the tab is hidden. `interval` may be a function of the
// snapshot; when it gives 0 or null the parts will not change by themselves,
// and they are fetched once, on `refresh()`, and when the tab is shown again.
// A part is undefined until its first answer and then keeps its last good
// value while a refresh is in flight or failing, so the page never flashes empty.
export function useSnapshot(parts, { incident = null, interval = 5000 } = {}) {
  useSyncExternalStore(subscribeToParts, () => partsVersion);
  const [failure, setFailure] = useState({ path: null, error: null });
  const latestInterval = useRef(interval);
  latestInterval.current = interval;
  const load = useRef(null);
  // A new fixed interval restarts the poll; a function is read each time it is needed.
  const fixedInterval = typeof interval === "function" ? null : interval;
  const wanted = parts.join(",");
  const path = wanted ? `/snapshot?parts=${wanted}${incident ? `&incident=${encodeURIComponent(incident)}` : ""}` : null;

  useEffect(() => {
    if (!path) return undefined;
    const names = wanted.split(",");
    let stopped = false;
    let timer = null;
    let sent = 0;
    let shown = 0;
    let pending = 0;

    const schedule = () => {
      const every =
        typeof latestInterval.current === "function" ? latestInterval.current(readParts(names, incident)) : latestInterval.current;
      if (every && !document.hidden) timer = window.setTimeout(fetchNow, every);
    };
    const fetchNow = async () => {
      window.clearTimeout(timer);
      const id = ++sent;
      pending += 1;
      let answer = null;
      let error = null;
      try {
        answer = await api(path, { timeoutMs: POLL_TIMEOUT_MS });
      } catch (err) {
        error = err;
      }
      pending -= 1;
      if (stopped) return;
      // An answer overtaken by that of a later request is already out of date.
      if (id > shown) {
        shown = id;
        setFailure((last) => (last.path === path && last.error === error ? last : { path, error }));
        if (!error) {
          for (const name of names) partValues.set(partKey(name, incident), answer[name]);
          partsVersion += 1;
          partListeners.forEach((listener) => listener());
        }
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
  }, [path, wanted, incident, fixedInterval]);

  const refresh = useCallback(() => load.current?.(), []);
  return { data: readParts(parts, incident), error: failure.path === path ? failure.error : null, refresh };
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
