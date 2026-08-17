import {
  Area,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  XAxis,
  YAxis,
} from 'recharts'
import { betaCurve, clipCurve } from '../lib/beta'
import { Skeleton } from './primitives'

/**
 * The Beta posterior, drawn from the alpha and beta the API sent.
 *
 * The shaded region is the API's credible interval, not a recomputed one, and
 * the vertical rule is the API's posterior mean. If either is missing the
 * chart draws the curve without them rather than guessing.
 */
export function PosteriorChart({
  alpha,
  beta,
  ciLow,
  ciHigh,
  mean,
  gate,
  height = 130,
  color = 'var(--color-nx-accent)',
}) {
  if (!Number.isFinite(alpha) || !Number.isFinite(beta)) {
    return <Skeleton height={height} />
  }
  const data = clipCurve(betaCurve(alpha, beta), ciLow, ciHigh)

  return (
    <div>
      <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 6, right: 2, bottom: 0, left: 2 }}>
          <XAxis dataKey="x" type="number" domain={[0, 1]} hide />
          <YAxis type="number" domain={[0, 1.05]} hide />
          <Area
            dataKey="density"
            stroke={color}
            strokeWidth={1.5}
            fill={color}
            fillOpacity={0.1}
            isAnimationActive={false}
            dot={false}
          />
          <Area
            dataKey="band"
            stroke="none"
            fill={color}
            fillOpacity={0.2}
            isAnimationActive={false}
            connectNulls={false}
            dot={false}
          />
          {Number.isFinite(mean) ? (
            <ReferenceLine x={mean} stroke="var(--color-nx-text)" strokeOpacity={0.7} />
          ) : null}
          {Number.isFinite(gate) ? (
            <ReferenceLine
              x={gate}
              stroke="var(--color-nx-experimental)"
              strokeOpacity={0.55}
              strokeDasharray="3 3"
            />
          ) : null}
          <Line dataKey="density" stroke="none" dot={false} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
      </div>
      <div className="mt-1 flex justify-between px-1">
        <span className="nx-num text-[8.5px] text-nx-faint-2">0.0</span>
        <span className="nx-num text-[8.5px] text-nx-faint-2">0.5</span>
        <span className="nx-num text-[8.5px] text-nx-faint-2">1.0</span>
      </div>
    </div>
  )
}
