import type { ReactNode } from 'react';
import { ChevronDown, ExternalLink, FileCheck2, SlidersHorizontal } from 'lucide-react';

type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as RecordValue) : {};
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const at = (value: unknown, path: string): unknown =>
  path.split('.').reduce<unknown>((current, key) => record(current)[key], value);
const number = (value: unknown): number => (typeof value === 'number' ? value : 0);
const labels: Record<string, string> = {
  id: 'Identifier',
  schema_version: 'Schema version',
  revision: 'Revision',
  dataset_ref: 'Dataset',
  provider_instance_id: 'Provider instance ID',
  node_name: 'Kubernetes node',
  node_uid: 'Kubernetes node UID',
  instance_type: 'Instance type',
  gpu_description: 'GPU',
  price_cents_per_hour: 'Price (cents per hour)',
  ip: 'Public IP address',
  public_ip: 'Public IP address',
  private_ip: 'Private IP address',
  gpus: 'GPUs',
  vcpus: 'vCPUs',
  memory_gib: 'Memory (GiB)',
  storage_gib: 'Storage (GiB)',
  observed_at: 'Last observed',
  allocated_at: 'First allocation',
  expires_at: 'Lease expires',
  phase: 'Lifecycle stage',
  status: 'Status',
  ssh_key_names: 'SSH key names',
  ttft_ms: 'Time to first token (TTFT)',
  tpot_ms: 'Time per output token (TPOT)',
  completion_latency_ms: 'Request completion latency',
  session_latency_ms: 'Session latency',
};
const terms: Record<string, string> = {
  sentence_correction: 'Sentence correction',
  independent_requests: 'Independent requests',
  session: 'Response-dependent sessions',
  best_effort: 'Best effort',
  dedicated: 'Dedicated',
  provisioned: 'Provisioned',
  vllm: 'vLLM',
  lambda: 'Lambda Cloud',
  nvidia: 'NVIDIA',
  poisson: 'Poisson arrivals',
  constant: 'Constant arrivals',
  load_aware: 'Load-aware routing',
  cache_aware: 'Cache-aware routing',
  not_evaluated: 'Not evaluated',
  not_provided: 'Not provided',
  experimental_assumption: 'Experimental assumption',
  recorded_trace: 'Recorded trace',
  custom_single_turn: 'Single-turn requests',
  custom_multi_turn: 'Multi-turn conversations',
  exgentic_snapshot: 'Exgentic recorded-context replay',
  multiple_corrections: 'Multiple fluency references',
  dialogue_annotations: 'Dialogue annotations',
  clause_spans: 'Clause spans',
  answers_and_supporting_facts: 'Answers and supporting facts',
  not_available: 'Not available',
  jfleg_gleu: 'JFLEG GLEU',
  hotpotqa_em_f1: 'HotpotQA exact match / F1',
  extraction_f1: 'Extraction F1',
  tutoring_reference_review: 'Tutoring reference review',
  pod_delete: 'Pod deletion',
  ngram: 'N-gram lookup',
};
export function readableLabel(key: string): string {
  return (
    labels[key] ||
    key
      .replaceAll('_', ' ')
      .replaceAll('-', ' ')
      .replace(/^./, (value) => value.toUpperCase())
  );
}
function text(value: unknown): string {
  return typeof value === 'string' ? terms[value] || value : String(value);
}
function Quantity({ value, unit }: { value: unknown; unit: string }) {
  return typeof value === 'number' ? (
    <>
      {value.toLocaleString()}
      <span className="value-unit"> {unit}</span>
    </>
  ) : (
    <Unset />
  );
}
function Percent({ value }: { value: unknown }) {
  return typeof value === 'number' ? (
    <>{(value * 100).toLocaleString(undefined, { maximumFractionDigits: 2 })}%</>
  ) : (
    <Unset />
  );
}
function Unset({ children = 'Not configured' }: { children?: ReactNode }) {
  return <span className="value-unset">{children}</span>;
}
function Value({ value }: { value: unknown }) {
  if (value === undefined || value === null || value === '') return <Unset />;
  if (typeof value === 'boolean')
    return (
      <span className={`boolean-value ${value ? 'enabled' : 'disabled'}`}>
        <span />
        {value ? 'Enabled' : 'Disabled'}
      </span>
    );
  if (typeof value === 'number') return <>{value.toLocaleString()}</>;
  if (Array.isArray(value))
    return value.length ? (
      <div className="value-tags">
        {value.map((item, index) => (
          <span key={index}>
            {typeof item === 'object'
              ? Object.values(record(item))
                  .filter((v) => typeof v !== 'object')
                  .map(text)
                  .join(' · ')
              : text(item)}
          </span>
        ))}
      </div>
    ) : (
      <Unset>None</Unset>
    );
  if (typeof value === 'object')
    return (
      <SettingsList
        entries={Object.entries(record(value)).map(([key, child]) => ({
          label: readableLabel(key),
          value: child,
        }))}
      />
    );
  const string = String(value);
  if (/^https?:\/\//.test(string))
    return (
      <a className="reference-link" href={string} target="_blank" rel="noreferrer noopener">
        {string}
        <ExternalLink size={12} />
      </a>
    );
  return <span>{text(string)}</span>;
}
type Entry = { label: string; value?: unknown; render?: ReactNode; mono?: boolean };
export function SettingsList({ entries }: { entries: Entry[] }) {
  return (
    <dl className="settings-list">
      {entries.map((entry) => (
        <div className="setting-row" key={entry.label}>
          <dt>{entry.label}</dt>
          <dd className={entry.mono ? 'technical-value' : undefined}>
            {entry.render !== undefined ? entry.render : <Value value={entry.value} />}
          </dd>
        </div>
      ))}
    </dl>
  );
}
function Section({
  title,
  children,
  initiallyOpen = true,
  meta,
}: {
  title: string;
  children: ReactNode;
  initiallyOpen?: boolean;
  meta?: ReactNode;
}) {
  return (
    <details className="configuration-section" open={initiallyOpen}>
      <summary>
        <span>{title}</span>
        {meta && <small>{meta}</small>}
        <ChevronDown size={14} />
      </summary>
      <div className="configuration-section-body">{children}</div>
    </details>
  );
}
function Fields({ data, fields }: { data: unknown; fields: (string | [string, string])[] }) {
  return (
    <SettingsList
      entries={fields.map((field) => {
        const [path, label] =
          typeof field === 'string' ? [field, readableLabel(field.split('.').at(-1)!)] : field;
        return { label, value: at(data, path) };
      })}
    />
  );
}
function Customer({ data }: { data: RecordValue }) {
  const workload = record(data.workload),
    generation = record(data.generation),
    limits = record(data.limits),
    allowance = record(data.traffic_allowance),
    slo = record(data.slo),
    quality = record(data.quality);
  return (
    <>
      <Section title="Service contract">
        <SettingsList
          entries={[
            { label: 'Service tier', value: data.tier },
            { label: 'Model alias', value: data.model_alias },
            {
              label: 'Reserved concurrency',
              render: <Quantity value={data.reserved_concurrency} unit="requests" />,
            },
          ]}
        />
      </Section>
      <Section title="Workload & dataset">
        <Fields
          data={workload}
          fields={[
            'task',
            ['dataset_ref', 'Dataset revision'],
            ['interaction', 'Interaction model'],
            ['preserve_recorded_context', 'Preserve recorded context'],
          ]}
        />
      </Section>
      <Section title="Request limits">
        <SettingsList
          entries={[
            {
              label: 'Rendered input ceiling',
              render: <Quantity value={limits.max_rendered_input_tokens} unit="tokens" />,
            },
            {
              label: 'Output ceiling',
              render: <Quantity value={generation.max_output_tokens} unit="tokens" />,
            },
            { label: 'Concurrent requests', value: limits.max_inflight_requests },
            {
              label: 'Request timeout',
              render: <Quantity value={limits.request_timeout_ms} unit="ms" />,
            },
          ]}
        />
      </Section>
      <Section title="SLO policy">
        <SettingsList
          entries={[
            {
              label: 'Evaluation window',
              render: <Quantity value={slo.window_s} unit="seconds" />,
            },
            { label: 'Minimum eligible samples', value: slo.minimum_eligible_requests },
          ]}
        />
        {list(slo.targets).map((value, index) => {
          const target = record(value);
          return (
            <div className="target-card" key={index}>
              <strong>{readableLabel(String(target.metric))}</strong>
              <SettingsList
                entries={[
                  {
                    label: 'Latency threshold',
                    render: <Quantity value={target.threshold_ms} unit="ms" />,
                  },
                  {
                    label: 'Required attainment',
                    render: <Percent value={target.minimum_attainment_fraction} />,
                  },
                ]}
              />
            </div>
          );
        })}
      </Section>
      <Section title="Traffic allowance" initiallyOpen={false}>
        <SettingsList
          entries={[
            { label: 'Admission unit', value: allowance.unit },
            {
              label: 'Sustained rate',
              render: <Quantity value={allowance.sustained_per_second} unit="/ second" />,
            },
            {
              label: 'Burst rate',
              render: <Quantity value={allowance.burst_per_second} unit="/ second" />,
            },
            {
              label: 'Maximum burst duration',
              render: <Quantity value={allowance.max_burst_duration_s} unit="seconds" />,
            },
          ]}
        />
      </Section>
      <Section title="Generation settings" initiallyOpen={false}>
        <Fields data={generation} fields={['temperature', 'streaming']} />
      </Section>
      <Section title="Prompt templates" initiallyOpen={false}>
        {workload.prompt_template ? (
          Object.entries(record(workload.prompt_template)).map(([key, value]) => (
            <div className="prompt-block" key={key}>
              <small>{readableLabel(key)} prompt</small>
              <p>{String(value)}</p>
            </div>
          ))
        ) : (
          <p className="section-note">
            No prompt overrides. Recorded context is preserved when configured.
          </p>
        )}
      </Section>
      <Section title="Quality evaluation intent" initiallyOpen={false}>
        <SettingsList entries={[{ label: 'Evaluator', value: quality.evaluator }]} />
        <p className="section-note">{String(quality.notes || 'No evaluation notes supplied.')}</p>
      </Section>
    </>
  );
}
function WorkerProfile({ data }: { data: RecordValue }) {
  const hardware = record(data.hardware);
  return (
    <>
      <Section title="Allocation policy">
        <Fields
          data={data}
          fields={[
            'provider',
            ['instance_type', 'Lambda instance type'],
            'region',
            ['desired_instances', 'Desired instances'],
          ]}
        />
      </Section>
      <Section title="GPU hardware">
        <SettingsList
          entries={[
            { label: 'Vendor', value: hardware.vendor },
            { label: 'GPU model', value: hardware.gpu_model },
            { label: 'GPUs per instance', value: hardware.gpu_count },
            {
              label: 'Memory per GPU',
              render: <Quantity value={hardware.vram_gib_per_gpu} unit="GiB" />,
            },
          ]}
        />
      </Section>
      <Section title="Lease & cleanup">
        <SettingsList
          entries={[
            { label: 'Time to live', render: <Quantity value={data.ttl_minutes} unit="minutes" /> },
            {
              label: 'Drain timeout',
              render: <Quantity value={data.drain_timeout_s} unit="seconds" />,
            },
          ]}
        />
        <p className="section-note">
          The lease begins at the first confirmed allocation, including preparation time.
        </p>
      </Section>
      <Section title="Placement">
        <Fields data={data} fields={['worker_class', 'host_profile']} />
      </Section>
    </>
  );
}
function ServingProfile({ data }: { data: RecordValue }) {
  const model = record(data.model),
    engine = record(data.engine),
    topology = record(data.topology),
    hardware = record(data.hardware);
  return (
    <>
      <Section title="Model source">
        <Fields
          data={model}
          fields={[
            'alias',
            'repository',
            ['revision', 'Model revision'],
            ['tokenizer_revision', 'Tokenizer revision'],
          ]}
        />
      </Section>
      <Section title="Serving engine">
        <SettingsList
          entries={[
            { label: 'Backend', value: engine.backend },
            { label: 'Image digest', value: engine.image, mono: true },
            { label: 'Precision', value: engine.dtype },
            {
              label: 'Context ceiling',
              render: <Quantity value={engine.max_model_len} unit="tokens" />,
            },
            { label: 'Concurrent sequences', value: engine.max_num_seqs },
            {
              label: 'GPU memory budget',
              render: <Percent value={engine.gpu_memory_utilization} />,
            },
            { label: 'Prefix caching', value: engine.prefix_caching },
          ]}
        />
      </Section>
      <Section title="Deployment topology">
        <SettingsList
          entries={[
            { label: 'Topology', value: topology.kind },
            { label: 'Replicas', value: topology.replicas },
            { label: 'GPUs per replica', value: topology.gpus_per_replica },
            {
              label: 'Total configured demand',
              render: (
                <Quantity
                  value={number(topology.replicas) * number(topology.gpus_per_replica)}
                  unit="GPUs"
                />
              ),
            },
            { label: 'Tensor parallelism', value: topology.tensor_parallel_size },
            { label: 'Pipeline parallelism', value: topology.pipeline_parallel_size },
          ]}
        />
      </Section>
      <Section title="Placement & routing" initiallyOpen={false}>
        <SettingsList
          entries={[
            { label: 'Worker class', value: data.worker_class },
            { label: 'Routing policy', value: data.routing_policy },
            { label: 'GPU vendor', value: hardware.vendor },
            { label: 'Required GPU model', value: hardware.gpu_model ?? 'Any compatible model' },
            {
              label: 'Minimum memory per GPU',
              render: <Quantity value={hardware.min_vram_gib_per_gpu} unit="GiB" />,
            },
          ]}
        />
      </Section>
      <Section title="Speculative decoding" initiallyOpen={false}>
        {engine.speculative_decoding ? (
          <Fields
            data={engine.speculative_decoding}
            fields={['method', 'num_speculative_tokens', 'prompt_lookup_min', 'prompt_lookup_max']}
          />
        ) : (
          <p className="section-note">Not enabled for this profile.</p>
        )}
      </Section>
      <div className="configuration-footnote">
        <FileCheck2 size={14} />
        {data.draft
          ? 'Draft recipe · execution prerequisites remain unresolved.'
          : 'Version-pinned recipe · runtime readiness is checked separately.'}
      </div>
    </>
  );
}
function Scenario({ data }: { data: RecordValue }) {
  const phases = list(data.phases),
    warmup = record(data.warmup),
    duration = phases.reduce<number>((sum, value) => sum + number(record(value).duration_s), 0);
  return (
    <>
      <Section title="Participants" meta={`${list(data.participants).length} customers`}>
        <Value value={data.participants} />
      </Section>
      <Section title="Schedule">
        <SettingsList
          entries={[
            {
              label: 'Unscored warmup',
              render: <Quantity value={warmup.duration_s} unit="seconds" />,
            },
            { label: 'Measurement duration', render: <Quantity value={duration} unit="seconds" /> },
            {
              label: 'Drain timeout',
              render: <Quantity value={data.drain_timeout_s} unit="seconds" />,
            },
            { label: 'Random seed', value: data.seed },
          ]}
        />
      </Section>
      <div className="phase-overview" aria-label="Traffic phase durations">
        {phases.map((value, index) => {
          const phase = record(value);
          return (
            <div key={index} style={{ flexGrow: number(phase.duration_s) || 1 }}>
              <span>{text(phase.id)}</span>
              <small>{number(phase.duration_s).toLocaleString()}s</small>
            </div>
          );
        })}
      </div>
      {phases.map((value, index) => {
        const phase = record(value);
        return (
          <Section
            title={readableLabel(String(phase.id)) + ' phase'}
            meta={`${number(phase.duration_s)}s`}
            key={index}
            initiallyOpen={index === 0}
          >
            {Object.entries(record(phase.arrivals)).map(([customer, value]) => {
              const arrival = record(value);
              return (
                <div className="arrival-policy" key={customer}>
                  <strong>{customer}</strong>
                  <span>
                    {number(arrival.rate).toLocaleString()} {String(arrival.unit)}
                    {number(arrival.rate) === 1 ? '' : 's'} / {String(arrival.per)}
                  </span>
                  <small>{text(arrival.pattern)}</small>
                </div>
              );
            })}
          </Section>
        );
      })}
      <Section title="Warmup dataset overrides" initiallyOpen={false}>
        {Object.keys(record(warmup.datasets)).length ? (
          <SettingsList
            entries={Object.entries(record(warmup.datasets)).map(([customer, value]) => ({
              label: customer,
              value,
            }))}
          />
        ) : (
          <p className="section-note">No dataset overrides configured.</p>
        )}
      </Section>
      <Section title="Fault injection" initiallyOpen={false}>
        {list(data.faults).length ? (
          list(data.faults).map((value, index) => (
            <Fields
              key={index}
              data={value}
              fields={[
                'kind',
                ['customer_id', 'Target customer'],
                ['offset_s', 'Offset (seconds)'],
              ]}
            />
          ))
        ) : (
          <p className="section-note">No faults configured.</p>
        )}
      </Section>
    </>
  );
}
function Dataset({ data }: { data: RecordValue }) {
  const source = record(data.source),
    prepared = record(data.prepared);
  return (
    <>
      <Section title="Source & provenance">
        <SettingsList
          entries={[
            { label: 'Source', value: source.url },
            { label: 'Subset / split', value: source.subset },
            { label: 'Pinned source revision', value: source.revision },
            {
              label: 'License review',
              render: source.license ? (
                <Value value={source.license} />
              ) : (
                <span className="value-pending">Not reviewed</span>
              ),
            },
            { label: 'Input format', value: data.format },
            { label: 'Interaction', value: data.interaction },
            { label: 'Reference annotations', value: data.references },
            { label: 'Timing provenance', value: data.timing_provenance },
          ]}
        />
      </Section>
      <Section title="Prepared artifact">
        {data.prepared ? (
          <Fields
            data={prepared}
            fields={[
              'uri',
              ['record_count', 'Records'],
              ['max_rendered_input_tokens', 'Maximum rendered input (tokens)'],
              'sha256',
              'tokenizer_revision',
              'prompt_template_sha256',
            ]}
          />
        ) : (
          <div className="preparation-state">
            <span className="status-dot queued" />
            <div>
              <strong>Preparation pending</strong>
              <p>This is a source manifest. No benchmark artifact has been prepared.</p>
            </div>
          </div>
        )}
      </Section>
      <Section title="Preparation steps" initiallyOpen={false}>
        <ol className="configuration-checklist">
          {list(data.preparation).map((value, index) => (
            <li key={index}>{String(value)}</li>
          ))}
        </ol>
      </Section>
      <Section title="Known limitations" initiallyOpen={false}>
        <ul className="configuration-limitations">
          {list(data.limitations).map((value, index) => (
            <li key={index}>{String(value)}</li>
          ))}
        </ul>
      </Section>
      {Object.keys(record(source.filters)).length > 0 && (
        <Section title="Source filters" initiallyOpen={false}>
          <SettingsList
            entries={Object.entries(record(source.filters)).map(([key, value]) => ({
              label: readableLabel(key),
              value,
            }))}
          />
        </Section>
      )}
    </>
  );
}
function GenericDetails({ data }: { data: RecordValue }) {
  return (
    <Section title="Resource details">
      <SettingsList
        entries={Object.entries(data)
          .filter(([key]) => key !== 'notes')
          .map(([key, value]) => ({ label: readableLabel(key), value }))}
      />
    </Section>
  );
}
export default function ConfigurationDetails({
  kind,
  document,
}: {
  kind: string;
  document: unknown;
}) {
  const data = record(document);
  return (
    <section className="configuration-details" aria-label="Configuration details">
      <div className="configuration-heading">
        <SlidersHorizontal size={15} />
        <h4>Specification details</h4>
      </div>
      {kind === 'customer' ? (
        <Customer data={data} />
      ) : kind === 'worker-group' ? (
        <WorkerProfile data={data} />
      ) : kind === 'serving-profile' ? (
        <ServingProfile data={data} />
      ) : kind === 'scenario' ? (
        <Scenario data={data} />
      ) : kind === 'dataset-manifest' ? (
        <Dataset data={data} />
      ) : (
        <GenericDetails data={data} />
      )}
      {Boolean(data.schema_version || data.revision) && (
        <footer className="configuration-version">
          {data.revision ? `Revision ${String(data.revision)}` : ''}
          {data.schema_version ? <span>Schema {String(data.schema_version)}</span> : null}
        </footer>
      )}
    </section>
  );
}
