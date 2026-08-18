import { useState } from 'react'
import { Dot, Label } from './primitives'

const CHANNEL_COLORS = [
  'var(--color-nx-failing)',
  'var(--color-nx-accent)',
  'var(--color-nx-experimental)',
  'var(--color-nx-proven)',
]

export function formatMetricValue(name, val) {
  if (val == null || !Number.isFinite(val)) return '—'
  if (
    name.includes('pct') ||
    name.includes('ratio') ||
    name === 'cpu_utilization' ||
    name === 'disk_used_pct' ||
    name === 'heap_used_pct' ||
    name === 'error_rate'
  ) {
    const p = val > 1 ? val : val * 100
    return `${p.toFixed(p < 1 ? 2 : 1)}%`
  }
  if (name.endsWith('_ms') || name.includes('latency') || name.includes('delay')) {
    return `${Math.round(val)} ms`
  }
  if (name.includes('minutes') || name.includes('age')) {
    return `${Math.round(val)} min`
  }
  if (val >= 1000) {
    return Math.round(val).toLocaleString()
  }
  return val.toFixed(1)
}

function MetricRow({ name, points, color, hoverIndex, onHover }) {
  const values = (points || []).filter((v) => Number.isFinite(v))
  const latest = values.length ? values[values.length - 1] : null
  const min = values.length ? Math.min(...values) : 0
  const max = values.length ? Math.max(...values) : 1
  const span = max - min || 1
  const pad = span * 0.1
  const lo = min - pad
  const hi = max + pad

  const width = 480
  const height = 44

  const coords = values.map((val, i) => {
    const x = (i / Math.max(1, values.length - 1)) * width
    const y = height - ((val - lo) / (hi - lo)) * height
    return { x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), val }
  })

  const line = coords.map(({ x, y }, i) => `${i ? 'L' : 'M'}${x},${y}`).join(' ')
  const area = `M0,${height} ${line.slice(1)} L${width},${height} Z`
  const gradId = `traj-grad-${name.replace(/[^a-z0-9]/gi, '-')}`

  const hoveredVal =
    hoverIndex !== null && coords[hoverIndex] ? coords[hoverIndex].val : latest

  return (
    <div className="flex items-stretch gap-3 border-b border-nx-line-soft py-2 last:border-b-0">
      {/* Metric Metadata Side Column */}
      <div className="flex w-[160px] shrink-0 flex-col justify-center">
        <div className="flex items-center gap-1.5">
          <Dot color={color} size={5} />
          <span className="truncate font-mono text-[11px] font-medium text-nx-text-2" title={name}>
            {name}
          </span>
        </div>
        <div className="mt-1 flex items-baseline gap-2">
          <span className="nx-num text-[14px] font-bold tracking-tight" style={{ color }}>
            {formatMetricValue(name, hoveredVal)}
          </span>
          <span className="nx-num text-[9px] text-nx-faint-2">
            lo {formatMetricValue(name, min)} · hi {formatMetricValue(name, max)}
          </span>
        </div>
      </div>

      {/* SVG Canvas & Line Chart */}
      <div className="relative flex-1 rounded border border-nx-line/60 bg-nx-sunken/80 px-2 py-1">
        {values.length < 2 ? (
          <div className="flex h-full items-center justify-center text-[10px] text-nx-faint">
            No telemetry points
          </div>
        ) : (
          <div className="relative h-[44px] w-full">
            <svg
              viewBox={`0 0 ${width} ${height}`}
              preserveAspectRatio="none"
              className="h-[44px] w-full overflow-visible"
              onMouseLeave={() => onHover(null)}
            >
              <defs>
                <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={color} stopOpacity="0.25" />
                  <stop offset="100%" stopColor={color} stopOpacity="0.0" />
                </linearGradient>
              </defs>
              <path d={area} fill={`url(#${gradId})`} stroke="none" />
              <path
                d={line}
                fill="none"
                stroke={color}
                strokeWidth="1.8"
                strokeLinejoin="round"
                strokeLinecap="round"
              />
              {coords.map((pt, i) => (
                <rect
                  key={i}
                  x={pt.x - width / (values.length * 2)}
                  y="0"
                  width={width / values.length}
                  height={height}
                  fill="transparent"
                  className="cursor-pointer"
                  onMouseEnter={() => onHover(i)}
                />
              ))}
              {hoverIndex !== null && coords[hoverIndex] && (
                <g>
                  <line
                    x1={coords[hoverIndex].x}
                    y1="0"
                    x2={coords[hoverIndex].x}
                    y2={height}
                    stroke="rgba(255,255,255,0.4)"
                    strokeDasharray="2 2"
                    strokeWidth="1"
                  />
                  <circle
                    cx={coords[hoverIndex].x}
                    cy={coords[hoverIndex].y}
                    r="4"
                    fill={color}
                    stroke="#08090c"
                    strokeWidth="1.5"
                  />
                </g>
              )}
            </svg>
            <span className="nx-num absolute top-0.5 right-1 text-[8.5px] text-nx-faint-3">
              {formatMetricValue(name, hi)}
            </span>
            <span className="nx-num absolute bottom-0.5 right-1 text-[8.5px] text-nx-faint-3">
              {formatMetricValue(name, lo)}
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

export function TrajectoryChart({ trajectory }) {
  const [hoverIndex, setHoverIndex] = useState(null)
  const metrics = trajectory?.metrics ?? {}
  const names = Object.keys(metrics).slice(0, 4)

  if (!names.length) {
    return (
      <div className="flex h-32 items-center justify-center text-[11px] text-nx-faint">
        No telemetry window available in the sensory tier.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-nx-line bg-nx-panel p-3.5 shadow-sm">
      <div className="flex items-center justify-between border-b border-nx-line pb-2.5">
        <div className="flex items-center gap-2">
          <Label>Telemetry Trajectory</Label>
          <span className="nx-num text-[10px] text-nx-faint-2">
            {names.length} channels · live sensory window
          </span>
        </div>
        {hoverIndex !== null ? (
          <span className="nx-num text-[10px] text-nx-accent">
            sample point #{hoverIndex + 1} inspected
          </span>
        ) : (
          <span className="nx-num text-[9.5px] text-nx-faint-3">
            hover points to inspect exact values
          </span>
        )}
      </div>

      <div className="flex flex-col gap-1">
        {names.map((name, i) => (
          <MetricRow
            key={name}
            name={name}
            points={metrics[name]}
            color={CHANNEL_COLORS[i % CHANNEL_COLORS.length]}
            hoverIndex={hoverIndex}
            onHover={setHoverIndex}
          />
        ))}
      </div>

      {/* Time Axis Legend */}
      <div className="flex items-center justify-between pt-1 font-mono text-[9px] text-nx-faint-2">
        <span>-180m</span>
        <span>-120m</span>
        <span>-60m</span>
        <span className="text-nx-accent">now (live)</span>
      </div>
    </div>
  )
}
