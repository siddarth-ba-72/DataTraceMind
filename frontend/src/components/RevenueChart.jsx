// Daily net revenue: what the source implies versus what analytics reports.
//
// Two series on one axis. Where the pipeline is correct the lines coincide, so
// days on which they differ get a marker: the gap is the story, and it must be
// findable without comparing two overlapping strokes.
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { compactMoney, money, shortDate, signedMoney } from "../format.js";

// The chart is drawn at the width it is given, so axis text keeps its type
// size on a phone as on a desktop. This is the width assumed until measured.
const WIDTH = 1040;
// Below this there is no room for a tooltip beside the pointer.
const NARROW = 520;
const MARGIN = { top: 16, right: 24, bottom: 30, left: 58 };
const SERIES = [
  { key: "source_net", label: "Source-derived (contract)", className: "series-1" },
  { key: "analytics_net", label: "Analytics daily_revenue", className: "series-2" },
];

function niceCeiling(value) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((m) => m * magnitude >= value);
  return step * magnitude;
}

// The width of an element, kept current as the layout changes. Measured
// before the first paint, so the chart is never shown at the wrong scale.
function useWidth(fallback) {
  const [node, setNode] = useState(null);
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    if (!node) return undefined;
    const measure = () => setWidth(node.clientWidth || fallback);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [node, fallback]);
  return [setNode, width];
}

export default function RevenueChart({ daily, currency, tolerance = 0.01 }) {
  const rows = useMemo(() => daily.filter((d) => d.currency === currency), [daily, currency]);
  const [hover, setHover] = useState(null);
  const [showTable, setShowTable] = useState(false);
  const [chart, width] = useWidth(WIDTH);
  const frame = useRef(null);

  if (rows.length < 2) return <p className="empty">Not enough days of data to chart yet.</p>;

  const narrow = width < NARROW;
  const height = narrow ? 220 : 270;
  const innerWidth = width - MARGIN.left - MARGIN.right;
  const innerHeight = height - MARGIN.top - MARGIN.bottom;
  const top = niceCeiling(Math.max(...rows.flatMap((r) => [r.source_net, r.analytics_net])));
  const x = (index) => MARGIN.left + (index / (rows.length - 1)) * innerWidth;
  const y = (value) => MARGIN.top + innerHeight - (Math.max(value, 0) / top) * innerHeight;
  const path = (key) => rows.map((r, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(r[key]).toFixed(1)}`).join(" ");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((share) => share * top);
  // As many date labels as fit without touching, and never more than eight.
  const labelEvery = Math.ceil(rows.length / Math.max(2, Math.min(8, Math.floor(innerWidth / 64))));
  const last = rows.length - 1;
  const differs = (row) => Math.abs(row.analytics_net - row.source_net) > tolerance;
  const mismatched = rows.filter(differs).length;

  function onMove(event) {
    const box = frame.current.getBoundingClientRect();
    const position = ((event.clientX - box.left) / box.width) * width;
    const index = Math.round(((position - MARGIN.left) / innerWidth) * (rows.length - 1));
    setHover(Math.min(rows.length - 1, Math.max(0, index)));
  }

  const active = hover === null ? null : rows[hover];
  const tooltipLeft = hover === null ? 0 : (x(hover) / width) * 100;

  return (
    <div className="chart" ref={chart}>
      <div className="chart-legend">
        {SERIES.map((series) => (
          <span key={series.key} className="legend-item">
            <span className={`legend-line ${series.className}`} aria-hidden="true" />
            {series.label}
          </span>
        ))}
        <span className="legend-item">
          <span className="legend-dot series-2" aria-hidden="true" />
          Day where they differ
        </span>
        <button type="button" className="link chart-toggle" onClick={() => setShowTable((v) => !v)}>
          {showTable ? "Show chart" : "Show as table"}
        </button>
      </div>

      {showTable ? (
        <div className="table-scroll">
          <table className="table table-inset numeric">
            <thead>
              <tr>
                <th>Day</th>
                <th className="right">Source-derived</th>
                <th className="right">Analytics</th>
                <th className="right">Difference</th>
              </tr>
            </thead>
            <tbody>
              {[...rows].reverse().map((row) => (
                <tr key={row.date}>
                  <td>{shortDate(row.date)}</td>
                  <td className="right">{money(row.source_net, currency)}</td>
                  <td className="right">{money(row.analytics_net, currency)}</td>
                  <td className="right">
                    {differs(row) ? signedMoney(row.analytics_net - row.source_net, currency) : "matches"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="chart-frame" ref={frame}>
          <svg
            viewBox={`0 0 ${width} ${height}`}
            role="img"
            aria-label={`Daily net revenue in ${currency}, source-derived versus analytics. ${mismatched} of ${rows.length} days differ.`}
            onPointerDown={onMove}
            onPointerMove={onMove}
            // A finger leaves the chart when it lifts; the reading it chose stays up.
            onPointerLeave={(event) => event.pointerType === "mouse" && setHover(null)}
            onPointerCancel={() => setHover(null)}
          >
            {ticks.map((tick) => (
              <g key={tick}>
                <line className="grid" x1={MARGIN.left} x2={width - MARGIN.right} y1={y(tick)} y2={y(tick)} />
                <text className="axis" x={MARGIN.left - 8} y={y(tick)} textAnchor="end" dominantBaseline="middle">
                  {compactMoney(tick, currency)}
                </text>
              </g>
            ))}
            {rows.map(
              (row, index) =>
                // The last day is always labelled; a regular label too close to it gives way.
                (index === last || (index % labelEvery === 0 && last - index >= labelEvery / 2)) && (
                  <text key={row.date} className="axis" x={x(index)} y={height - 8} textAnchor="middle">
                    {shortDate(row.date)}
                  </text>
                ),
            )}
            {active && <line className="crosshair" x1={x(hover)} x2={x(hover)} y1={MARGIN.top} y2={MARGIN.top + innerHeight} />}
            {SERIES.map((series) => (
              <path key={series.key} className={`line ${series.className}`} d={path(series.key)} />
            ))}
            {rows.map(
              (row, index) =>
                differs(row) && (
                  <circle key={row.date} className="marker series-2" cx={x(index)} cy={y(row.analytics_net)} r="4.5" />
                ),
            )}
            {active &&
              SERIES.map((series) => (
                <circle key={series.key} className={`marker ${series.className}`} cx={x(hover)} cy={y(active[series.key])} r="4.5" />
              ))}
          </svg>
          {active && (
            <div
              className={`tooltip ${narrow ? "tooltip-above" : tooltipLeft > 60 ? "tooltip-left" : ""}`}
              style={narrow ? undefined : { left: `${tooltipLeft}%` }}
            >
              <div className="tooltip-title">{shortDate(active.date)}</div>
              {SERIES.map((series) => (
                <div key={series.key} className="tooltip-row">
                  <span className={`legend-line ${series.className}`} aria-hidden="true" />
                  <strong>{money(active[series.key], currency)}</strong>
                  <span className="muted">{series.label}</span>
                </div>
              ))}
              <div className="tooltip-row tooltip-delta">
                {differs(active)
                  ? `Analytics is ${signedMoney(active.analytics_net - active.source_net, currency)} against the source`
                  : "Analytics matches the source"}
              </div>
            </div>
          )}
        </div>
      )}
      <p className="muted chart-note">
        {mismatched === 0
          ? "The two lines coincide on every day shown: analytics matches the source."
          : `${mismatched} of ${rows.length} days differ. The lines coincide wherever analytics matches the source.`}
      </p>
    </div>
  );
}
