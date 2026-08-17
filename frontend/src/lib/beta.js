// Rendering a Beta density from the alpha and beta the API sends.
//
// This is arithmetic on API-supplied parameters, not a source of data: the
// posterior mean and credible interval come from the API and are displayed as
// sent. Only the shape of the curve is computed here, because sending 120
// sample points over the wire on a five-second poll would be silly.

/**
 * Unnormalised Beta density on a grid, scaled so the peak is 1.
 * Computed in log space so a sharp posterior does not overflow.
 */
export function betaCurve(alpha, beta, points = 120) {
  if (!Number.isFinite(alpha) || !Number.isFinite(beta) || alpha <= 0 || beta <= 0) {
    return []
  }
  const xs = []
  const logs = []
  let max = -Infinity
  for (let i = 0; i < points; i += 1) {
    const x = (i + 0.5) / points
    const lp = (alpha - 1) * Math.log(x) + (beta - 1) * Math.log(1 - x)
    xs.push(x)
    logs.push(lp)
    if (lp > max) max = lp
  }
  return xs.map((x, i) => ({ x, density: Math.exp(logs[i] - max) }))
}

/** The curve clipped to a credible interval, for the shaded band. */
export function clipCurve(curve, low, high) {
  if (low === null || low === undefined || high === null || high === undefined) return []
  return curve.map((point) => ({
    ...point,
    band: point.x >= low && point.x <= high ? point.density : null,
  }))
}
