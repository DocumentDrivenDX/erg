# Public verification evidence

The local-first release gate is recorded in [local-first-release.md](local-first-release.md).
Historical operator qualification logs and raw machine observations remain local
under the ignored `evidence/` directory and `.local-evidence/`. They are not part
of the source release and should not be used as portable acceptance requirements.
Central and fleet rollout require independent qualification for each deployment.
