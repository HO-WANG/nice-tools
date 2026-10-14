# SAP client migration execution

The user authorized installed SAP isql/bcp and automatic encrypted persistence.
Implementation ownership was divided across the CLI adapter, console/process
runner, profile persistence, manager integration, browser UI and packaging.

1. Confirm SAP flag/interface semantics and preserve catalog/transfer contracts.
2. Add password-prompt console execution with suspended Windows Job ownership.
3. Replace native catalogs/BCP while retaining schema, file and atomic-publication
   checks; remove obsolete driver/runtime build paths.
4. Save only successful connections with Jasypt ciphertext and remember the last
   profile; reconcile UI selection without changing the connected session.
5. Add meaningful regression tests and browser checks, review the final diff,
   and run the complete source suite.
6. Build Windows developer console fixtures, run native Windows tests, package
   and launch the sole EXE, and publish only after verification succeeds.
7. Save reusable cloud setup instructions with the final repository revision.

Actual SAP/ASE integration remains a separate environment-dependent check;
source mocks and compiled console fixtures must not be described as live tests.
