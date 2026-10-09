## MODIFIED Requirements

### Requirement: Preserve global and credential boundaries

Hub SHALL preserve verified-system-admin global builder/GC gates and key-only native package writes, without granting service admin global authority or allowing package keys to authorize OpenStack/VM launch. System administrator recognition SHALL use a fresh direct user/system-all assignment query through the separate reader, not project-oriented effective expansion, and SHALL still require an exact subject user, system-all scope and named admin role.

#### Scenario: Service admin and package key cannot launch

- **WHEN** service admin tokens or package keys call global builder/GC or VM operations
- **THEN** no new authority is granted.

#### Scenario: Direct system administrator survives project-oriented expansion

- **WHEN** a project-scoped subject user has a direct admin assignment on system-all while effective expansion omits that assignment
- **THEN** Hub recognizes the administrator through the direct reader lookup without exchanging or re-scoping the original subject token.

#### Scenario: Revocation removes global authority despite project admin

- **WHEN** the direct system-all admin assignment is removed while the original token retains a project admin role
- **THEN** the next validation no longer recognizes a system administrator and privileged requests are denied with 403.
