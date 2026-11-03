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
  useReactFlow,
} from '@xyflow/react';
import type { Edge, Node, NodeProps, Viewport } from '@xyflow/react';
import ELK from 'elkjs/lib/elk.bundled.js';
import {
  ArrowUpRight,
  Boxes,
  Cpu,
  Database,
  FlaskConical,
  Layers3,
  Network,
  Users,
} from 'lucide-react';
import { endpoints, unwrap } from '../api/client';
import type { GraphNode, Topology } from '../api/client';

export const colours: Record<string, string> = {
  'dataset-manifest': '#64b99e',
  customer: '#ab96ee',
  'serving-profile': '#82adf8',
  'worker-group': '#d7b67c',
  group: '#76c5ce',
  instance: '#76c5ce',
  scenario: '#ad9eaf',
  run: '#dc92b6',
};
export const icons = {
  customer: Users,
  'dataset-manifest': Database,
  'serving-profile': Layers3,
  'worker-group': Cpu,
  group: Boxes,
  instance: Cpu,
  scenario: Network,
  run: FlaskConical,
};
export const names: Record<string, string> = {
  'dataset-manifest': 'Dataset',
  customer: 'Customer',
  'serving-profile': 'Serving',
  'worker-group': 'Worker profile',
  group: 'Worker group',
  instance: 'GPU instance',
  scenario: 'Scenario',
  run: 'Run',
};
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
const elk = new ELK();
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
  const [nodes, setNodes, onNodesChange] = useNodesState<VisualNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const { fitView, setViewport } = useReactFlow();
  const viewport = useRef<Viewport | null>(null);
  const [expanded, setExpanded] = useState(new Set<string>());
  const positions = useRef<Record<string, { x: number; y: number }>>({});
  const initialized = useRef(false);
  const [preferencesReady, setPreferencesReady] = useState(false);
  useEffect(() => {
    unwrap(endpoints.getPreferences())
      .then((p) => {
        positions.current = (p.positions || {}) as typeof positions.current;
        viewport.current = (p.viewport || null) as Viewport | null;
        setExpanded(new Set((p.expanded || []) as string[]));
      })
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
    elk
      .layout({
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
      })
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
        if (!initialized.current) {
          initialized.current = true;
          setTimeout(
            () =>
              viewport.current
                ? setViewport(viewport.current)
                : fitView({ padding: 0.12, maxZoom: 1 }),
            80,
          );
        }
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
                ? '#9d8ad4'
                : '#363943',
            strokeWidth: 1.2,
            opacity:
              !selected || (neighborhood?.has(e.source) && neighborhood?.has(e.target)) ? 1 : 0.15,
          },
          markerEnd: { type: MarkerType.ArrowClosed, color: '#5c586b' },
          labelStyle: { fill: '#c9c4d9', fontSize: 10 },
          labelBgStyle: { fill: '#161820' },
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
      colorMode="dark"
      proOptions={{ hideAttribution: false }}
    >
      <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#30323c" />
      <Controls showInteractive={false} />
      <MiniMap
        nodeColor={(node) => colours[node.data.kind as string] || '#aaa'}
        maskColor="#0d0f17b0"
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
