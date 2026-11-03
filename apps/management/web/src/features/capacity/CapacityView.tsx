import { useEffect, useState } from 'react';
import {
  CircleCheck,
  CircleHelp,
  CircleMinus,
  Clock3,
  Cpu,
  Loader2,
  MapPin,
  RefreshCw,
} from 'lucide-react';
import type { Capacity } from '../../api/client';
import { Badge, formatDate } from '../../components/Inspector';

type Region = { name: string; description?: string };
type InstanceType = {
  name: string;
  label: string;
  gpus?: number;
  vcpus?: number;
  memory?: number;
  price?: number;
  regions: Region[] | null;
};
type Availability = 'available' | 'unavailable' | 'unconfirmed';
const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const string = (value: unknown) => (typeof value === 'string' && value.trim() ? value : undefined);
const numeric = (value: unknown) =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined;
function instanceType(item: Record<string, unknown>, index: number): InstanceType {
  const specs = object(item.specs);
  const raw = item.regions_with_capacity_available;
  const regions = Array.isArray(raw)
    ? raw.flatMap((value) => {
        const region = object(value),
          name = string(region.name);
        return name ? [{ name, description: string(region.description) }] : [];
      })
    : [];
  const name = string(item.name) || 'instance-type-' + index;
  return {
    name,
    label: string(item.gpu_description) || string(item.description) || name,
    gpus: numeric(specs.gpus),
    vcpus: numeric(specs.vcpus),
    memory: numeric(specs.memory_gib),
    price: numeric(item.price_cents_per_hour),
    regions:
      Array.isArray(raw) && (!raw.length || regions.length)
        ? [...new Map(regions.map((region) => [region.name, region])).values()]
        : null,
  };
}
function availability(item: InstanceType, current: boolean): Availability {
  return !current || item.regions === null
    ? 'unconfirmed'
    : item.regions.length
      ? 'available'
      : 'unavailable';
}
function CapacityCard({ item, current }: { item: InstanceType; current: boolean }) {
  const state = availability(item, current);
  const stateLabel = {
    available: 'Available',
    unavailable: 'Unavailable',
    unconfirmed: 'Unconfirmed',
  }[state];
  const Icon = { available: CircleCheck, unavailable: CircleMinus, unconfirmed: Clock3 }[state];
  const quantity = (value?: number, suffix = '') =>
    value === undefined ? 'Not reported' : value.toLocaleString() + suffix;
  return (
    <article
      className={'capacity-card is-' + state}
      aria-label={item.label + ' · ' + stateLabel}
      data-instance-type={item.name}
      data-availability={state}
    >
      <div className="capacity-card-heading">
        <span className="capacity-card-icon">
          <Cpu size={23} />
        </span>
        <span className={'availability-pill ' + state}>
          <Icon size={13} />
          {stateLabel}
        </span>
      </div>
      <h5>{item.label}</h5>
      <code className="capacity-sku">{item.name}</code>
      <div className="capacity-price">
        <strong>
          {item.price === undefined
            ? 'Price not reported'
            : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(
                item.price / 100,
              )}
        </strong>
        {item.price !== undefined && <span>USD / instance-hour</span>}
      </div>
      <dl className="capacity-specs">
        <div>
          <dt>GPUs</dt>
          <dd>{quantity(item.gpus)}</dd>
        </div>
        <div>
          <dt>vCPUs</dt>
          <dd>{quantity(item.vcpus)}</dd>
        </div>
        <div>
          <dt>System RAM</dt>
          <dd>{quantity(item.memory, ' GiB')}</dd>
        </div>
      </dl>
      <div className="capacity-regions">
        <small>
          {state === 'available'
            ? 'Available regions'
            : state === 'unavailable'
              ? 'Regional capacity'
              : 'Last reported regions'}
        </small>
        {item.regions?.length ? (
          <ul aria-label={state === 'available' ? 'Available regions' : 'Last reported regions'}>
            {item.regions.map((region) => (
              <li key={region.name} title={region.description}>
                <MapPin size={11} />
                {region.name}
              </li>
            ))}
          </ul>
        ) : (
          <p>
            {state === 'unavailable'
              ? 'No regions currently report capacity.'
              : 'No regional capacity confirmed.'}
          </p>
        )}
      </div>
    </article>
  );
}
function CatalogueSection({
  id,
  title,
  description,
  items,
  current,
}: {
  id: string;
  title: string;
  description: string;
  items: InstanceType[];
  current: boolean;
}) {
  if (!items.length) return null;
  return (
    <section className="capacity-group" aria-labelledby={'capacity-' + id}>
      <header className="capacity-group-heading">
        <div>
          <h4 id={'capacity-' + id}>
            {title}
            <span className="count-chip">{items.length}</span>
          </h4>
          <p>{description}</p>
        </div>
      </header>
      <div className="capacity-grid">
        {items.map((item) => (
          <CapacityCard key={item.name} item={item} current={current} />
        ))}
      </div>
    </section>
  );
}
export default function CapacityView({
  data,
  busy,
  refresh,
  requestError,
}: {
  data?: Capacity;
  busy: boolean;
  refresh: () => void;
  requestError?: string;
}) {
  const observedAt = Date.parse(data?.observed_at || '');
  const [, checkFreshness] = useState(0);
  useEffect(() => {
    // The API marks observations stale after 90 seconds. Expire the presentation
    // at that boundary too, even if subsequent API requests cannot complete.
    const remaining = observedAt + 90_000 - Date.now();
    if (!Number.isFinite(remaining) || remaining <= 0 || remaining > 120_000) return;
    const timer = window.setTimeout(() => checkFreshness((value) => value + 1), remaining + 1);
    return () => window.clearTimeout(timer);
  }, [observedAt]);
  const error = requestError || data?.error;
  const age = Date.now() - observedAt;
  const current =
    data?.status === 'fresh' && !error && Number.isFinite(age) && age >= -30_000 && age < 90_000;
  const types = (data?.instance_types || [])
    .map(instanceType)
    .sort((a, b) => a.label.localeCompare(b.label) || a.name.localeCompare(b.name));
  const gpus = types.filter((item) => item.gpus !== undefined && item.gpus > 0);
  const other = types
    .filter((item) => item.gpus === undefined || item.gpus === 0)
    .sort((a, b) => {
      const rank = { available: 0, unconfirmed: 1, unavailable: 2 };
      return rank[availability(a, current)] - rank[availability(b, current)];
    });
  const available = gpus.filter((item) => availability(item, current) === 'available');
  const unconfirmed = gpus.filter((item) => availability(item, current) === 'unconfirmed');
  const unavailable = gpus.filter((item) => availability(item, current) === 'unavailable');
  return (
    <div className="list-page capacity-page">
      <div className="section-title capacity-title">
        <div>
          <h3>Lambda Cloud observations</h3>
          <p>
            {data?.observed_at ? 'Last observed ' + formatDate(data.observed_at) + ' · ' : ''}
            Regional availability is not reserved capacity.
          </p>
        </div>
        <button className="secondary small" disabled={busy} onClick={refresh}>
          <RefreshCw size={14} className={busy ? 'spin' : undefined} />
          Refresh
        </button>
      </div>
      {error && (
        <div className="notice error" role="alert">
          {error}
          {data?.observed_at && ' · Previous observations are retained.'}
        </div>
      )}
      {!data ? (
        !error && (
          <div className="capacity-loading">
            <Loader2 className="spin" />
            <p>Loading Lambda observations…</p>
          </div>
        )
      ) : data.status === 'unconfigured' ? (
        <div className="connection-empty">
          <div className="login-icon">
            <Cpu size={27} />
          </div>
          <h3>Connect your Lambda account</h3>
          <p>
            Add a Lambda API key through the server secret to see live GPU types, regional
            availability and your account instances.
          </p>
          <span className="badge">Credentials stay server-side</span>
        </div>
      ) : (
        <>
          <div className="capacity-observation-state">
            <Badge
              value={
                data.status === 'fresh' && !current ? 'unconfirmed' : data.status || 'unconfirmed'
              }
            />
            <span>
              {current
                ? 'Provider-reported availability. Capacity can change before allocation.'
                : 'Availability is unconfirmed until the next successful refresh.'}
            </span>
          </div>
          <CatalogueSection
            id="available"
            title="Available to rent"
            description="GPU options with capacity reported by Lambda."
            items={available}
            current={current}
          />
          {current && gpus.length > 0 && !available.length && (
            <div className="notice capacity-no-availability">
              <CircleHelp size={17} />
              No GPU capacity is currently reported. Unavailable options remain visible below.
            </div>
          )}
          <CatalogueSection
            id="unconfirmed"
            title="GPU availability unconfirmed"
            description="Last known specifications and regions. Refresh to confirm current capacity."
            items={unconfirmed}
            current={current}
          />
          <CatalogueSection
            id="unavailable"
            title="Unavailable GPUs"
            description="No region currently reports capacity for these GPU options."
            items={unavailable}
            current={current}
          />
          {!gpus.length && (
            <div className="notice">
              <Cpu size={17} />
              No GPU instance types were reported in this observation.
            </div>
          )}
          <CatalogueSection
            id="other"
            title="Other instance types"
            description="Non-GPU options and types without reported GPU specifications."
            items={other}
            current={current}
          />
          <section
            className="capacity-group capacity-instances"
            aria-labelledby="capacity-instances"
          >
            <header className="capacity-group-heading">
              <div>
                <h4 id="capacity-instances">
                  Account instances<span className="count-chip">{data.instances?.length || 0}</span>
                </h4>
                <p>
                  Observed account inventory. Instance status does not indicate Kubernetes or GPU
                  readiness.
                </p>
              </div>
            </header>
            {data.instances?.length ? (
              data.instances.map((item, index) => (
                <article className="detail-card" key={string(item.id) || index}>
                  <div className="split">
                    <strong>{string(item.name) || string(item.id) || 'Unnamed instance'}</strong>
                    <Badge value={string(item.status) || 'unknown'} />
                  </div>
                  <p>{string(item.id)} · observational account instance</p>
                </article>
              ))
            ) : (
              <div className="empty-state compact-empty">
                <Cpu size={26} />
                <h3>No account instances</h3>
                <p>
                  This is the provider-reported account inventory, separate from simulated groups.
                </p>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
