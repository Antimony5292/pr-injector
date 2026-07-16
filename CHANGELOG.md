# Changelog

## 0.2.0 - 2026-07-16

### Added

- Reusable bug-transplant and feature-addition construction toolkits.
- Optional Agent Maestro integrations and toolkit-specific construction tests.
- Compatibility documentation for running the reusable toolkit and legacy
  workflows side by side.

### Preserved

- The legacy `scripts/` workflows, including the ProdBench Azure DevOps/C#
  contract in `scripts/experiment_ado.py`.
- Historical analysis, assembly, verification, launch, and agent-runner entry
  points that existed before the toolkit packaging update.

### Fixed

- Windows path normalization in feature-addition modern-path discovery.
- Platform-specific pytest null-configuration handling in bug-transplant tests.

The reusable toolkit and legacy workflows remain independent and do not
silently fall back to one another.
