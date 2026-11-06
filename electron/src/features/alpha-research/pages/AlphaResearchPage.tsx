/**
 * AlphaResearchPage — mounts the ported AutoAlpha frontend-v2 dashboard.
 *
 * This is the Alpha Research entry point. It scopes the AutoAlpha shadcn
 * design tokens via [data-theme="alpha-research"] and renders the AppRoot
 * (HomePage / MiningDashboard / FactorLibrary / Backtest / Settings) inside
 * a TaskProvider.
 *
 * The original three-panel scaffold (FactorListPanel / FactorDetailPanel /
 * FactorEvolutionPanel / EvolutionStatsPanel) under ./components is left in
 * place for now in case anything links to it directly, but the user-facing
 * /alpha-research route now renders the full frontend-v2 experience.
 */

import '../styles-v2/index.css';
import AppRoot from '../pages-v2/AppRoot';

export default function AlphaResearchPage() {
  return (
    <div
      data-theme="alpha-research"
      style={{ height: '100%', width: '100%', overflow: 'auto', position: 'relative' }}
    >
      <AppRoot />
    </div>
  );
}
