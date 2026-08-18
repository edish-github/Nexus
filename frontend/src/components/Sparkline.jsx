import { useState } from 'react'

/**
 * The fleet tile's trailing window.
 *
 * Inline SVG with gradient fill & hover tooltips. `points` comes
 * straight from `telemetry_embeddings.raw_metrics`.
 */
export function Sparkline({ points, color, width = 200, height = 38 }) {
  const [hoverIndex, setHoverIndex] = useState(null)
  const values = (points || []).filter((v) => Number.isFinite(v))

  if (values.length < 2) {
    return (
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="h-[38px] w-full">
        <line
          x1="0"
          y1={height - 1}
          x2={width}
          y2={height - 1}
          stroke="rgba(255,255,255,.08)"
          strokeWidth="1"
          strokeDasharray="3 3"
        />
      </svg>
    )
  }

  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const pad = span * 0.12
  const lo = min - pad
  const hi = max + pad

  const coords = values.map((value, i) => {
    const x = (i / (values.length - 1)) * width
    const y = height - ((value - lo) / (hi - lo)) * height
    return { x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), val: value }
  })

  const line = coords.map(({ x, y }, i) => `${i ? 'L' : 'M'}${x},${y}`).join(' ')
  const area = `M0,${height} ${line.slice(1)} L${width},${height} Z`
  const gradId = `spark-grad-${Math.random().toString(36).slice(2, 9)}`

  return (
    <div className="relative group w-full">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="h-[38px] w-full overflow-visible transition-opacity"
        onMouseLeave={() => setHoverIndex(null)}
      >
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.3" />
            <stop offset="100%" stopColor={color} stopOpacity="0.0" />
          </linearGradient>
        </defs>
        <path d={area} fill={`url(#${gradId})`} stroke="none" />
        <path
          d={line}
          fill="none"
          stroke={color}
          strokeWidth="1.6"
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
            onMouseEnter={() => setHoverIndex(i)}
          />
        ))}
        {hoverIndex !== null && coords[hoverIndex] && (
          <g>
            <line
              x1={coords[hoverIndex].x}
              y1="0"
              x2={coords[hoverIndex].x}
              y2={height}
              stroke="rgba(255,255,255,0.3)"
              strokeDasharray="2 2"
              strokeWidth="1"
            />
            <circle
              cx={coords[hoverIndex].x}
              cy={coords[hoverIndex].y}
              r="3.5"
              fill={color}
              stroke="#08090c"
              strokeWidth="1.5"
            />
          </g>
        )}
      </svg>
      {hoverIndex !== null && coords[hoverIndex] && (
        <div
          className="pointer-events-none absolute -top-5 z-20 -translate-x-1/2 rounded bg-nx-elevated px-1.5 py-0.5 font-mono text-[9px] text-nx-text border border-nx-line shadow-md"
          style={{ left: `${(coords[hoverIndex].x / width) * 100}%` }}
        >
          {coords[hoverIndex].val.toFixed(2)}
        </div>
      )}
    </div>
  )
}
