import { useQuery } from '@tanstack/react-query';
import { CheckCircle2, CircleHelp, Layers3, PlugZap } from 'lucide-react';
import { endpoints as e, unwrap } from '../../api/client';
import { Badge, formatDate } from '../../components/Inspector';
export default function ReadinessPage() {
  const integrations = useQuery({
    queryKey: ['integrations'],
    queryFn: () => unwrap(e.getIntegrations()),
    refetchInterval: 5000,
  });
  const inventory = useQuery({
    queryKey: ['inventory'],
    queryFn: () => unwrap(e.listInventory()),
    refetchInterval: 5000,
  });
  const artifacts = useQuery({
    queryKey: ['artifacts'],
    queryFn: () => unwrap(e.listArtifacts()),
    refetchInterval: 5000,
  });
  return (
    <section className="list-page integration-page" tabIndex={0} aria-label="Integration readiness">
      <div className="section-title">
        <div>
          <h3>Platform readiness</h3>
          <p>Configuration, imported evidence and live observations remain separate.</p>
        </div>
        <PlugZap size={22} />
      </div>
      {integrations.isLoading && <p role="status">Loading integration status…</p>}
      {integrations.error && (
        <div className="notice error" role="alert">
          Integration status could not be loaded.
        </div>
      )}
      <div className="readiness-grid">
        {integrations.data?.checks?.map((item) => (
          <article className="detail-card readiness-check" key={item.code}>
            {item.status === 'ready' ? <CheckCircle2 size={20} /> : <CircleHelp size={20} />}
            <div>
              <div className="split">
                <strong>{item.label}</strong>
                <Badge
                  value={integrations.error && item.status === 'ready' ? 'stale' : item.status}
                />
              </div>
              <p>{item.detail}</p>
            </div>
          </article>
        ))}
      </div>
      <h4>Integration adapters</h4>
      <div className="adapter-list">
        {integrations.data?.adapters.map((item) => (
          <article className="detail-card" key={item.category}>
            <div className="split">
              <strong>{item.category.replace(/^./, (s) => s.toUpperCase())}</strong>
              <Badge value={item.configured ? 'configured' : 'unconfigured'} />
            </div>
            <dl className="settings-list">
              <div className="setting-row">
                <dt>Simulation</dt>
                <dd className="technical-value">{item.simulation}</dd>
              </div>
              <div className="setting-row">
                <dt>Real integration</dt>
                <dd className="technical-value">{item.real}</dd>
              </div>
            </dl>
          </article>
        ))}
      </div>
      <div className="section-title">
        <div>
          <h3>Infrastructure evidence</h3>
          <p>Partial foundation observations never establish a complete Kubernetes inventory.</p>
        </div>
        <Layers3 size={20} />
      </div>
      {inventory.error ? (
        <div className="notice error" role="alert">
          Infrastructure evidence could not be loaded.
        </div>
      ) : inventory.isLoading ? (
        <p role="status">Loading infrastructure evidence…</p>
      ) : !inventory.data?.length ? (
        <div className="notice">
          No infrastructure imports yet. Import B's foundation observations or full inventory.
        </div>
      ) : (
        inventory.data.map((item) => (
          <article className="detail-card" key={item.id}>
            <div className="split">
              <strong>{item.cluster_id}</strong>
              <Badge
                value={item.fixture ? 'synthetic' : item.complete ? 'full_inventory' : 'partial'}
              />
            </div>
            <p>
              {item.source} · captured {formatDate(item.captured_at)}
            </p>
            <small>
              {item.id} ·{' '}
              {item.current ? 'Current observation for this source type' : 'Historical observation'}
            </small>
          </article>
        ))
      )}
      <div className="section-title">
        <div>
          <h3>Artifact availability</h3>
          <p>
            Only registered, verified files inside the configured shared root can be downloaded.
          </p>
        </div>
      </div>
      <p className="artifact-root">
        Shared root: <code>{integrations.data?.artifacts_root || 'Not configured'}</code>
      </p>
      {artifacts.error ? (
        <div className="notice error" role="alert">
          Artifact availability could not be loaded.
        </div>
      ) : artifacts.isLoading ? (
        <p role="status">Loading artifact availability…</p>
      ) : !artifacts.data?.length ? (
        <div className="notice">No artifact references have been imported.</div>
      ) : (
        artifacts.data.map((item) => (
          <article className="detail-card" key={item.id}>
            <div className="split">
              <strong className="artifact-uri">{item.uri}</strong>
              <Badge value={item.status} />
            </div>
            <p>
              {item.error || 'Checksum verified'} · {formatDate(item.checked_at)}
            </p>
            {item.status === 'verified' && (
              <a className="text-button" href={'/api/v1/artifacts/' + item.id} download>
                Download verified artifact →
              </a>
            )}
          </article>
        ))
      )}
    </section>
  );
}
