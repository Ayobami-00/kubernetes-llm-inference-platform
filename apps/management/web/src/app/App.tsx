import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { ThemeControl } from './theme';
import ImportsPage from '../features/integrations/ImportsPage';
import ReadinessPage from '../features/integrations/ReadinessPage';
import CapacityView from '../features/capacity/CapacityView';
import WorkerProvisioner from '../features/capacity/WorkerProvisioner';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  Boxes,
  Check,
  ChevronDown,
  CircleHelp,
  Command,
  Cpu,
  Database,
  FlaskConical,
  GitBranch,
  Layers3,
  Loader2,
  LogOut,
  Network,
  PanelRightClose,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Terminal,
  Users,
  FileUp,
  PlugZap,
  X,
} from 'lucide-react';
import { endpoints as e, message, setCsrf, unwrap } from '../api/client';
import type { GraphNode, Run, SpecView } from '../api/client';
import { colours, icons, names } from '../components/resourceTheme';
const Graph = lazy(() => import('../components/Graph'));
import Inspector, { Badge, OperationCard, RunLauncher, formatDate } from '../components/Inspector';
import type { Act } from '../components/Inspector';

const navigation = [
  { path: '/', label: 'Topology', icon: Network },
  { path: '/customers', label: 'Customers', icon: Users },
  { path: '/capacity', label: 'GPU capacity', icon: Cpu },
  { path: '/runs', label: 'Runs & results', icon: FlaskConical },
  { path: '/operations', label: 'Operations', icon: Activity },
  { path: '/simulation', label: 'Simulation lab', icon: Sparkles },
  { path: '/imports', label: 'Imports', icon: FileUp },
  { path: '/integrations', label: 'Integrations', icon: PlugZap },
];
const allKinds = [
  'dataset-manifest',
  'customer',
  'serving-profile',
  'worker-group',
  'group',
  'instance',
  'scenario',
  'run',
  'cluster',
  'infrastructure-node',
];
export default function App() {
  const query = useQuery({
    queryKey: ['session'],
    queryFn: async () => {
      const data = await unwrap(e.getSession());
      setCsrf(data.csrf_token);
      return data;
    },
    retry: false,
    refetchInterval: 60000,
  });
  if (query.isLoading)
    return (
      <div className="boot">
        <Loader2 className="spin" />
        Connecting to your workspace…
      </div>
    );
  if (!query.data) return <Login />;
  return <Workspace />;
}
function Login() {
  const client = useQueryClient();
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return (
    <div className="login-shell">
      <div className="login-appearance">
        <ThemeControl />
      </div>
      <div className="login-art">
        <Brand />
        <div className="login-copy">
          <span className="eyebrow">A CONNECTED INFERENCE WORKSPACE</span>
          <h1>
            See the platform.
            <br />
            <em>Understand the whole.</em>
          </h1>
          <p>
            From customer intent to GPU capacity. One clear view of every workload, resource and
            experiment.
          </p>
          <div className="login-chain">
            <Database />
            <span />
            <Users />
            <span />
            <Layers3 />
            <span />
            <Cpu />
          </div>
          <div className="login-caption">
            <span className="pulse-dot" />
            LOCAL MANAGEMENT PLANE
          </div>
        </div>
        <small className="login-footer">Built for experiments. Designed for clarity.</small>
      </div>
      <div className="login-form-wrap">
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            setBusy(true);
            setError('');
            try {
              const info = await unwrap(e.login({ body: { username: 'admin', password } }));
              setCsrf(info.csrf_token);
              client.setQueryData(['session'], info);
            } catch (error) {
              setError(message(error));
            } finally {
              setBusy(false);
            }
          }}
        >
          <div className="login-icon">
            <ShieldCheck size={25} />
          </div>
          <h2>Welcome to KLIP</h2>
          <p>Sign in to your management workspace.</p>
          <label>
            Username
            <input value="admin" readOnly autoComplete="username" />
          </label>
          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(v) => setPassword(v.target.value)}
              autoComplete="current-password"
              placeholder="Enter your admin password"
              required
            />
          </label>
          {error && (
            <div className="notice error" role="alert">
              {error}
            </div>
          )}
          <button className="primary full" disabled={busy}>
            {busy ? (
              <Loader2 className="spin" size={16} />
            ) : (
              <>
                Open workspace
                <ArrowRight size={16} />
              </>
            )}
          </button>
          <div className="login-security">
            <ShieldCheck size={13} />
            Authenticated local workspace
          </div>
        </form>
      </div>
    </div>
  );
}
function Brand() {
  return (
    <Link className="brand" to="/" aria-label="KLIP home" title="Kubernetes LLM Inference Platform">
      <span className="brand-symbol">
        <Layers3 size={23} />
      </span>
      <span className="brand-name">KLIP</span>
    </Link>
  );
}
function Workspace() {
  const client = useQueryClient();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const [selection, setSelection] = useState<GraphNode | null>(null);
  const [snapshot, setSnapshot] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [kinds, setKinds] = useState(
    new Set([
      'dataset-manifest',
      'customer',
      'serving-profile',
      'worker-group',
      'group',
      'run',
      'cluster',
      'infrastructure-node',
    ]),
  );
  const [showFilters, setShowFilters] = useState(false);
  const [history, setHistory] = useState(false);
  const [creation, setCreation] = useState(false);
  const [isNew, setIsNew] = useState(false);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{
    text: string;
    error: boolean;
  } | null>(null);
  useEffect(() => {
    if (!feedback || feedback.error) return;
    const timer = window.setTimeout(() => setFeedback(null), 5000);
    return () => window.clearTimeout(timer);
  }, [feedback]);
  const returnFocus = useRef<HTMLElement | null>(null);
  function closeInspector() {
    setSelection(null);
    setIsNew(false);
    requestAnimationFrame(() => returnFocus.current?.focus({ preventScroll: true }));
  }
  const topology = useQuery({
    queryKey: ['topology', snapshot, history],
    queryFn: () =>
      snapshot
        ? unwrap(e.getRunTopology({ path: { identity: snapshot } }))
        : unwrap(e.getTopology({ query: { include_history: history } })),
    refetchInterval: 2000,
  });
  const groups = useQuery({
    queryKey: ['groups'],
    queryFn: () => unwrap(e.listWorkerGroups()),
    refetchInterval: 2000,
  });
  const operations = useQuery({
    queryKey: ['operations'],
    queryFn: () => unwrap(e.listOperations()),
    refetchInterval: 1500,
  });
  const runs = useQuery({
    queryKey: ['runs'],
    queryFn: () => unwrap(e.listRuns()),
    refetchInterval: 2000,
  });
  const scenarios = useQuery({
    queryKey: ['scenarios'],
    queryFn: () => unwrap(e.listScenarios()),
  });
  const customers = useQuery({
    queryKey: ['customers'],
    queryFn: () => unwrap(e.listCustomers()),
  });
  const profiles = useQuery({
    queryKey: ['profiles'],
    queryFn: () => unwrap(e.listServingProfiles()),
  });
  const workerSpecs = useQuery({
    queryKey: ['worker-specs'],
    queryFn: () => unwrap(e.listWorkerGroupSpecs()),
  });
  const capacity = useQuery({
    queryKey: ['capacity'],
    queryFn: () => unwrap(e.getCapacity()),
    refetchInterval: 15000,
  });
  const act: Act = async (action, success) => {
    setBusy(true);
    setFeedback(null);
    try {
      await action();
      await client.invalidateQueries({
        predicate: (q) => q.queryKey[0] !== 'session',
      });
      if (success) setFeedback({ text: success, error: false });
    } catch (error) {
      setFeedback({ text: message(error), error: true });
    } finally {
      setBusy(false);
    }
  };
  const selected =
    (!isNew && topology.data?.nodes.find((n) => n.id === selection?.id)) || selection;
  function select(node: GraphNode) {
    returnFocus.current = document.activeElement as HTMLElement | null;
    setIsNew(false);
    setSelection(node);
  }
  function selectRun(
    run: Run,
    source = 'management',
    provenance = run.fixture ? 'synthetic' : 'unverified',
  ) {
    select({
      id: 'run:' + run.id,
      kind: 'run',
      label: run.snapshot.scenario.id,
      subtitle: run.status,
      status: run.status,
      provenance: 'historical',
      resource_id: run.id,
      document: { ...run, _management: { source, provenance } } as unknown as Record<
        string,
        unknown
      >,
    });
  }
  function newConfiguration(kind: string) {
    const source = (
      {
        customer: customers.data,
        scenario: scenarios.data,
        'serving-profile': profiles.data,
        'worker-group': workerSpecs.data,
      } as Record<string, SpecView[] | undefined>
    )[kind]?.[0];
    if (!source?.document) return;
    const document = {
      ...structuredClone(source.document),
      id: '',
      revision: 1,
    };
    setSelection({
      id: 'new:' + kind,
      kind,
      label: 'New ' + names[kind],
      subtitle: '',
      status: 'draft',
      provenance: 'configuration',
      resource_id: '',
      document: {
        id: '',
        kind,
        current_revision: 0,
        version: 0,
        document,
        draft: document,
      },
    });
    setIsNew(true);
    setCreation(false);
  }
  const count = topology.data?.counts;
  const active =
    operations.data?.filter((o) => ['queued', 'running'].includes(o.operation.status)).length || 0;
  const filtered =
    topology.data?.nodes.filter((n) =>
      `${n.label} ${n.subtitle}`.toLowerCase().includes(search.toLowerCase()),
    ) || [];
  const title = navigation.find((n) => n.path === pathname)?.label || 'Topology';
  const usage = { synthetic: 0, reported: 0, missing: 0 };
  for (const item of runs.data || []) {
    for (const measurement of item.run.result?.measurements || []) {
      for (const value of [measurement.input_tokens, measurement.output_tokens]) {
        if (typeof value !== 'number') usage.missing++;
        else usage[item.run.fixture ? 'synthetic' : 'reported'] += value;
      }
    }
  }
  const tokenHint =
    usage.synthetic.toLocaleString() +
    ' synthetic · ' +
    usage.reported.toLocaleString() +
    ' reported tokens' +
    (usage.missing ? ' · some usage unreported' : '');

  return (
    <div className="application">
      <aside className="navigation">
        <Brand />
        <span className="nav-label">WORKSPACE</span>
        <nav>
          {navigation.map((item) => (
            <Link
              key={item.path}
              aria-label={item.label}
              title={item.label}
              to={item.path}
              className={pathname === item.path ? 'active' : ''}
              onClick={() => {
                setSnapshot(null);
                setSelection(null);
                setSearch('');
              }}
            >
              <item.icon size={17} />
              <span>{item.label}</span>
              {item.path === '/operations' && active > 0 && (
                <span className="nav-count">{active}</span>
              )}
              {item.path === '/simulation' && <span className="tiny-label">SIM</span>}
            </Link>
          ))}
        </nav>
        <div className="nav-divider" />
        <span className="nav-label">CONFIGURATION</span>
        <button className="nav-link" onClick={() => newConfiguration('customer')}>
          <Plus size={16} />
          New customer
        </button>
        <button
          className="nav-link"
          onClick={() => {
            const node = topology.data?.nodes.find((n) => n.kind === 'worker-group');
            if (node) select(node);
          }}
        >
          <Boxes size={16} />
          Worker profiles
        </button>
        <a className="nav-link" href="/api/v1/configuration/export" download>
          <ArrowDownToLine size={16} />
          Export configuration
        </a>
        <div className="nav-bottom">
          <div className="user-card">
            <span className="user-avatar">A</span>
            <div>
              <b>Administrator</b>
              <small>admin · local</small>
            </div>
            <button
              className="icon-button"
              aria-label="Sign out"
              onClick={() =>
                act(async () => {
                  await unwrap(e.logout());
                  setCsrf('');
                  client.clear();
                })
              }
            >
              <LogOut size={15} />
            </button>
          </div>
        </div>
      </aside>
      <main className="workspace">
        <header className="topbar">
          <div className="breadcrumbs">
            <span>Workspace</span>
            <ChevronDown size={12} className="breadcrumb-chevron" />
            <b>{snapshot ? 'Run snapshot' : title}</b>
          </div>
          <div className="topbar-right">
            <ThemeControl />
            <span className="environment">
              <span className="status-dot ready" />
              Local environment
            </span>
            <span className="topbar-divider" />
            <ShieldCheck size={16} />
            <span>Admin</span>
          </div>
        </header>
        <div className="page-heading">
          <div>
            <div className="eyebrow">KLIP / MANAGEMENT</div>
            <h1>
              {snapshot
                ? 'Frozen run snapshot'
                : pathname === '/'
                  ? 'Your platform, connected.'
                  : title}
            </h1>
            <p>
              {snapshot
                ? 'Historical inputs are preserved exactly as recorded.'
                : pathname === '/'
                  ? 'Explore how customers, models and GPU capacity fit together.'
                  : pathname === '/capacity'
                    ? 'Provider observations, regional availability and account instances.'
                    : pathname === '/runs'
                      ? 'Execution progress and stored assessments, with provenance intact.'
                      : pathname === '/simulation'
                        ? 'Exercise complete workflows with persistent, explicitly synthetic resources.'
                        : 'Manage your workspace with context, history and clear progress.'}
            </p>
          </div>
          <div className="heading-actions">
            {snapshot ? (
              <button
                className="secondary"
                onClick={() => {
                  setSnapshot(null);
                  setSelection(null);
                }}
              >
                Return to current topology
              </button>
            ) : (
              <>
                <span className="simulation-label">
                  <FlaskConical size={13} />
                  Simulation enabled
                </span>
                <div className="create-menu">
                  <button className="primary" onClick={() => setCreation(!creation)}>
                    <Plus size={16} />
                    Create
                    <ChevronDown size={13} />
                  </button>
                  {creation && (
                    <div className="dropdown">
                      {['customer', 'scenario', 'serving-profile', 'worker-group'].map((kind) => (
                        <button key={kind} onClick={() => newConfiguration(kind)}>
                          {names[kind]}
                          <ArrowRight size={13} />
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
        {!['/capacity', '/imports', '/integrations'].includes(pathname) && (
          <div className="summary-grid">
            <Summary
              icon={Users}
              label="Customers"
              value={count?.customers ?? '—'}
              hint="Published service contracts"
            />
            <Summary
              icon={Cpu}
              label="Configured GPU demand"
              value={count?.planned_gpus ?? '—'}
              hint={`Across ${count?.serving_deployments ?? 0} distinct deployments`}
            />
            <Summary
              icon={Activity}
              label={snapshot ? 'Worker specifications' : 'Active worker groups'}
              value={(snapshot ? count?.worker_group_specs : count?.worker_groups) ?? 0}
              hint={
                snapshot
                  ? 'Frozen configuration inputs'
                  : `${groups.data?.filter((g) => g.group.status.phase === 'ready').length || 0} GPU-ready · simulated`
              }
            />
            <Summary
              icon={FlaskConical}
              label={snapshot ? 'Historical run' : 'Active runs'}
              value={snapshot ? 1 : (count?.active_runs ?? 0)}
              hint={snapshot ? 'Immutable recorded inputs' : tokenHint}
            />
          </div>
        )}
        {feedback && (
          <div
            role={feedback.error ? 'alert' : 'status'}
            className={`toast ${feedback.error ? 'error' : ''}`}
          >
            <span>{feedback.error ? <CircleHelp size={16} /> : <Check size={16} />}</span>
            <p>{feedback.text}</p>
            <button
              className="icon-button"
              aria-label="Dismiss notification"
              onClick={() => setFeedback(null)}
            >
              <X size={15} />
            </button>
          </div>
        )}
        <section className={`content-surface ${selected ? 'has-inspector' : ''}`}>
          <div className="content-primary">
            {pathname === '/' ? (
              <>
                <div className="graph-toolbar">
                  <div className="graph-heading">
                    <Network size={16} />
                    <b>{snapshot ? 'Snapshot topology' : 'Platform topology'}</b>
                    <span className="count-chip">
                      {topology.data?.nodes.filter((n) => kinds.has(n.kind)).length || 0} resources
                    </span>
                  </div>
                  <div className="toolbar-actions">
                    <div className="search-input">
                      <Search size={14} />
                      <input
                        aria-label="Search resources"
                        placeholder="Find a resource…"
                        value={search}
                        onChange={(v) => setSearch(v.target.value)}
                      />
                      <kbd>⌕</kbd>
                    </div>
                    <button
                      className={`secondary small ${showFilters ? 'selected' : ''}`}
                      onClick={() => setShowFilters(!showFilters)}
                    >
                      <Settings2 size={14} />
                      Layers
                    </button>
                    <button
                      className="icon-button"
                      aria-label="Refresh topology"
                      onClick={() => client.invalidateQueries({ queryKey: ['topology'] })}
                    >
                      <RefreshCw size={15} />
                    </button>
                  </div>
                </div>
                {showFilters && (
                  <div className="filter-bar">
                    {allKinds.map((kind) => (
                      <label key={kind}>
                        <input
                          type="checkbox"
                          checked={kinds.has(kind)}
                          onChange={() =>
                            setKinds((previous) => {
                              const next = new Set(previous);
                              next.has(kind) ? next.delete(kind) : next.add(kind);
                              return next;
                            })
                          }
                        />
                        <span style={{ background: colours[kind] }} />
                        {names[kind]}
                      </label>
                    ))}
                    <label>
                      <input
                        type="checkbox"
                        checked={history}
                        onChange={(v) => setHistory(v.target.checked)}
                      />
                      Include run history
                    </label>
                  </div>
                )}
                <div className="graph-canvas">
                  {topology.isLoading ? (
                    <div className="canvas-state">
                      <Loader2 className="spin" />
                      <p>Connecting your resources…</p>
                    </div>
                  ) : topology.error ? (
                    <div className="canvas-state error-text">
                      <CircleHelp />
                      <p>{message(topology.error)}</p>
                      <button className="secondary" onClick={() => topology.refetch()}>
                        Retry
                      </button>
                    </div>
                  ) : (
                    topology.data && (
                      <Suspense
                        fallback={
                          <div className="canvas-state">
                            <Loader2 className="spin" />
                            <p>Preparing your platform graph…</p>
                          </div>
                        }
                      >
                        <Graph
                          key={snapshot || 'current'}
                          topology={topology.data}
                          selected={selected?.id || null}
                          onSelect={select}
                          visibleKinds={kinds}
                          search={search}
                          persistPreferences={!snapshot}
                        />
                      </Suspense>
                    )
                  )}
                  <div className="canvas-context">
                    <span className="pulse-dot" />
                    {snapshot ? 'IMMUTABLE RUN INPUTS' : 'CONFIGURATION + PERSISTED SIMULATION'}
                  </div>
                </div>
                <footer className="graph-footer">
                  <div className="legend">
                    {['dataset-manifest', 'customer', 'serving-profile', 'worker-group'].map(
                      (kind) => (
                        <span key={kind}>
                          <i style={{ background: colours[kind] }} />
                          {names[kind]}
                        </span>
                      ),
                    )}
                  </div>
                  <span>Click a resource to explore · Drag to arrange</span>
                </footer>
              </>
            ) : pathname === '/capacity' ? (
              <CapacityView
                data={capacity.data}
                requestError={capacity.error ? message(capacity.error) : undefined}
                busy={busy}
                refresh={() => act(() => unwrap(e.refreshCapacity()), 'Capacity refresh requested')}
                provisioning={(current) => (
                  <WorkerProvisioner
                    capacity={capacity.data}
                    current={current}
                    groups={groups.data || []}
                    busy={busy || groups.isPending || Boolean(groups.error)}
                    act={act}
                  />
                )}
              />
            ) : pathname === '/imports' ? (
              <ImportsPage act={act} busy={busy} />
            ) : pathname === '/integrations' ? (
              <ReadinessPage />
            ) : pathname === '/runs' ? (
              <div className="list-page">
                <div className="section-title">
                  <div>
                    <h3>Experiments & results</h3>
                    <p>Historical verdicts retain their original inputs.</p>
                  </div>
                  <Badge value="synthetic" />
                </div>
                <div className="data-table">
                  <div className="table-row table-header">
                    <span>SCENARIO / RUN</span>
                    <span>EXECUTION</span>
                    <span>MEASUREMENTS</span>
                    <span>SLO VERDICT</span>
                    <span>FINISHED</span>
                  </div>
                  {runs.data?.map(({ run, source, provenance }) => (
                    <button
                      className="table-row"
                      key={run.id}
                      onClick={() => selectRun(run, source, provenance)}
                    >
                      <span>
                        <b>{run.snapshot.scenario.id.replace('-baseline', '')}</b>
                        <small>
                          {run.id} · {run.fixture ? 'synthetic' : provenance}
                        </small>
                      </span>
                      <span>
                        <Badge value={run.status} />
                      </span>
                      <span>
                        <Badge value={run.result?.validity || 'not_evaluated'} />
                      </span>
                      <span>
                        <Badge value={run.result?.slo_verdict || 'not_evaluated'} />
                      </span>
                      <span>{formatDate(run.finished_at)}</span>
                    </button>
                  ))}
                </div>
                {!runs.data?.length && (
                  <Empty
                    title="No runs yet"
                    text="Create simulated resources, then launch a scenario to see results here."
                  />
                )}
              </div>
            ) : pathname === '/operations' ? (
              <section className="list-page" tabIndex={0} aria-label="Operation activity">
                <div className="section-title">
                  <div>
                    <h3>Operation activity</h3>
                    <p>Persisted steps, attempts and cleanup status.</p>
                  </div>
                  <span className="count-chip">{active} active</span>
                </div>
                {operations.data?.length ? (
                  operations.data.map((value) => (
                    <OperationCard key={value.operation.id} value={value} />
                  ))
                ) : (
                  <Empty
                    title="Ready when you are"
                    text="Create a simulated worker group to follow its lifecycle here."
                  />
                )}
              </section>
            ) : pathname === '/simulation' ? (
              <div className="list-page">
                <div className="notice synthetic">
                  <FlaskConical size={17} />
                  Simulation resources and outcomes are stored in PostgreSQL. They do not represent
                  measured inference performance.
                </div>
                <div className="section-title">
                  <div>
                    <h3>Simulation lab</h3>
                    <p>Start with capacity, deploy serving, then exercise a scenario.</p>
                  </div>
                </div>
                <div className="lab-steps">
                  {[
                    [
                      '01',
                      'Allocate capacity',
                      'Choose a worker profile and an optional failure preset.',
                    ],
                    [
                      '02',
                      'Deploy serving',
                      'Place a simulated model deployment on a ready group.',
                    ],
                    ['03', 'Run an experiment', 'Choose a scenario and a synthetic result preset.'],
                  ].map(([number, title, text]) => (
                    <div key={number}>
                      <span>{number}</span>
                      <strong>{title}</strong>
                      <p>{text}</p>
                    </div>
                  ))}
                </div>
                <h4>Worker profiles</h4>
                <div className="resource-list">
                  {topology.data?.nodes
                    .filter((n) => n.kind === 'worker-group')
                    .map((n) => (
                      <ResourceRow key={n.id} node={n} onClick={() => select(n)} />
                    ))}
                </div>
                <h4>Serving deployments</h4>
                <div className="resource-list">
                  {topology.data?.nodes
                    .filter((n) => n.kind === 'serving-profile')
                    .map((n) => (
                      <ResourceRow key={n.id} node={n} onClick={() => select(n)} />
                    ))}
                </div>
                <h4>Scenarios</h4>
                <div className="resource-list">
                  {topology.data?.nodes
                    .filter((n) => n.kind === 'scenario')
                    .map((n) => (
                      <ResourceRow key={n.id} node={n} onClick={() => select(n)} />
                    ))}
                </div>
              </div>
            ) : (
              <div className="list-page">
                <div className="section-title">
                  <div>
                    <h3>Customer workloads</h3>
                    <p>Requirements, serving assignments and published revisions.</p>
                  </div>
                  <button className="secondary small" onClick={() => newConfiguration('customer')}>
                    <Plus size={14} />
                    New customer
                  </button>
                </div>
                <div className="resource-list">
                  {filtered
                    .filter((n) => n.kind === 'customer')
                    .map((n) => (
                      <ResourceRow key={n.id} node={n} onClick={() => select(n)} />
                    ))}
                </div>
              </div>
            )}
          </div>
          {selected && (
            <Inspector
              key={selected.id}
              node={selected}
              onClose={closeInspector}
              act={act}
              busy={busy}
              groups={groups.data || []}
              operations={operations.data || []}
              onSnapshot={(id) => {
                setSnapshot(id);
                setSelection(null);
                navigate('/');
              }}
              onPublished={(specification) => {
                const doc = specification.document;
                if (!doc) return;
                const prefix = selected.kind === 'serving-profile' ? 'profile' : selected.kind;
                setSelection({
                  ...selected,
                  id: `${prefix}:${doc.id}@${doc.revision}`,
                  resource_id: doc.id,
                  label: 'name' in doc ? doc.name : doc.id.replaceAll('-', ' '),
                  status: 'published',
                  document: specification as unknown as Record<string, unknown>,
                });
                setIsNew(false);
              }}
              newResource={isNew}
            />
          )}
        </section>
        <footer className="workspace-footer">
          <span>
            <GitBranch size={12} />
            KLIP · local management
          </span>
          <span>Live observations are timestamped. Synthetic results are labelled.</span>
        </footer>
      </main>
    </div>
  );
}
function Summary({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: typeof Users;
  label: string;
  value: string | number;
  hint: string;
}) {
  return (
    <div className="summary-card">
      <div>
        <span>{label}</span>
        <Icon size={16} />
      </div>
      <strong>{value}</strong>
      <small>{hint}</small>
    </div>
  );
}
function ResourceRow({ node, onClick }: { node: GraphNode; onClick: () => void }) {
  const Icon = icons[node.kind as keyof typeof icons] || Boxes;
  return (
    <button className="resource-row" onClick={onClick}>
      <span className="row-symbol" style={{ color: colours[node.kind] }}>
        <Icon size={19} />
      </span>
      <span>
        <b>{node.label}</b>
        <small>{node.subtitle}</small>
      </span>
      <Badge value={node.status} />
      <ArrowRight size={15} />
    </button>
  );
}
function Empty({ title, text }: { title: string; text: string }) {
  return (
    <div className="empty-state">
      <Network size={33} />
      <h3>{title}</h3>
      <p>{text}</p>
    </div>
  );
}
