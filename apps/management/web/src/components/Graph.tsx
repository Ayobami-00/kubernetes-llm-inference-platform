import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useNodesInitialized,
  useReactFlow,
} from '@xyflow/react';
import type { Edge, Node, NodeProps, Viewport } from '@xyflow/react';
import type { ELK } from 'elkjs/lib/elk-api';
import { ArrowUpRight, Boxes } from 'lucide-react';
import { endpoints, unwrap } from '../api/client';
import type { GraphNode, Topology } from '../api/client';

import { colours, icons, names } from './resourceTheme';
import { useTheme } from '../app/theme';
type VisualNode = Node<GraphNode & { dimmed: boolean }, 'resource'>;
function ResourceNode({ data, selected }: NodeProps<VisualNode>) {
  const Icon = icons[data.kind as keyof typeof icons] || Boxes;
  return (
    <div
      className={`resource-node ${selected ? 'chosen' : ''} ${data.dimmed ? 'dimmed' : ''}`}
      style={{ '--node-colour': colours[data.kind] || '#aaa' } as React.CSSProperties}
      data-testid={`node-${data.resource_id}`}
      title={
        data.kind === 'group' ? 'Double-click to expand or collapse GPU instances' : data.label
      }
    >
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
      <div className="node-eyebrow">
        <span>
          <Icon size={12} />
          {names[data.kind]}
        </span>
        <ArrowUpRight size={12} />
      </div>
      <strong>{data.label}</strong>
      <p>{data.subtitle}</p>
      <div className="node-bottom">
        <span className={`status-dot ${data.status}`} />
        <span>{data.status.replaceAll('_', ' ')}</span>
        <span className={`provenance ${data.provenance}`}>
          {data.provenance === 'configuration' ? 'CONFIG' : data.provenance.toUpperCase()}
        </span>
      </div>
    </div>
  );
}
const nodeTypes = { resource: ResourceNode };
let layoutEngine: Promise<ELK> | undefined;
const getLayoutEngine = () =>
  (layoutEngine ??= import('elkjs/lib/elk.bundled.js').then(({ default: Engine }) => new Engine()));
