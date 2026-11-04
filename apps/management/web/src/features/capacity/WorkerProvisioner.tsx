import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Cpu } from 'lucide-react';
import { endpoints as e, mutationHeaders, unwrap } from '../../api/client';
import type { Capacity, GroupView, WorkerLifecycleOptions } from '../../api/client';
import type { Act } from '../../components/Inspector';

export function WorkerProvisionForm({
  options,
  capacity,
  current,
  groups,
  busy,
  act,
}: {
  options: WorkerLifecycleOptions;
  capacity?: Capacity;
  current: boolean;
  groups: GroupView[];
  busy: boolean;
  act: Act;
}) {
  const [groupId, setGroupId] = useState('');
  const [selectedRef, setSelectedRef] = useState('');
  const selected = options.compatible_specs?.find(
    (spec) => spec.spec_ref === (selectedRef || options.compatible_specs?.[0]?.spec_ref),
  );
  const offering = capacity?.instance_types?.find((item) => item.name === options.instance_type);
  const price = offering?.price_cents_per_hour;
  const priceKnown = typeof price === 'number' && Number.isFinite(price) && price > 0;
  const regionAvailable =
    Array.isArray(offering?.regions_with_capacity_available) &&
    offering.regions_with_capacity_available.some(
      (region) => region && typeof region === 'object' && region.name === options.region,
    );
  const reserved = groups.some(
    (group) =>
      group.mode === 'real' &&
      group.ownership !== 'external' &&
      !['deleted', 'expired'].includes(group.group.status.phase) &&
      (group.group.status.counts.allocated_instances > 0 ||
        ['allocating', 'joining', 'draining'].includes(group.group.status.phase)),
  );
  const allowed =
    options.configured &&
    current &&
    regionAvailable &&
    priceKnown &&
    price <= (options.price_ceiling_cents_per_hour || 0) &&
    Boolean(selected) &&
    !reserved;
  const dollars = (cents: number) =>
    new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100);
  if (!options.configured)
    return <div className="notice">{options.reason || 'Live provisioning is unavailable.'}</div>;
  return (
    <section className="detail-card" aria-label="Provision live Lambda worker">
      <div className="section-title">
        <div>
          <h4>Provision a live GPU worker</h4>
          <p>
            One A10 for this stage. Networking, Kubernetes and GPU readiness are checked before use.
          </p>
        </div>
        <Cpu size={22} />
      </div>
      <div className="form-grid">
        <label>
          GPU instance type
          <select value={options.instance_type || ''} disabled>
            <option value={options.instance_type || ''}>{options.instance_type}</option>
          </select>
        </label>
        <label>
          Region
          <select value={options.region || ''} disabled>
            <option value={options.region || ''}>{options.region} · Virginia</option>
          </select>
        </label>
        <label>
          Worker profile &amp; lease
          <select
            value={selected?.spec_ref || ''}
            onChange={(event) => setSelectedRef(event.target.value)}
          >
            {!options.compatible_specs?.length && <option value="">No compatible profile</option>}
            {options.compatible_specs?.map((spec) => (
              <option key={spec.spec_ref} value={spec.spec_ref}>
                {spec.spec_ref} · {spec.lease_minutes} minutes
              </option>
            ))}
          </select>
        </label>
        <label>
          Group name
          <input
            value={groupId}
            onChange={(event) => setGroupId(event.target.value)}
            placeholder="writer-a10"
            maxLength={63}
          />
        </label>
      </div>
      {selected && priceKnown && (
        <p>
          {dollars(price)} / instance-hour · estimated{' '}
          {dollars((price * selected.lease_minutes) / 60)} for this lease. Charges continue until
          provider termination is confirmed.
        </p>
      )}
      {!allowed && (
        <div className="notice" role="status">
          {reserved
            ? 'Existing live capacity or cleanup must be reconciled before another allocation.'
            : options.reason ||
              (!current
                ? 'Refresh provider capacity before provisioning.'
                : !regionAvailable
                  ? 'The approved A10 region has no confirmed capacity.'
                  : !priceKnown || price > (options.price_ceiling_cents_per_hour || 0)
                    ? 'The current price is unavailable or exceeds this stage’s limit.'
                    : 'Choose a compatible worker profile.')}
        </div>
      )}
      <button
        className="primary"
        disabled={busy || !allowed || !/^[a-z][a-z0-9-]{0,62}$/.test(groupId)}
        onClick={() =>
          act(
            () =>
              unwrap(
                e.createWorkerGroup({
                  headers: mutationHeaders(),
                  body: { id: groupId, spec_ref: selected!.spec_ref, mode: 'real', fault: 'none' },
                }),
              ),
            'Live worker provisioning queued',
          )
        }
      >
        Provision worker
      </button>
    </section>
  );
}

export default function WorkerProvisioner(props: {
  capacity?: Capacity;
  current: boolean;
  groups: GroupView[];
  busy: boolean;
  act: Act;
}) {
  const options = useQuery({
    queryKey: ['worker-lifecycle-options'],
    queryFn: () => unwrap(e.getWorkerLifecycleOptions()),
    refetchInterval: 15000,
  });
  if (options.error)
    return (
      <div className="notice error" role="alert">
        Live provisioning availability could not be verified.
      </div>
    );
  if (!options.data) return <p role="status">Checking live provisioning availability…</p>;
  return <WorkerProvisionForm {...props} options={options.data} />;
}
