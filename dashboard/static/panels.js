/* Offline / no-active-data panels */

function renderOfflinePanel(area, view) {
  const titles = {
    policy: 'Policy',
    memory: 'Memory',
    shield: 'Droid Shield',
    skills: 'Skills',
    telemetry: 'Telemetry Log',
    accounts: 'Service Accounts',
    email: 'Agent Email',
    orgchart: 'Org Chart',
    goals: 'Goals',
  };
  showNoData(
    area,
    titles[view] || view,
    'No active data for this subsystem. Wire a live backend to enable it.',
  );
}
