import { useCallback, useMemo, useState } from 'react'
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { usePolled } from '../lib/usePolled'
import { CLASS_COLOR, DASH, fixed, humanise, num } from '../lib/format'
import { NODE_HEIGHT, NODE_WIDTH, layoutTree } from '../lib/layoutTree'
import { EmptyState, ErrorState, Label, Meter, Skeleton } from '../components/primitives'
import { EventRow } from './Overview'

const LEGEND = [
  ['proven', 'proven'],
  ['experimental', 'experimental'],
  ['failing', 'failing'],
  ['institutional', 'institutional'],
  ['retired', 'retired'],
]

/** A playbook. Colour comes from the API's resolved `class`, never re-derived. */
function PlaybookNode({ data, selected }) {
  const color = CLASS_COLOR[data.node.class] ?? CLASS_COLOR.retired
  const node = data.node
  return (
    <div
      className="flex h-full w-full flex-col gap-1.5 rounded-md border px-2.5 py-2 transition-colors"
      style={{
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        borderColor: selected ? color : 'rgba(255,255,255,.09)',
        background: selected ? 'rgba(255,255,255,.06)' : 'var(--color-nx-raised)',
        boxShadow: selected ? `0 0 0 3px color-mix(in srgb, ${color} 22%, transparent)` : 'none',
        opacity: node.status === 'active' ? 1 : 0.55,
      }}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div className="flex items-center gap-1.5">
        <span className="h-[5px] w-[5px] shrink-0 rounded-full" style={{ background: color }} />
        <span className="nx-num text-[9px] text-nx-faint">g{node.generation}</span>
        <span className="nx-num ml-auto text-[10px]" style={{ color }}>
          {fixed(node.posterior_mean, 2)}
        </span>
      </div>
      <div
        className="truncate text-[11px] leading-tight"
        style={{ color: node.status === 'active' ? 'var(--color-nx-text-2)' : 'var(--color-nx-dim)' }}
        title={node.name}
      >
        {node.name}
      </div>
      <div className="mt-auto flex items-center gap-1.5">
        <Meter value={node.posterior_mean} color={color} height={2} />
        <span className="nx-num shrink-0 text-[8.5px] text-nx-faint-2">{node.trials}t</span>
      </div>
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}

const NODE_TYPES = { playbook: PlaybookNode }

export function Evolution() {
  const [category, setCategory] = useState('')
  const evolution = usePolled('/evolution', {
    intervalMs: 5000,
    params: { limit: 80, category: category || undefined },
  })

  if (evolution.error && !evolution.data) {
    return <ErrorState error={evolution.error} what="The evolution graph" />
  }

  const data = evolution.data

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-start gap-4 border-b border-nx-line px-5 py-3.5">
          <div>
            <h1 className="text-[19px] font-semibold tracking-[-0.01em]">Evolution</h1>
            <p className="mt-0.5 text-[11.5px] text-nx-dim">
              {data
                ? `${data.nodes.length} playbooks · ${Object.values(data.event_counts ?? {}).reduce((a, b) => a + b, 0)} lifecycle events · every node was written by a transaction`
                : DASH}
            </p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-3">
            {LEGEND.map(([key, label]) => (
              <span key={key} className="flex items-center gap-1.5 text-[10px] text-nx-dim">
                <span
                  className="h-[5px] w-[5px] rounded-full"
                  style={{ background: CLASS_COLOR[key] }}
                />
                {label}
              </span>
            ))}
          </div>
        </div>

        <div className="flex flex-wrap gap-1.5 border-b border-nx-line px-5 py-2.5">
          <FilterChip active={!category} onClick={() => setCategory('')}>
            All categories
          </FilterChip>
          {(data?.categories ?? []).map((key) => (
            <FilterChip key={key} active={category === key} onClick={() => setCategory(key)}>
              {humanise(key)}
            </FilterChip>
          ))}
        </div>

        <div className="min-h-0 flex-1">
          {evolution.loading && !data ? (
            <div className="p-6">
              <Skeleton rows={5} height={40} />
            </div>
          ) : !data?.nodes.length ? (
            <EmptyState
              title="No playbooks"
              body="The playbooks table has no rows in this category, so there is no genealogy to draw."
              source="playbooks"
            />
          ) : (
            <ReactFlowProvider>
              <Genealogy nodes={data.nodes} edges={data.edges} />
            </ReactFlowProvider>
          )}
        </div>
      </div>

      <aside className="flex w-[380px] shrink-0 flex-col border-l border-nx-line">
        <div className="flex items-center gap-2 border-b border-nx-line px-4 py-3">
          <Label>evolution_log</Label>
          <span className="text-[10px] text-nx-dim">append-only</span>
          <span className="nx-num ml-auto text-[10px] text-nx-faint-2">
            {num(data?.events?.length)} shown
          </span>
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          {evolution.loading && !data ? (
            <div className="p-4">
              <Skeleton rows={8} height={14} />
            </div>
          ) : !data?.events?.length ? (
            <EmptyState
              title="No lifecycle events"
              body="evolution_log has no rows for this filter."
              source="evolution_log"
            />
          ) : (
            data.events.map((event) => <EventRow key={event.id} event={event} />)
          )}
        </div>
      </aside>
    </div>
  )
}

function FilterChip({ active, onClick, children }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded px-2.5 py-1 text-[11px] transition-colors"
      style={{
        border: `1px solid ${active ? 'var(--color-nx-line-strong)' : 'var(--color-nx-line)'}`,
        background: active ? 'rgba(255,255,255,.06)' : 'transparent',
        color: active ? 'var(--color-nx-text)' : 'var(--color-nx-muted-3)',
      }}
    >
      {children}
    </button>
  )
}

function Genealogy({ nodes, edges }) {
  const { fitView } = useReactFlow()

  const flowNodes = useMemo(() => {
    const positions = layoutTree(nodes, edges)
    return nodes.map((node) => ({
      id: node.id,
      type: 'playbook',
      position: positions.get(node.id) ?? { x: 0, y: 0 },
      data: { node },
      style: { width: NODE_WIDTH, height: NODE_HEIGHT },
    }))
  }, [nodes, edges])

  const flowEdges = useMemo(
    () =>
      edges.map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: 'smoothstep',
        animated: false,
        style: {
          stroke:
            edge.kind === 'merge'
              ? 'var(--color-nx-sensory)'
              : 'var(--color-nx-institutional)',
          strokeWidth: edge.kind === 'merge' ? 1.3 : 1.1,
          strokeDasharray: edge.kind === 'merge' ? '4 3' : undefined,
          opacity: 0.6,
        },
      })),
    [edges],
  )

  const onInit = useCallback(() => {
    window.setTimeout(() => fitView({ padding: 0.12, minZoom: 0.55, duration: 300 }), 0)
  }, [fitView])

  return (
    <ReactFlow
      nodes={flowNodes}
      edges={flowEdges}
      nodeTypes={NODE_TYPES}
      onInit={onInit}
      fitView
      fitViewOptions={{ padding: 0.12, minZoom: 0.55 }}
      minZoom={0.2}
      maxZoom={1.6}
      proOptions={{ hideAttribution: true }}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      className="bg-nx-bg"
    >
      <Background color="rgba(255,255,255,.07)" gap={26} size={1} />
      <Controls showInteractive={false} className="!bottom-4 !left-4" />
    </ReactFlow>
  )
}
