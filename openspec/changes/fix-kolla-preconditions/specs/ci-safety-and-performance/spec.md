## ADDED Requirements

### Requirement: Keep the client CLI version aligned with immutable release identity

The release version guard SHALL require the normalized release tag to match the root distribution version, Hub distribution version, and root client `__version__` before artifact creation. The packaged CLI smoke SHALL require both `palimpsest --version` and `plm --version` to match the built distribution version.

#### Scenario: Matching release surfaces

- **WHEN** an approved numeric or historical tag matches both distribution versions and the client `__version__`
- **THEN** the version gate passes and the built package's CLI commands report that same version

#### Scenario: Stale client CLI version

- **WHEN** either the source `__version__` or an installed CLI command reports a version different from the distribution tag
- **THEN** release verification or package smoke fails before publication
