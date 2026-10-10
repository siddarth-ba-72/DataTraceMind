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

export async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const code = getAccessCode();
  if (code) headers["X-Demo-Access-Code"] = code;
  const response = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
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

// Poll `path` every `intervalMs`. Keeps the last good data while a refresh is
// in flight or failing, so the page never flashes empty.
export function usePoll(path, intervalMs = 2500) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const latest = useRef(path);
  latest.current = path;

  const refresh = useCallback(async () => {
    if (!path) return;
    try {
      const next = await api(path);
      if (latest.current === path) {
        setData(next);
        setError(null);
      }
    } catch (err) {
      if (latest.current === path) setError(err);
    }
  }, [path]);

  useEffect(() => {
    setData(null);
    setError(null);
    if (!path) return undefined;
    refresh();
    const timer = window.setInterval(refresh, intervalMs);
    return () => window.clearInterval(timer);
  }, [path, intervalMs, refresh]);

  return { data, error, refresh };
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
