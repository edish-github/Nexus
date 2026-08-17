// Tidy-tree layout for the genealogy graph.
//
// react-flow positions nodes but does not lay them out, and a generic layout
// dependency would be a lot of weight for one screen. This is the classic
// leaf-cursor walk: leaves are placed left to right in order, a parent centres
// over its children, and each root's subtree starts after the previous one.
//
// `parent_id` alone defines the tree. Merge edges — a lineage ancestor that is
// not the declared parent — are drawn on top of it and take no layout space,
// which is what keeps a merged family readable.

export const NODE_WIDTH = 168
export const NODE_HEIGHT = 62
const GAP_X = 26
const GAP_Y = 108

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

  const positions = new Map()
  let cursor = 0

  // Iterative so a pathological lineage cannot blow the stack.
  function place(rootId) {
    const stack = [{ id: rootId, depth: 0, phase: 'down' }]
    while (stack.length) {
      const frame = stack[stack.length - 1]
      const kids = children.get(frame.id) ?? []
      if (frame.phase === 'down') {
        frame.phase = 'up'
        if (!kids.length) {
          positions.set(frame.id, { x: cursor * (NODE_WIDTH + GAP_X), y: frame.depth * GAP_Y })
          cursor += 1
          stack.pop()
          continue
        }
        for (let i = kids.length - 1; i >= 0; i -= 1) {
          stack.push({ id: kids[i], depth: frame.depth + 1, phase: 'down' })
        }
        continue
      }
      const xs = kids.map((k) => positions.get(k)?.x).filter((x) => x !== undefined)
      const x = xs.length ? (xs[0] + xs[xs.length - 1]) / 2 : cursor * (NODE_WIDTH + GAP_X)
      positions.set(frame.id, { x, y: frame.depth * GAP_Y })
      stack.pop()
    }
  }

  for (const root of roots) {
    place(root)
    cursor += 0.7 // breathing room between families
  }

  return positions
}
