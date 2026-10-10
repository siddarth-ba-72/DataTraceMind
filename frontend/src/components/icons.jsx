// Line icons, drawn on a 24 x 24 grid in the current text colour.

const PATHS = {
  activity: ["M22 12h-4l-3 9L9 3l-3 9H2"],
  alert: ["M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4", "M12 17h.01"],
  list: ["M8 6h13", "M8 12h13", "M8 18h13", "M3 6h.01", "M3 12h.01", "M3 18h.01"],
  flask: ["M9 3h6", "M10 3v6.5L4.6 18.6A1.6 1.6 0 0 0 6 21h12a1.6 1.6 0 0 0 1.4-2.4L14 9.5V3", "M7.6 15h8.8"],
  external: ["M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6", "M15 3h6v6", "M10 14 21 3"],
  reset: ["M3 12a9 9 0 1 0 3-6.7L3 8", "M3 3v5h5"],
  play: ["M7 4.5v15l12-7.5-12-7.5z"],
  pause: ["M8 5v14", "M16 5v14"],
  check: ["M20 6 9 17l-5-5"],
  x: ["M18 6 6 18", "M6 6l12 12"],
  chevronRight: ["m9 18 6-6-6-6"],
  arrowRight: ["M5 12h14", "m13 6 6 6-6 6"],
  database: [
    "M12 8c5 0 9-1.3 9-3s-4-3-9-3-9 1.3-9 3 4 3 9 3z",
    "M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5",
    "M3 12c0 1.7 4 3 9 3s9-1.3 9-3",
  ],
  clock: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z", "M12 6v6l4 2"],
  shield: ["M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z", "m9 12 2 2 4-4"],
  zap: ["M13 2 3 14h9l-1 8 10-12h-9l1-8z"],
  search: ["M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z", "m21 21-4.3-4.3"],
  layers: ["M12 2 2 7l10 5 10-5-10-5z", "m2 17 10 5 10-5", "m2 12 10 5 10-5"],
  info: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z", "M12 16v-4", "M12 8h.01"],
  cpu: [
    "M7 5h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z",
    "M9.5 9.5h5v5h-5z",
    "M9 2v3", "M15 2v3", "M9 19v3", "M15 19v3", "M2 9h3", "M2 15h3", "M19 9h3", "M19 15h3",
  ],
  coins: ["M9 14a6 6 0 1 0 0-12 6 6 0 0 0 0 12z", "M15.6 10.1a6 6 0 1 1-5.5 5.5", "M9 6v4", "M7.5 7.5h3"],
  send: ["M22 2 11 13", "M22 2 15 22l-4-9-9-4 20-7z"],
  user: ["M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2", "M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z"],
};

export function Icon({ name, size = 16 }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}

// The product mark: a trace through three observations, the last one flagged.
export function BrandMark({ size = 28 }) {
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 28 28" aria-hidden="true">
      <rect width="28" height="28" rx="7" fill="#2557d6" />
      <path d="M5.5 18.5 11 12l4.5 4L22 8.5" fill="none" stroke="#ffffff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="22" cy="8.5" r="2.6" fill="#ffb347" stroke="#2557d6" strokeWidth="1.2" />
    </svg>
  );
}
