/**
 * The fleet tile's trailing window.
 *
 * Inline SVG rather than a chart component: there is one of these per service
 * refreshing every five seconds, and the drawing is two paths. `points` comes
 * straight from `telemetry_embeddings.raw_metrics`; an empty window draws a
 * flat rule and the caller says why it is empty.
 */
export function Sparkline({ points, color, width = 200, height = 34 }) {
  const values = (points || []).filter((v) => Number.isFinite(v))
  if (values.length < 2) {
    return (
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="h-[34px] w-full">
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
    return [Number(x.toFixed(1)), Number(y.toFixed(1))]
  })

  const line = coords.map(([x, y], i) => `${i ? 'L' : 'M'}${x},${y}`).join(' ')
  const area = `M0,${height} ${line.slice(1)} L${width},${height} Z`

  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="h-[34px] w-full">
      <path d={area} fill={color} fillOpacity="0.09" stroke="none" />
      <path
        d={line}
        fill="none"
        stroke={color}
        strokeWidth="1.4"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}
