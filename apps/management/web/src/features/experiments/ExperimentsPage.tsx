import { useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowDownToLine, FlaskConical, Loader2, Play, Square } from 'lucide-react';
import { endpoints as e, message, mutationHeaders, unwrap } from '../../api/client';
import type { ExperimentView } from '../../api/client';
import { Badge, formatDate } from '../../components/Inspector';

const terminal = new Set(['completed', 'failed', 'cancelled']);

export default function ExperimentsPage() {
  const queryClient = useQueryClient();
  const preview = useQuery({
    queryKey: ['a10-experiment-preview'],
    queryFn: () => unwrap(e.previewA10Experiment()),
    refetchInterval: 15000,
  });
  const history = useQuery({
    queryKey: ['experiments'],
    queryFn: () => unwrap(e.listExperiments()),
    refetchInterval: 2000,
  });
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef<{ key: string; sha: string } | null>(null);
  const active = history.data?.some((item) => !terminal.has(item.status));

  async function launch() {
    if (!preview.data?.definition_sha256) return;
    pending.current ||= { key: crypto.randomUUID(), sha: preview.data.definition_sha256 };
    setBusy(true);
    setError('');
    try {
      await unwrap(
        e.startA10Experiment({
          body: { definition_sha256: pending.current.sha },
          headers: { 'Idempotency-Key': pending.current.key },
        }),
      );
      pending.current = null;
      setReviewed(false);
      await queryClient.invalidateQueries({ queryKey: ['experiments'] });
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }

  async function cancel(identity: string) {
    setBusy(true);
    setError('');
    try {
      await unwrap(e.stopExperiment({ path: { identity }, headers: mutationHeaders() }));
      await queryClient.invalidateQueries({ queryKey: ['experiments'] });
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="list-page integration-page" aria-label="A10 experiments">
      <div className="section-title">
        <div>
          <h3>WriterAssist · baseline vs n-gram</h3>
          <p>A paired grammar-correction experiment on the retained A10. About 20 minutes.</p>
        </div>
        <FlaskConical size={24} />
      </div>
      <article className="detail-card">
        <p>
          Two sequential runs use the same public JFLEG inputs and traffic. Only speculative
          decoding changes. The experiment temporarily switches model serving; it never creates,
          restarts or destroys the GPU VM.
        </p>
        <p>
          Each variant: 60 seconds warmup, 300 seconds scoring and up to 30 seconds drain. Existing
          latency targets and delivery checks stay fixed. Speedup is a hypothesis.
        </p>
        {preview.isLoading && <p role="status">Checking the experiment preset…</p>}
        {preview.error && (
          <div className="notice error" role="alert">
            The experiment preset could not be loaded.
          </div>
        )}
        {preview.data?.blockers?.map((blocker) => (
          <div className="notice" key={blocker}>
            {blocker}
          </div>
        ))}
        {preview.data?.available && (
          <>
            <details open={reviewed} onToggle={(event) => setReviewed(event.currentTarget.open)}>
              <summary>Review frozen experiment settings</summary>
              <pre className="experiment-definition">
                {JSON.stringify(preview.data.summary, null, 2)}
              </pre>
            </details>
            <button className="primary" disabled={!reviewed || busy || active} onClick={launch}>
              {busy ? <Loader2 size={16} className="spin" /> : <Play size={16} />}
              Run A10 baseline vs n-gram
            </button>
          </>
        )}
        {active && <p role="status">The A10 is reserved by an active experiment.</p>}
        {error && (
          <div className="notice error" role="alert">
            {error}
          </div>
        )}
      </article>
      <div className="section-title">
        <h3>Experiment history</h3>
      </div>
      {history.isLoading && <p role="status">Loading experiment history…</p>}
      {history.error && (
        <div className="notice error" role="alert">
          Experiment history is unavailable.
        </div>
      )}
      {history.data?.length === 0 && (
        <p>No paired experiments yet. Completed runs remain available here.</p>
      )}
      {history.data?.map((item) => (
        <ExperimentCard key={item.id} item={item} busy={busy} cancel={cancel} />
      ))}
    </section>
  );
}

function ExperimentCard({
  item,
  busy,
  cancel,
}: {
  item: ExperimentView;
  busy: boolean;
  cancel: (id: string) => Promise<void>;
}) {
  const seconds = Math.max(
    0,
    (new Date(item.finished_at || Date.now()).getTime() - new Date(item.created_at).getTime()) /
      1000,
  );
  return (
    <article className="detail-card experiment-card">
      <div className="split">
        <strong>{item.id}</strong>
        <Badge value={item.status} />
      </div>
      <p>
        Stage: <strong>{item.stage.replaceAll('-', ' ')}</strong> · {Math.floor(seconds / 60)}m{' '}
        {Math.floor(seconds % 60)}s · {formatDate(item.created_at)}
      </p>
      {!terminal.has(item.status) && (
        <button disabled={busy || item.stop_requested} onClick={() => cancel(item.id)}>
          <Square size={14} />
          {item.stop_requested ? 'Cancelling…' : 'Cancel experiment'}
        </button>
      )}
      {item.error && <div className="notice error">{item.error.message}</div>}
      <div className="experiment-children">
        {Object.entries(item.children || {}).map(([variant, run]) => (
          <div key={variant}>
            <strong>{variant}</strong>{' '}
            <a href={`/api/v1/runs/${encodeURIComponent(run)}`} target="_blank" rel="noreferrer">
              {run}
            </a>
            <p>Child run, with its own immutable snapshot, result and artifacts.</p>
          </div>
        ))}
      </div>
      {item.comparison && (
        <>
          <h4>{String(item.comparison.conclusion)}</h4>
          <p>
            Execution, delivery validity, SLO attainment and correction quality are independent
            results.
          </p>
          <ComparisonTable comparison={item.comparison} />
          <details>
            <summary>Side-by-side results and diagnostics</summary>
            <pre className="experiment-definition">{JSON.stringify(item.comparison, null, 2)}</pre>
          </details>
        </>
      )}
      <div className="experiment-downloads">
        {Object.entries(item.artifacts || {}).map(
          ([name, artifact]) =>
            typeof artifact.id === 'string' && (
              <a
                className="button"
                key={name}
                href={`/api/v1/artifacts/${encodeURIComponent(artifact.id)}`}
              >
                <ArrowDownToLine size={14} />
                Download {name}
              </a>
            ),
        )}
      </div>
      <p>
        <a href="http://127.0.0.1:13000/d/klip-a10-speculation" target="_blank" rel="noreferrer">
          Open A10 Grafana diagnostics
        </a>{' '}
        (requires the private access tunnel).
      </p>
    </article>
  );
}

function valueAt(value: unknown, keys: string[]): unknown {
  for (const key of keys) {
    if (!value || typeof value !== 'object') return null;
    value = (value as Record<string, unknown>)[key];
  }
  return value;
}

export function ComparisonTable({ comparison }: { comparison: Record<string, unknown> }) {
  const rows: [string, string[]][] = [
    ['Execution', ['execution_status']],
    ['Measurement validity', ['measurement_validity']],
    ['Eligible requests', ['quality', 'coverage', 'eligible']],
    ['Completed requests', ['scored_outcomes', 'completed']],
    ['Latency samples', ['completion_latency', 'samples']],
    ['Completion p50 (ms)', ['completion_latency', 'p50_ms']],
    ['Completion p95 (ms)', ['completion_latency', 'p95_ms']],
    ['Completion p99 (ms)', ['completion_latency', 'p99_ms']],
    ['SLO verdict', ['slo_verdict']],
    ['GLEU', ['quality', 'gleu']],
    ['Quality evaluated', ['quality', 'coverage', 'evaluated']],
    ['Quality failures', ['quality', 'coverage', 'failed']],
    ['Missed admissions', ['delivery', 'missed_due_admissions']],
  ];
  const format = (value: unknown) =>
    typeof value === 'number'
      ? value.toLocaleString(undefined, { maximumFractionDigits: 4 })
      : value == null
        ? 'Unavailable'
        : String(value);
  return (
    <div className="experiment-results">
      <table>
        <thead>
          <tr>
            <th scope="col">Measurement</th>
            <th scope="col">Baseline</th>
            <th scope="col">N-gram</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, path]) => (
            <tr key={label}>
              <th scope="row">{label}</th>
              <td>{format(valueAt(comparison.baseline, path))}</td>
              <td>{format(valueAt(comparison.ngram, path))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
