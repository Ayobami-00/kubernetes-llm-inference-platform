import type { WorkerMember } from '../../api/generated/types.gen';

export function WorkerObservations({ members }: { members: WorkerMember[] }) {
  return members.map((member) => (
    <section
      className="detail-card"
      key={member.provider_instance_id}
      aria-label="Worker observation"
    >
      <div className="key-value">
        <span>Worker</span>
        <b>{member.node_name || 'Not joined'}</b>
      </div>
      <div className="key-value">
        <span>Phase</span>
        <b>{member.phase.replaceAll('_', ' ')}</b>
      </div>
      {member.gpu_observation ? (
        <>
          <div className="key-value">
            <span>Device observation</span>
            <b>{new Date(member.gpu_observation.observed_at).toLocaleString()}</b>
          </div>
          <div className="key-value">
            <span>Observed allocatable GPUs</span>
            <b>{member.gpu_observation.allocatable_gpu_count}</b>
          </div>
          {member.gpu_observation.devices.map((device) => (
            <div key={device.uuid}>
              <div className="key-value">
                <span>GPU {device.index}</span>
                <b>{device.name}</b>
              </div>
              <div className="key-value">
                <span>Observed usable memory</span>
                <b>{(device.memory_total_mib / 1024).toFixed(2)} GiB</b>
              </div>
              <div className="key-value">
                <span>Driver</span>
                <b>{device.driver_version}</b>
              </div>
              <small>{device.uuid}</small>
            </div>
          ))}
        </>
      ) : (
        <p>No verified device observation yet.</p>
      )}
    </section>
  ));
}