function Canvas({
  topology,
  selected,
  onSelect,
  visibleKinds,
  search,
}: {
  topology: Topology;
  selected: string | null;
  onSelect: (node: GraphNode) => void;
  visibleKinds: Set<string>;
  search: string;
}) {
  const { resolved } = useTheme();
  const [nodes, setNodes, onNodesChange] = useNodesState<VisualNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const { fitView, setViewport } = useReactFlow();
  const viewport = useRef<Viewport | null>(null);
  const [expanded, setExpanded] = useState(new Set<string>());
  const positions = useRef<Record<string, { x: number; y: number }>>({});
  const initialized = useRef(false);
  const measured = useNodesInitialized();
  const [layoutReady, setLayoutReady] = useState(false);
  useEffect(() => {
    if (!measured || initialized.current) return;
    initialized.current = true;
    const position = viewport.current
      ? setViewport(viewport.current)
      : fitView({ padding: 0.12, maxZoom: 1 });
    void position.finally(() => setLayoutReady(true));
  }, [measured, setViewport, fitView]);
  const [preferencesReady, setPreferencesReady] = useState(false);
  useEffect(() => {
    unwrap(endpoints.getPreferences())
      .then((p) => {
        positions.current = (p.positions || {}) as typeof positions.current;
        viewport.current = (p.viewport || null) as Viewport | null;
        setExpanded(new Set((p.expanded || []) as string[]));
      })
      .catch(() => {})
      .finally(() => setPreferencesReady(true));
  }, []);
  const visible = (node: GraphNode) =>
    visibleKinds.has(node.kind) ||
    (node.kind === 'instance' &&
      topology.edges.some((edge) => edge.target === node.id && expanded.has(edge.source)));
  const structuralKey = topology.nodes
    .filter(visible)
    .map((n) => n.id)
    .sort()
    .join('|');
  const matching = useMemo(
    () =>
      new Set(
        topology.nodes
          .filter((n) =>
            `${n.label} ${n.subtitle} ${n.resource_id}`
              .toLowerCase()
              .includes(search.toLowerCase()),
          )
          .map((n) => n.id),
      ),
    [topology, search],
  );
  const neighborhood = useMemo(() => {
    if (!selected) return null;
    const result = new Set([selected]);
    let changed = true;
    // Trace both upstream and downstream without crossing into unrelated sibling workloads.
    const upstream = new Set([selected]),
      downstream = new Set([selected]);
    while (changed) {
      changed = false;
      for (const edge of topology.edges) {
        if (upstream.has(edge.target) && !upstream.has(edge.source)) {
          upstream.add(edge.source);
          changed = true;
        }
        if (downstream.has(edge.source) && !downstream.has(edge.target)) {
          downstream.add(edge.target);
          changed = true;
        }
      }
    }
    upstream.forEach((n) => result.add(n));
    downstream.forEach((n) => result.add(n));
    return result;
  }, [selected, topology]);
  useEffect(() => {
    if (!preferencesReady) return;
    let cancelled = false;
    const available = topology.nodes.filter(visible);
    const ids = new Set(available.map((n) => n.id));
    const links = topology.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
    getLayoutEngine()
      .then((engine) =>
        engine.layout({
          id: 'root',
          layoutOptions: {
            'elk.algorithm': 'layered',
            'elk.direction': 'RIGHT',
            'elk.spacing.nodeNode': '30',
            'elk.layered.spacing.nodeNodeBetweenLayers': '90',
          },
          children: available.map((n) => ({ id: n.id, width: 245, height: 126 })),
          edges: links.map((e) => ({
            id: e.id,
            sources: [e.source],
            targets: [e.target],
          })),
        }),
      )
      .then((layout) => {
        if (cancelled) return;
        const calculated = Object.fromEntries(
          (layout.children || []).map((n) => [n.id, { x: n.x || 0, y: n.y || 0 }]),
        );
        setNodes(
          available.map((n) => ({
            id: n.id,
            type: 'resource',
            position: positions.current[n.id] || calculated[n.id] || { x: 0, y: 0 },
            data: { ...n, dimmed: false },
            selected: n.id === selected,
          })),
        );
      });
    return () => {
      cancelled = true;
    };
    // Only structural changes arrange nodes. Polling and selection preserve the canvas.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structuralKey, preferencesReady]);
  useEffect(() => {
    const map = new Map(topology.nodes.map((n) => [n.id, n]));
    setNodes((previous) =>
      previous.map((n) => ({
        ...n,
        selected: n.id === selected,
        data: {
          ...(map.get(n.id) || n.data),
          dimmed: !matching.has(n.id) || !!(neighborhood && !neighborhood.has(n.id)),
        },
      })),
    );
    const ids = new Set(topology.nodes.filter(visible).map((n) => n.id));
    setEdges(
      topology.edges
        .filter((e) => ids.has(e.source) && ids.has(e.target))
        .map((e) => ({
          ...e,
          type: 'smoothstep',
          label: selected && (e.source === selected || e.target === selected) ? e.label : undefined,
          style: {
            stroke:
              selected && neighborhood?.has(e.source) && neighborhood?.has(e.target)
                ? 'var(--graph-edge-active)'
                : 'var(--graph-edge)',
            strokeWidth: 1.2,
            opacity:
              !selected || (neighborhood?.has(e.source) && neighborhood?.has(e.target)) ? 1 : 0.15,
          },
          markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--graph-edge)' },
          labelStyle: { fill: 'var(--text-secondary)', fontSize: 10 },
          labelBgStyle: { fill: 'var(--surface)' },
        })),
    );
  }, [topology, selected, visibleKinds, matching, neighborhood, setNodes, setEdges]);
  const persist = useCallback(() => {
    unwrap(
      endpoints.savePreferences({
        body: { positions: positions.current, viewport: viewport.current, expanded: [...expanded] },
      }),
    ).catch(() => {});
  }, [expanded]);
  return (
    <ReactFlow
      data-layout-ready={layoutReady ? 'true' : 'false'}
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onNodeClick={(_, node) => onSelect(node.data)}
      onNodeDoubleClick={(_, node) => {
        if (node.data.kind === 'group')
          setExpanded((previous) => {
            const next = new Set(previous);
            next.has(node.id) ? next.delete(node.id) : next.add(node.id);
            return next;
          });
      }}
      onMoveEnd={(_, value) => {
        viewport.current = value;
        persist();
      }}
      onNodeDragStop={(_, node) => {
        positions.current[node.id] = node.position;
        persist();
      }}
      nodesConnectable={false}
      deleteKeyCode={null}
      minZoom={0.2}
      maxZoom={1.5}
      colorMode={resolved}
      proOptions={{ hideAttribution: false }}
    >
      <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--graph-dot)" />
      <Controls showInteractive={false} />
      <MiniMap
        nodeColor={(node) => colours[node.data.kind as string] || '#aaa'}
        maskColor="var(--minimap-mask)"
        pannable
        zoomable
      />
    </ReactFlow>
  );
}
export default function Graph(props: Parameters<typeof Canvas>[0]) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}
