import { useEffect, useState } from 'react';
import * as Tabs from '@radix-ui/react-tabs';
import {
  ArrowRight,
  Check,
  ChevronRight,
  ExternalLink,
  FileJson,
  Loader2,
  Pencil,
  Play,
  Save,
  Trash2,
  X,
} from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { endpoints as e, message, mutationHeaders, unwrap } from '../api/client';
import type { GraphNode, GroupView, OperationView, Run, SpecView, SpecWrite } from '../api/client';
import { icons, names } from './resourceTheme';
import ConfigurationDetails from './ConfigurationDetails';

// Dynamic form paths edit generated shared document types; validation is performed by the API.
type Doc = Record<string, any>;
export const specApi = {
  customer: {
    save: e.saveCustomersDraft,
    publish: e.publishCustomers,
    history: e.historyCustomers,
    path: 'customers',
  },
  scenario: {
    save: e.saveScenariosDraft,
    publish: e.publishScenarios,
    history: e.historyScenarios,
    path: 'scenarios',
  },
  'serving-profile': {
    save: e.saveServingProfilesDraft,
    publish: e.publishServingProfiles,
    history: e.historyServingProfiles,
    path: 'serving-profiles',
  },
  'worker-group': {
    save: e.saveWorkerGroupSpecsDraft,
    publish: e.publishWorkerGroupSpecs,
    history: e.historyWorkerGroupSpecs,
    path: 'worker-group-specs',
  },
};
export type Act = (action: () => Promise<unknown>, success?: string) => Promise<void>;
export function Badge({ value }: { value: string }) {
  return <span className={`badge ${value}`}>{value.replaceAll('_', ' ')}</span>;
}
export function RunDetails({ run }: { run: Run }) {
  const result = run.result;
  return (
    <>
      <div className="notice synthetic">
        <FlaskIcon />
        Synthetic fixture data. These values are illustrative, not measured performance.
      </div>
      <div className="result-statuses">
        <div>
          <small>Execution</small>
          <Badge value={run.status} />
        </div>
        <div>
          <small>Measurements</small>
          <Badge value={result?.validity || 'not_evaluated'} />
        </div>
        <div>
          <small>SLO verdict</small>
          <Badge value={result?.slo_verdict || 'not_evaluated'} />
        </div>
        <div>
          <small>Quality</small>
          <Badge value="not_evaluated" />
        </div>
      </div>
      {run.stage && (
        <div className="progress-stage">
          <span className="pulse-dot" />
          {run.stage.replaceAll('_', ' ')}
        </div>
      )}
      {run.error && <div className="notice error">{run.error.message}</div>}
      {result && (
        <>
          <h4>Request accounting</h4>
          <div className="metric-grid">
            {Object.entries(result.requests).map(([key, value]) => (
              <div key={key}>
                <small>{key.replaceAll('_', ' ')}</small>
                <strong>{value.toLocaleString()}</strong>
              </div>
            ))}
          </div>
          <h4>Tokens and latency</h4>
          {result.measurements?.map((m) => (
            <section className="detail-card" key={m.customer_ref}>
              <strong>{m.customer_ref}</strong>
              <div className="inline-metrics">
                <span>
                  Input <b>{m.input_tokens.toLocaleString()}</b>
                </span>
                <span>
                  Cached <b>{m.cached_input_tokens.toLocaleString()}</b>
                </span>
                <span>
                  Output <b>{m.output_tokens.toLocaleString()}</b>
                </span>
              </div>
              {Object.entries(m.latencies || {}).map(([metric, value]) => (
                <div className="latency-row" key={metric}>
                  <span>{metric.replaceAll('_', ' ')}</span>
                  <span>
                    p50 {value.p50_ms} · p95 {value.p95_ms} · p99 {value.p99_ms} ms
                  </span>
                </div>
              ))}
            </section>
          ))}
          <h4>Stored SLO assessments</h4>
          {result.slo_assessments?.map((a) => (
            <section className="detail-card" key={a.customer_ref + a.metric}>
              <div className="split">
                <strong>{a.customer_ref}</strong>
                <Badge value={a.verdict} />
              </div>
              <p>
                {a.metric.replaceAll('_', ' ')} ≤ {a.threshold_ms} ms ·{' '}
                {a.minimum_attainment_fraction * 100}% attainment
              </p>
              {a.windows.map((w) => (
                <div className="window-row" key={w.offset_s}>
                  <span>
                    {w.offset_s}–{w.offset_s + w.duration_s}s
                  </span>
                  <span>
                    {w.good_requests}/{w.eligible_requests} eligible
                  </span>
                  <Badge value={w.verdict} />
                </div>
              ))}
            </section>
          ))}
          {result.invalid_reasons?.map((reason) => (
            <div className="notice error" key={reason}>
              {reason}
            </div>
          ))}
          {result.artifacts &&
            Object.entries(result.artifacts).map(([name, artifact]) => (
              <div className="artifact" key={name}>
                <FileJson size={15} />
                <span>
                  {name}
                  <small>{String((artifact as { uri: string }).uri)}</small>
                </span>
              </div>
            ))}
        </>
      )}
    </>
  );
}
function FlaskIcon() {
  return <span className="small-diamond">◇</span>;
}
function read(doc: Doc, path: string): any {
  return path.split('.').reduce<any>((v, k) => v?.[k], doc);
}
function assign(doc: Doc, path: string, value: unknown) {
  const next = structuredClone(doc);
  const keys = path.split('.');
  let target = next;
  keys.slice(0, -1).forEach((k) => (target = target[k] ??= {}));
  target[keys[keys.length - 1]] = value;
  return next;
}
const fields: Record<string, { path: string; label: string; type?: string; options?: string[] }[]> =
  {
    customer: [
      { path: 'name', label: 'Customer name' },
      { path: 'model_alias', label: 'Model alias' },
      {
        path: 'tier',
        label: 'Service tier',
        options: ['dedicated', 'provisioned', 'best_effort'],
      },
      {
        path: 'reserved_concurrency',
        label: 'Reserved concurrency',
        type: 'number',
      },
      { path: 'workload.dataset_ref', label: 'Dataset reference' },
      {
        path: 'limits.max_rendered_input_tokens',
        label: 'Input token limit',
        type: 'number',
      },
      {
        path: 'generation.max_output_tokens',
        label: 'Output token limit',
        type: 'number',
      },
      {
        path: 'limits.max_inflight_requests',
        label: 'Concurrent request limit',
        type: 'number',
      },
      {
        path: 'limits.request_timeout_ms',
        label: 'Request timeout (ms)',
        type: 'number',
      },
      {
        path: 'traffic_allowance.sustained_per_second',
        label: 'Sustained admissions / second',
        type: 'number',
      },
      {
        path: 'traffic_allowance.burst_per_second',
        label: 'Burst admissions / second',
        type: 'number',
      },
      {
        path: 'slo.targets.0.threshold_ms',
        label: 'First SLO target (ms)',
        type: 'number',
      },
      { path: 'slo.window_s', label: 'SLO window (seconds)', type: 'number' },
    ],
    scenario: [
      { path: 'seed', label: 'Random seed', type: 'number' },
      { path: 'warmup.duration_s', label: 'Warmup (seconds)', type: 'number' },
      {
        path: 'drain_timeout_s',
        label: 'Drain timeout (seconds)',
        type: 'number',
      },
    ],
    'serving-profile': [
      { path: 'model.alias', label: 'Model alias' },
      { path: 'model.repository', label: 'Model repository' },
      { path: 'model.revision', label: 'Immutable model revision' },
      { path: 'model.tokenizer_revision', label: 'Immutable tokenizer revision' },
      { path: 'engine.image', label: 'Serving image digest' },
      { path: 'worker_class', label: 'Worker class' },
      { path: 'topology.replicas', label: 'Replicas', type: 'number' },
      {
        path: 'topology.gpus_per_replica',
        label: 'GPUs per replica',
        type: 'number',
      },
      {
        path: 'topology.tensor_parallel_size',
        label: 'Tensor parallel size',
        type: 'number',
      },
      {
        path: 'topology.pipeline_parallel_size',
        label: 'Pipeline parallel size',
        type: 'number',
      },
      {
        path: 'engine.max_model_len',
        label: 'Model context length',
        type: 'number',
      },
      {
        path: 'engine.max_num_seqs',
        label: 'Engine concurrency',
        type: 'number',
      },
    ],
    'worker-group': [
      { path: 'instance_type', label: 'Lambda instance type' },
      { path: 'region', label: 'Region' },
      { path: 'desired_instances', label: 'Desired instances', type: 'number' },
      { path: 'hardware.gpu_model', label: 'GPU model' },
      { path: 'hardware.gpu_count', label: 'GPUs per instance', type: 'number' },
      {
        path: 'hardware.vram_gib_per_gpu',
        label: 'VRAM per GPU (GiB)',
        type: 'number',
      },
      { path: 'worker_class', label: 'Worker class' },
      { path: 'ttl_minutes', label: 'TTL (minutes)', type: 'number' },
      {
        path: 'drain_timeout_s',
        label: 'Drain timeout (seconds)',
        type: 'number',
      },
    ],
  };
