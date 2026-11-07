## ADDED Requirements

### Requirement: Numeric immutable release identity
Release workflows SHALL accept numeric X.Y.Z and historical vX.Y.Z tags, validate the normalized version against both distribution metadata before artifact creation, and preserve trusted-repository/push/ref, native opt-in/opt-out, verification and least-privilege publication gates. Image metadata SHALL retain the normalized numeric semantic version.

#### Scenario: Numeric candidate with matching artifacts
- **WHEN** tag 0.3.12 identifies source whose client and Hub versions are 0.3.12
- **THEN** normal verification and unchanged native/publisher policy apply and OCI tags identify 0.3.12.

#### Scenario: Malformed or mismatched release identity
- **WHEN** a tag is malformed or either distribution version differs
- **THEN** verification fails before artifact publication, without moving an existing tag.
