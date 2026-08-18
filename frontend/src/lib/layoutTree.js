// Tidy-tree layout for the genealogy graph.
//
// react-flow positions nodes but does not lay them out, and a generic layout
// dependency would be a lot of weight for one screen. Each family is laid out
// with the classic leaf-cursor walk — leaves placed left to right in order, a
// parent centred over its children — and the families are then packed into
// bands rather than strung out in one row.
//
// The banding is what makes the whole genealogy legible at once. Eight families
// side by side is several thousand pixels wide against a viewport that is not,
// so fitting it would shrink the nodes past reading size. Wrapping trades
// horizontal run for vertical, which the panel has.
//
// `parent_id` alone defines the tree. Merge edges — a lineage ancestor that is
// not the declared parent — are drawn on top of it and take no layout space,
// which is what keeps a merged family readable.

export const NODE_WIDTH = 220
export const NODE_HEIGHT = 86
const GAP_X = 36
const GAP_Y = 120
const FAMILY_GAP_X = 80
const BAND_GAP_Y = 90
// Roughly the width the panel can show at a readable zoom. Exceeding it starts
// a new band.
const MAX_BAND_WIDTH = 2000

export function layoutTree(nodes, edges) {
  const present = new Set(nodes.map((n) => n.id))
  const parentOf = new Map()
  for (const edge of edges) {
    if (edge.kind !== 'merge' && present.has(edge.source) && present.has(edge.target)) {
      parentOf.set(edge.target, edge.source)
    }
  }

  const children = new Map()
  const roots = []
  for (const node of nodes) {
    const parent = parentOf.get(node.id)
    if (parent) {
      if (!children.has(parent)) children.set(parent, [])
      children.get(parent).push(node.id)
    } else {
      roots.push(node.id)
    }
  }

  // Lay each family out on its own origin first, so its extent is known before
  // it is placed. Iterative so a pathological lineage cannot blow the stack.
  function layoutFamily(rootId) {
    const local = new Map()
    let cursor = 0
    const stack = [{ id: rootId, depth: 0, phase: 'down' }]
    while (stack.length) {
      const frame = stack[stack.length - 1]
      const kids = children.get(frame.id) ?? []
      if (frame.phase === 'down') {
        frame.phase = 'up'
        if (!kids.length) {
          local.set(frame.id, { x: cursor * (NODE_WIDTH + GAP_X), y: frame.depth * GAP_Y })
          cursor += 1
          stack.pop()
          continue
        }
        for (let i = kids.length - 1; i >= 0; i -= 1) {
          stack.push({ id: kids[i], depth: frame.depth + 1, phase: 'down' })
        }
        continue
      }
      const xs = kids.map((k) => local.get(k)?.x).filter((x) => x !== undefined)
      const x = xs.length ? (xs[0] + xs[xs.length - 1]) / 2 : cursor * (NODE_WIDTH + GAP_X)
      local.set(frame.id, { x, y: frame.depth * GAP_Y })
      stack.pop()
    }
    let width = 0
    let height = 0
    for (const { x, y } of local.values()) {
      width = Math.max(width, x + NODE_WIDTH)
      height = Math.max(height, y + NODE_HEIGHT)
    }
    return { local, width, height }
  }

  const families = roots.map((root) => layoutFamily(root))

  const positions = new Map()
  let bandX = 0
  let bandY = 0
  let bandHeight = 0
  for (const family of families) {
    if (bandX && bandX + family.width > MAX_BAND_WIDTH) {
      bandY += bandHeight + BAND_GAP_Y
      bandX = 0
      bandHeight = 0
    }
    for (const [id, point] of family.local) {
      positions.set(id, { x: point.x + bandX, y: point.y + bandY })
    }
    bandX += family.width + FAMILY_GAP_X
    bandHeight = Math.max(bandHeight, family.height)
  }

  return positions
}
