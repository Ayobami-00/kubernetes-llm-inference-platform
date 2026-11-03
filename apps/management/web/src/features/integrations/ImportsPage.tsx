import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Check, FileUp, Loader2 } from 'lucide-react';
import { endpoints as e, unwrap, mutationHeaders } from '../../api/client';
import type {
  InventoryImport,
  DatasetImport,
  PreparedImport,
  RunImport,
  ExternalCapacityImport,
} from '../../api/client';
import { Badge, formatDate } from '../../components/Inspector';
import type { Act } from '../../components/Inspector';

const kinds = {
  inventory: 'Infrastructure inventory',
  'external-capacity': 'B-managed GPU capacity',
  datasets: 'Dataset manifest',
  'prepared-runs': 'Prepared run bundle',
  runs: 'Historical run',
};
type Kind = keyof typeof kinds;
export default function ImportsPage({ act, busy }: { act: Act; busy: boolean }) {
  const [kind, setKind] = useState<Kind>('inventory');
  const [source, setSource] = useState('');
  const [provenance, setProvenance] = useState<'unverified' | 'synthetic' | 'measured'>(
    'unverified',
  );
  const [raw, setRaw] = useState('');
  const [preview, setPreview] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState('');
  const [advanced, setAdvanced] = useState(false);
  const imports = useQuery({
    queryKey: ['imports'],
    queryFn: () => unwrap(e.listImports()),
    refetchInterval: 1500,
  });
  const prepared = useQuery({
    queryKey: ['prepared-runs'],
    queryFn: () => unwrap(e.listPreparedRuns()),
    refetchInterval: 3000,
  });
  function review(text: string) {
    setError('');
    setPreview(null);
    try {
      if (new TextEncoder().encode(text).length > 5 * 1024 * 1024)
        throw Error('The document exceeds 5 MiB.');
      const doc = JSON.parse(text);
      if (!doc || Array.isArray(doc) || typeof doc !== 'object')
        throw Error('Select one JSON document.');
      setRaw(text);
      setPreview(doc);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : 'Invalid document');
    }
  }
  async function apply() {
    const base = { document: preview, source, provenance };
    await act(async () => {
      if (kind === 'inventory')
        await unwrap(
          e.importInventory({ headers: mutationHeaders(), body: base as InventoryImport }),
        );
      else if (kind === 'external-capacity')
        await unwrap(
          e.importExternalCapacity({
            headers: mutationHeaders(),
            body: base as ExternalCapacityImport,
          }),
        );
      else if (kind === 'datasets')
        await unwrap(e.importDataset({ headers: mutationHeaders(), body: base as DatasetImport }));
      else if (kind === 'prepared-runs')
        await unwrap(
          e.importPreparedRun({ headers: mutationHeaders(), body: base as PreparedImport }),
        );
      else
        await unwrap(
          e.importHistoricalRun({ headers: mutationHeaders(), body: base as RunImport }),
        );
      setPreview(null);
      setRaw('');
    }, 'Import accepted. Follow verification below.');
  }
  return (
    <section className="list-page integration-page" tabIndex={0} aria-label="Imports">
      <div className="section-title">
        <div>
          <h3>Bring the platform together</h3>
          <p>Import A and B outputs with their source, exact revisions and historical evidence.</p>
        </div>
        <FileUp size={21} />
      </div>
      <div className="import-layout">
        <div className="import-form">
          <h4>New import</h4>
          <label>
            Document type
            <select
              value={kind}
              onChange={(event) => {
                setKind(event.target.value as Kind);
                setPreview(null);
              }}
            >
              {Object.entries(kinds).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Source name
            <input
              placeholder="Stream A · prepared export"
              value={source}
              maxLength={200}
              onChange={(event) => setSource(event.target.value)}
            />
          </label>
          <label>
            Source provenance
            <select
              value={provenance}
              onChange={(event) => setProvenance(event.target.value as typeof provenance)}
            >
              <option value="unverified">Unverified external record</option>
              <option value="synthetic">Explicitly synthetic fixture</option>
              <option value="measured">Source reports real measurements</option>
            </select>
          </label>
          <label className="import-upload">
            <FileUp size={22} />
            <span>Choose a JSON document</span>
            <small>Metadata only · 5 MiB maximum</small>
            <input
              aria-label="Import JSON file"
              type="file"
              key={kind}
              accept=".json,application/json"
              onChange={async (event) => {
                const file = event.target.files?.[0];
                setPreview(null);
                if (file) {
                  if (file.size > 5 * 1024 * 1024) {
                    setError('The document exceeds 5 MiB.');
                    return;
                  }
                  review(await file.text());
                }
              }}
            />
          </label>
          <button className="text-button" onClick={() => setAdvanced(!advanced)}>
            Paste a document instead
          </button>
          {advanced && (
            <>
              <label>
                Import document
                <textarea
                  aria-label="Import document"
                  className="code-editor"
                  spellCheck={false}
                  value={raw}
                  onChange={(event) => {
                    setRaw(event.target.value);
                    setPreview(null);
                  }}
                />
              </label>
              <button className="secondary" onClick={() => review(raw)}>
                Review document
              </button>
            </>
          )}
          {error && (
            <div className="notice error" role="alert">
              {error}
            </div>
          )}
          <div className="notice">
            Large datasets and artifacts stay on the shared filesystem. Importing a record does not
            make it executable or adopt cloud resources.
          </div>
        </div>
        <div className="import-review">
          <h4>Review before importing</h4>
          {preview ? (
            <>
              <dl className="settings-list">
                <div className="setting-row">
                  <dt>Document</dt>
                  <dd>{kinds[kind]}</dd>
                </div>
                <div className="setting-row">
                  <dt>Identity</dt>
                  <dd>
                    {String(
                      preview.id ||
                        preview.snapshot_id ||
                        preview.allocation?.cluster_id ||
                        'Not specified',
                    )}
                  </dd>
                </div>
                <div className="setting-row">
                  <dt>Schema</dt>
                  <dd>
                    {String(
                      preview.schema_version ||
                        preview.snapshot?.schema_version ||
                        'Shared contract',
                    )}
                  </dd>
                </div>
                <div className="setting-row">
                  <dt>Execution</dt>
                  <dd>
                    {String(
                      preview.status ||
                        (preview.fixture ? 'Synthetic fixture' : 'Not yet executed'),
                    )}
                  </dd>
                </div>
                {preview.result && (
                  <div className="setting-row">
                    <dt>Supplied SLO verdict</dt>
                    <dd>
                      <Badge value={preview.result.slo_verdict || 'not_evaluated'} />
                    </dd>
                  </div>
                )}
                <div className="setting-row">
                  <dt>Source</dt>
                  <dd>{source || 'Enter a source name'}</dd>
                </div>
                <div className="setting-row">
                  <dt>Provenance</dt>
                  <dd>
                    <Badge value={preview.fixture ? 'synthetic' : provenance} />
                  </dd>
                </div>
              </dl>
              <p className="section-note">
                The backend validates the shared document, resolves conflicts and checks local
                artifact checksums. Published content and old run snapshots remain immutable.
              </p>
              <button className="primary full" disabled={busy || !source.trim()} onClick={apply}>
                {busy ? <Loader2 size={15} className="spin" /> : <Check size={15} />}Confirm import
              </button>
            </>
          ) : (
            <div className="import-placeholder">
              <FileUp size={30} />
              <p>Choose a document to review its identity and provenance.</p>
            </div>
          )}
        </div>
      </div>
      <div className="section-title">
        <div>
          <h3>Import activity</h3>
          <p>Durable verification and publication, including failures requiring attention.</p>
        </div>
      </div>
      {imports.isLoading ? (
        <p role="status">Loading imports…</p>
      ) : imports.error ? (
        <div className="notice error" role="alert">
          Import activity could not be loaded.
        </div>
      ) : !imports.data?.length ? (
        <div className="notice">No imports yet.</div>
      ) : (
        imports.data.map((item) => (
          <article className="detail-card import-receipt" key={item.id}>
            <div className="split">
              <strong>
                {kinds[item.kind as Kind] || item.kind} · {item.source}
              </strong>
              <Badge value={item.status} />
            </div>
            <small>
              {item.id} · {formatDate(item.created_at)}
            </small>
            <p>
              Source provenance: {item.provenance}
              {item.result?.resource_id ? ' · ' + String(item.result.resource_id) : ''}
            </p>
            {item.error && <div className="notice error">{item.error}</div>}
            {Array.isArray(item.result?.artifacts) && (
              <p>
                {item.result.artifacts.length} artifact reference(s) checked. Inspect availability
                in Integrations.
              </p>
            )}
          </article>
        ))
      )}
      {prepared.error && (
        <div className="notice error" role="alert">
          Prepared bundles could not be loaded.
        </div>
      )}
      {!!prepared.data?.length && (
        <>
          <h4>Prepared execution bundles</h4>
          {prepared.data.map((item) => (
            <article className="detail-card" key={item.id}>
              <div className="split">
                <strong>{item.id}</strong>
                <Badge value={item.fixture ? 'synthetic' : 'prepared'} />
              </div>
              <p>
                {item.snapshot.scenario.id}@{item.snapshot.scenario.revision} · {item.source}
              </p>
              <small>Frozen snapshot {item.digest.slice(0, 16)}…</small>
            </article>
          ))}
        </>
      )}
    </section>
  );
}