export default function Inspector({
  node,
  onClose,
  act,
  busy,
  groups,
  operations,
  onSnapshot,
  onPublished,
  newResource = false,
}: {
  node: GraphNode;
  onClose: () => void;
  act: Act;
  busy: boolean;
  groups: GroupView[];
  operations: OperationView[];
  onSnapshot: (id: string) => void;
  onPublished?: (specification: SpecView) => void;
  newResource?: boolean;
}) {
  const configuration = node.document as unknown as SpecView;
  const isSpec = node.kind in specApi;
  const [tab, setTab] = useState(newResource ? 'configure' : 'overview');
  const [document, setDocument] = useState<Doc>(
    (configuration.draft || configuration.document || {}) as Doc,
  );
  const [saved, setSaved] = useState<SpecView | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [raw, setRaw] = useState('');
  const [localError, setLocalError] = useState('');
  const [groupId, setGroupId] = useState('');
  const [fault, setFault] = useState('none');
  const [minutes, setMinutes] = useState(30);
  useEffect(() => {
    setTab(newResource ? 'configure' : 'overview');
    setLocalError('');
    setAdvanced(false);
    setGroupId('');
  }, [node.id, newResource]);
  useEffect(() => {
    setDocument((configuration.draft || configuration.document || {}) as Doc);
    setSaved(null);
  }, [node.id, configuration.version]);
  const history = useQuery({
    queryKey: ['history', node.kind, node.resource_id],
    enabled: isSpec && tab === 'history' && !newResource,
    queryFn: () =>
      unwrap(
        specApi[node.kind as keyof typeof specApi].history({
          path: { resource_id: node.resource_id },
        }),
      ),
  });
  const selectedApi = specApi[node.kind as keyof typeof specApi];
  const base = saved || configuration;
  const data = (configuration.document || {}) as Doc;
  const savedDocument = base.draft || base.document || {};
  let dirty = JSON.stringify(document) !== JSON.stringify(savedDocument);
  if (advanced) {
    try {
      dirty = JSON.stringify(JSON.parse(raw)) !== JSON.stringify(savedDocument);
    } catch {
      dirty = true;
    }
  }
  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [onClose]);
  async function publish() {
    await act(async () => {
      const result = await unwrap(
        selectedApi.publish({
          path: { resource_id: document.id },
          body: { expected_version: base.version },
        }),
      );
      setSaved(result);
      setDocument((result.document || {}) as Doc);
      setTab('overview');
      onPublished?.(result);
    }, 'Revision published');
  }
  const Icon = icons[node.kind as keyof typeof icons] || FileJson;
  async function save() {
    try {
      setLocalError('');
      const doc = advanced ? JSON.parse(raw) : document;
      await act(async () => {
        const result = await unwrap(
          selectedApi.save({
            path: { resource_id: doc.id },
            body: {
              document: doc as SpecWrite['document'],
              expected_version: base.version || 0,
            },
          }),
        );
        setSaved(result);
        setDocument((result.draft || result.document) as Doc);
      }, 'Draft saved');
    } catch (error) {
      setLocalError(message(error));
    }
  }
  const activeOperations = operations.filter(
    (o) => o.operation.target.resource_id === node.resource_id,
  );
  return (
    <aside className="inspector" aria-label="Resource details">
      <header className="inspector-heading">
        <div className="resource-symbol" style={{ color: 'var(--accent)' }}>
          <Icon size={21} />
        </div>
        <div>
          <small>{newResource ? 'NEW CONFIGURATION' : names[node.kind]?.toUpperCase()}</small>
          <h2>{newResource ? 'Create ' + names[node.kind]?.toLowerCase() : node.label}</h2>
        </div>
        <button className="icon-button close" onClick={onClose} aria-label="Close details">
          <X size={18} />
        </button>
      </header>
      <Tabs.Root value={tab} onValueChange={setTab} className="inspector-tabs">
        <Tabs.List className="tab-list">
          {['overview', ...(isSpec ? ['configure', 'history'] : []), 'activity'].map((value) => (
            <Tabs.Trigger key={value} value={value}>
              {value}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        <div className="inspector-scroll">
          <Tabs.Content value="overview">
            <div className="split inspector-meta">
              <Badge value={node.status} />
              <span className={`provenance ${node.provenance}`}>{node.provenance}</span>
            </div>
            {node.kind === 'run' ? (
              <>
                <RunDetails run={node.document as unknown as Run} />
                <button className="secondary full" onClick={() => onSnapshot(node.resource_id)}>
                  Explore frozen snapshot <ExternalLink size={14} />
                </button>
                {['running', 'queued'].includes(node.status) && (
                  <button
                    className="secondary full"
                    disabled={busy}
                    onClick={() =>
                      act(
                        () =>
                          unwrap(
                            e.stopRun({
                              headers: mutationHeaders(),
                              path: { identity: node.resource_id },
                            }),
                          ),
                        'Stop requested',
                      )
                    }
                  >
                    Stop run
                  </button>
                )}
              </>
            ) : (
              <>
                {isSpec && (
                  <>
                    <div className="resource-ref">
                      {data.id}@{data.revision}
                    </div>
                    <p className="description">
                      {data.notes ||
                        'Published configuration. Execution readiness is checked separately.'}
                    </p>
                    <button className="secondary full" onClick={() => setTab('configure')}>
                      <Pencil size={14} />
                      Edit configuration <ArrowRight size={14} />
                    </button>
                    {node.kind === 'customer' && (
                      <>
                        {node.provenance !== 'historical' && (
                          <Assignment customer={data} act={act} busy={busy} />
                        )}
                      </>
                    )}
                    {node.kind === 'worker-group' && node.provenance !== 'historical' && (
                      <>
                        <h4>Allocate simulated capacity</h4>
                        <div className="notice synthetic">
                          Creates persisted simulation resources. Lambda account instances remain
                          observational.
                        </div>
                        <label>
                          Group name
                          <input
                            value={groupId}
                            onChange={(v) => setGroupId(v.target.value)}
                            placeholder={`${data.id}-demo`}
                          />
                        </label>
                        <label>
                          Simulation preset
                          <select value={fault} onChange={(v) => setFault(v.target.value)}>
                            {[
                              'none',
                              'shortage',
                              'transient',
                              'partial',
                              'ambiguous',
                              'termination',
                            ].map((f) => (
                              <option key={f} value={f}>
                                {f === 'none' ? 'Happy path' : f.replace('_', ' ')}
                              </option>
                            ))}
                          </select>
                        </label>
                        <button
                          className="primary full"
                          disabled={busy}
                          onClick={() =>
                            act(
                              () =>
                                unwrap(
                                  e.createWorkerGroup({
                                    headers: mutationHeaders(),
                                    body: {
                                      id: groupId || `${data.id}-demo`,
                                      spec_ref: `${data.id}@${data.revision}`,
                                      fault: fault as 'none',
                                    },
                                  }),
                                ),
                              'Worker group queued',
                            )
                          }
                        >
                          <Play size={14} />
                          Create simulated group
                        </button>
                      </>
                    )}
                    {node.kind === 'serving-profile' && node.provenance !== 'historical' && (
                      <>
                        <label>
                          GPU-ready worker group
                          <select value={groupId} onChange={(v) => setGroupId(v.target.value)}>
                            <option value="">Select a group</option>
                            {groups
                              .filter(
                                (g) =>
                                  g.group.status.phase === 'ready' &&
                                  g.group.spec.worker_class === data.worker_class,
                              )
                              .map((g) => (
                                <option key={g.id} value={g.id}>
                                  {g.id}
                                </option>
                              ))}
                          </select>
                        </label>
                        <button
                          className="primary full"
                          disabled={busy || !groupId}
                          onClick={() =>
                            act(
                              () =>
                                unwrap(
                                  e.deployServing({
                                    headers: mutationHeaders(),
                                    body: {
                                      id: (node.document.deployment_id as string) || data.id,
                                      profile_ref: `${data.id}@${data.revision}`,
                                      group_id: groupId,
                                    },
                                  }),
                                ),
                              'Simulated deployment queued',
                            )
                          }
                        >
                          <Play size={14} />
                          Deploy simulated serving
                        </button>
                      </>
                    )}
                    {node.kind === 'scenario' && (
                      <RunLauncher scenario={data} groups={groups} act={act} busy={busy} />
                    )}
                    <ConfigurationDetails kind={node.kind} document={data} />
                  </>
                )}
                {node.kind === 'group' && (
                  <>
                    <div className="notice synthetic">
                      Simulated GPU lifecycle. State persists across worker restarts.
                    </div>
                    <div className="detail-card">
                      {Object.entries((node.document.group as Doc).status.counts).map(([k, v]) => (
                        <div className="key-value" key={k}>
                          <span>{k.replaceAll('_', ' ')}</span>
                          <b>{String(v)}</b>
                        </div>
                      ))}
                      <div className="key-value">
                        <span>Allocated</span>
                        <b>{formatDate((node.document.group as Doc).status.allocated_at)}</b>
                      </div>
                      <div className="key-value">
                        <span>Expires</span>
                        <b>{formatDate((node.document.group as Doc).status.expires_at)}</b>
                      </div>
                    </div>
                    <label>
                      Extend lease (minutes)
                      <input
                        type="number"
                        value={minutes}
                        min={1}
                        max={1440}
                        onChange={(v) => setMinutes(Number(v.target.value))}
                      />
                    </label>
                    <button
                      className="secondary full"
                      disabled={busy || node.status !== 'ready'}
                      onClick={() =>
                        act(
                          () =>
                            unwrap(
                              e.renewWorkerGroup({
                                headers: mutationHeaders(),
                                path: { identity: node.resource_id },
                                body: { additional_minutes: minutes },
                              }),
                            ),
                          'Renewal queued',
                        )
                      }
                    >
                      Renew lease
                    </button>
                    <button
                      className="danger full"
                      disabled={busy || ['deleted', 'expired'].includes(node.status)}
                      onClick={() =>
                        act(
                          () =>
                            unwrap(
                              e.deleteWorkerGroup({
                                headers: mutationHeaders(),
                                path: { identity: node.resource_id },
                              }),
                            ),
                          'Drain and cleanup queued',
                        )
                      }
                    >
                      <Trash2 size={14} />
                      Drain and delete
                    </button>
                    {(node.document.fault as string) !== 'none' && (
                      <button
                        className="secondary full"
                        disabled={busy}
                        onClick={() =>
                          act(
                            () =>
                              unwrap(
                                e.setSimulationFault({
                                  path: { identity: node.resource_id },
                                  query: { fault: 'none' },
                                }),
                              ),
                            'Simulation fault cleared',
                          )
                        }
                      >
                        Clear simulated fault
                      </button>
                    )}
                  </>
                )}
                {node.kind === 'dataset-manifest' && (
                  <>
                    <div className="notice">
                      Dataset preparation is supplied by Stream A. Source manifests are not
                      executable datasets.
                    </div>
                    <ConfigurationDetails kind={node.kind} document={data} />
                  </>
                )}
                {node.kind === 'instance' && (
                  <ConfigurationDetails kind={node.kind} document={node.document} />
                )}
              </>
            )}
          </Tabs.Content>
          {isSpec && (
            <Tabs.Content value="configure">
              {node.provenance === 'historical' ? (
                <div className="notice">
                  This snapshot is historical and cannot be edited. Return to current topology to
                  edit configuration.
                </div>
              ) : (
                <>
                  <div className="notice">
                    Save a draft, then publish a new immutable revision. Existing runs and
                    assignments keep their original references.
                  </div>
                  <div className="split">
                    <span className="resource-ref">
                      Revision {(base.current_revision || 0) + 1} draft
                    </span>
                    <button
                      className="text-button"
                      onClick={() => {
                        setLocalError('');
                        if (advanced) {
                          try {
                            setDocument(JSON.parse(raw));
                            setAdvanced(false);
                          } catch {
                            setLocalError('Fix the JSON syntax before returning to the form.');
                          }
                        } else {
                          setRaw(JSON.stringify(document, null, 2));
                          setAdvanced(true);
                        }
                      }}
                    >
                      <FileJson size={14} />
                      {advanced ? 'Form editor' : 'Advanced JSON'}
                    </button>
                  </div>
                  {advanced ? (
                    <label>
                      Advanced configuration
                      <textarea
                        className="code-editor"
                        value={raw}
                        onChange={(v) => setRaw(v.target.value)}
                        spellCheck={false}
                      />
                    </label>
                  ) : (
                    <>
                      {newResource && (
                        <label>
                          Stable ID
                          <input
                            value={document.id || ''}
                            onChange={(v) => setDocument({ ...document, id: v.target.value })}
                            placeholder="customer-name"
                          />
                        </label>
                      )}
                      {(fields[node.kind] || []).map((field) => (
                        <label key={field.path}>
                          {field.label}
                          {field.options ? (
                            <select
                              value={read(document, field.path) ?? ''}
                              onChange={(v) =>
                                setDocument(assign(document, field.path, v.target.value))
                              }
                            >
                              {field.options.map((value) => (
                                <option key={value}>{value}</option>
                              ))}
                            </select>
                          ) : (
                            <input
                              type={field.type || 'text'}
                              step="any"
                              value={read(document, field.path) ?? ''}
                              onChange={(v) =>
                                setDocument(
                                  assign(
                                    document,
                                    field.path,
                                    field.type === 'number'
                                      ? Number(v.target.value)
                                      : v.target.value || null,
                                  ),
                                )
                              }
                            />
                          )}
                        </label>
                      ))}
                      {node.kind === 'scenario' && (
                        <label>
                          Participant revisions (comma separated)
                          <input
                            value={document.participants?.join(', ') || ''}
                            onChange={(v) =>
                              setDocument({
                                ...document,
                                participants: v.target.value.split(',').map((v) => v.trim()),
                              })
                            }
                          />
                        </label>
                      )}
                      <label>
                        Notes
                        <textarea
                          value={document.notes || ''}
                          onChange={(v) => setDocument({ ...document, notes: v.target.value })}
                        />
                      </label>
                    </>
                  )}
                  <div className="editor-state" aria-live="polite">
                    <span className={`status-dot ${dirty ? 'queued' : 'ready'}`} />
                    {base.version === 0
                      ? 'New specification · save a draft to begin'
                      : dirty
                        ? 'Unsaved changes'
                        : base.draft
                          ? 'Draft saved · ready to publish'
                          : 'Published settings · edits create a new revision'}
                  </div>
                  {localError && (
                    <div className="notice error" role="alert">
                      {localError}
                    </div>
                  )}
                  <div className="button-row">
                    <button className="secondary" disabled={busy} onClick={save}>
                      {busy ? <Loader2 className="spin" size={14} /> : <Save size={14} />}
                      Save draft
                    </button>
                    <button
                      className="primary"
                      disabled={
                        busy || base.version === 0 || !(base.draft || saved?.draft) || dirty
                      }
                      title={
                        dirty
                          ? 'Save draft changes before publishing'
                          : 'Publish a new immutable revision'
                      }
                      onClick={publish}
                    >
                      <Check size={14} />
                      Publish revision
                    </button>
                  </div>
                  {!newResource && (
                    <a
                      className="export-link"
                      href={`/api/v1/${selectedApi.path}/${node.resource_id}/export`}
                      download
                    >
                      Export published YAML <ExternalLink size={13} />
                    </a>
                  )}
                </>
              )}
            </Tabs.Content>
          )}
          <Tabs.Content value="history">
            {history.isLoading ? (
              <p className="muted">Loading revision history…</p>
            ) : (
              history.data?.map((rev) => (
                <div className="detail-card" key={rev.current_revision}>
                  <div className="split">
                    <strong>Revision {rev.current_revision}</strong>
                    <Badge value="published" />
                  </div>
                  <p>{rev.document?.notes}</p>
                  <details>
                    <summary>Review this revision</summary>
                    <ConfigurationDetails kind={node.kind} document={rev.document} />
                  </details>
                </div>
              ))
            )}
          </Tabs.Content>
          <Tabs.Content value="activity">
            {activeOperations.length ? (
              activeOperations.map((o) => <OperationCard key={o.operation.id} value={o} />)
            ) : (
              <div className="empty-state">
                <span>◇</span>
                <h3>No operations yet</h3>
                <p>Lifecycle actions and their progress will appear here.</p>
              </div>
            )}
          </Tabs.Content>
        </div>
      </Tabs.Root>
    </aside>
  );
}
export function OperationCard({ value }: { value: OperationView }) {
  const op = value.operation;
  return (
    <section className="detail-card operation-card">
      <div className="split">
        <strong>{op.type.replaceAll('_', ' ')}</strong>
        <Badge value={op.status} />
      </div>
      <p>{op.target.resource_id}</p>
      {op.cleanup_pending && (
        <div className="notice error">
          Cleanup pending. Resource termination remains unconfirmed.
        </div>
      )}
      {op.error && <p className="error-text">{op.error.message}</p>}
      <ol className="timeline">
        {value.events.map((event, i) => (
          <li key={i}>
            <span />
            {event.message}
            <small>{formatDate(event.created_at)}</small>
          </li>
        ))}
      </ol>
    </section>
  );
}
export function formatDate(value: unknown) {
  return value
    ? new Date(String(value)).toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })
    : '—';
}
function Assignment({ customer, act, busy }: { customer: Doc; act: Act; busy: boolean }) {
  const assignment = useQuery({
    queryKey: ['assignments'],
    queryFn: () => unwrap(e.listAssignments()),
  });
  const original = assignment.data?.find((a) => a.customer_ref.split('@')[0] === customer.id);
  const [value, setValue] = useState<Doc | null>(null);
  const current = value || original;
  return (
    <details className="assignment-form">
      <summary>
        Serving assignment <ChevronRight size={14} />
      </summary>
      {current ? (
        <>
          {['customer_ref', 'serving_profile_ref', 'worker_group_ref', 'deployment_id'].map(
            (field) => (
              <label key={field}>
                {field.replaceAll('_', ' ')}
                <input
                  value={current[field as keyof typeof current] || ''}
                  onChange={(v) => setValue({ ...current, [field]: v.target.value })}
                />
              </label>
            ),
          )}
          <button
            className="secondary full"
            disabled={busy}
            onClick={() =>
              act(
                () =>
                  unwrap(
                    e.saveAssignment({
                      path: { customer_id: customer.id },
                      body: current as never,
                    }),
                  ),
                'Assignment updated',
              )
            }
          >
            Update assignment
          </button>
        </>
      ) : (
        <button
          className="secondary"
          onClick={() =>
            setValue({
              customer_ref: `${customer.id}@${customer.revision}`,
              serving_profile_ref: '',
              worker_group_ref: '',
              deployment_id: '',
            })
          }
        >
          Configure assignment
        </button>
      )}
    </details>
  );
}
export function RunLauncher({
  scenario,
  groups,
  act,
  busy,
}: {
  scenario: Doc;
  groups: GroupView[];
  act: Act;
  busy: boolean;
}) {
  const [outcome, setOutcome] = useState('passing');
  const [report, setReport] = useState<Doc | null>(null);
  const assignments = useQuery({
    queryKey: ['assignments'],
    queryFn: () => unwrap(e.listAssignments()),
  });
  const deployments = useQuery({
    queryKey: ['deployments'],
    queryFn: () => unwrap(e.listServingDeployments()),
    refetchInterval: 2000,
  });
  const [chosen, setChosen] = useState<Record<string, string>>({});
  const relevant = (assignments.data || []).filter((a) =>
    scenario.participants?.includes(a.customer_ref),
  );
  const refs = [...new Set(relevant.map((a) => a.worker_group_ref))];
  const selectedGroups = {
    ...Object.fromEntries(
      relevant.flatMap((a) => {
        const deployment = deployments.data?.find((d) => d.id === a.deployment_id);
        return deployment ? [[a.worker_group_ref, deployment.group_id]] : [];
      }),
    ),
    ...chosen,
  };
  const simulationReady =
    relevant.length === (scenario.participants?.length || 0) &&
    relevant.length > 0 &&
    relevant.every(
      (a) =>
        deployments.data?.some(
          (d) =>
            d.id === a.deployment_id &&
            d.status === 'ready' &&
            d.group_id === selectedGroups[a.worker_group_ref],
        ) &&
        groups.some(
          (g) => g.id === selectedGroups[a.worker_group_ref] && g.group.status.phase === 'ready',
        ),
    );
  const body = {
    scenario_ref: `${scenario.id}@${scenario.revision}`,
    mode: 'simulation' as const,
    outcome: outcome as 'passing',
    group_ids: selectedGroups,
  };
  return (
    <>
      <h4>Run this scenario</h4>
      <div className="notice synthetic">
        Simulation uses compressed timing and labelled fixture results. Real execution remains
        subject to preflight.
      </div>
      {refs.map((ref) => (
        <label key={ref}>
          Runtime group for {ref}
          <select
            value={selectedGroups[ref] || ''}
            onChange={(v) => setChosen({ ...chosen, [ref]: v.target.value })}
          >
            <option value="">Select a ready group</option>
            {groups
              .filter(
                (g) =>
                  `${g.group.spec.id}@${g.group.spec.revision}` === ref &&
                  g.group.status.phase === 'ready',
              )
              .map((g) => (
                <option key={g.id} value={g.id}>
                  {g.id}
                </option>
              ))}
          </select>
        </label>
      ))}
      <label>
        Result preset
        <select value={outcome} onChange={(v) => setOutcome(v.target.value)}>
          {['passing', 'violating', 'insufficient_data', 'invalid', 'failed'].map((o) => (
            <option key={o} value={o}>
              {o.replaceAll('_', ' ')}
            </option>
          ))}
        </select>
      </label>
      <div className="button-row">
        <button
          className="secondary"
          disabled={busy}
          onClick={() => act(async () => setReport(await unwrap(e.preflightRun({ body }))))}
        >
          Check readiness
        </button>
        <button
          className="primary"
          disabled={busy || !simulationReady}
          onClick={() =>
            act(
              () => unwrap(e.startRun({ headers: mutationHeaders(), body })),
              'Simulated run queued',
            )
          }
        >
          <Play size={14} />
          Simulate run
        </button>
      </div>
      {!simulationReady && (
        <div className="notice" style={{ marginTop: 14 }}>
          Choose ready groups and deploy the assigned serving profiles before launching a
          simulation.
        </div>
      )}
      {report && (
        <div className="readiness">
          <h4>Real execution blocked</h4>
          {[...report.contract_blockers, ...report.runtime_blockers].map((reason: string) => (
            <p key={reason}>
              <span>○</span>
              {reason}
            </p>
          ))}
        </div>
      )}
    </>
  );
}
