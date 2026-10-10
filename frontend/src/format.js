// Formatting helpers shared by every page.

const LOCALE = { INR: "en-IN", USD: "en-US" };

export function money(value, currency) {
  if (value === null || value === undefined) return "–";
  return new Intl.NumberFormat(LOCALE[currency] || "en-US", {
    style: "currency",
    currency: currency || "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function compactMoney(value, currency) {
  return new Intl.NumberFormat(LOCALE[currency] || "en-US", {
    style: "currency",
    currency: currency || "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

export function number(value) {
  if (value === null || value === undefined) return "–";
  return new Intl.NumberFormat("en-US").format(value);
}

export function signedMoney(value, currency) {
  const text = money(Math.abs(value), currency);
  if (value > 0) return `+${text}`;
  if (value < 0) return `−${text}`;
  return text;
}

export function clock(iso) {
  if (!iso) return "–";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function dateTime(iso) {
  if (!iso) return "–";
  return new Date(iso).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function shortDate(isoDate) {
  const [year, month, day] = isoDate.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString([], { month: "short", day: "numeric" });
}

export function ago(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

export function titleCase(text) {
  return (text || "").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export function pretty(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
