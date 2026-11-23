## 1. Source and contract

- [x] 1.1 Compare the failure evidence with Kolla's stock shard-root and cloud-profile patterns.
- [x] 1.2 Fix the Keystone cloud-profile calls and add focused contract coverage.
- [x] 1.3 Add Kolla shard-root and ProxySQL-user defaults with regression coverage.

## 2. Release preparation

- [ ] 2.1 Synchronize root/Hub metadata, the client `__version__`, release validation, locks, Kolla image tag, changelog and installation guidance at 0.3.33.
- [ ] 2.2 Update architecture maintenance evidence and pass the architecture guard.
- [ ] 2.3 Run release verification, package builds and independent review on the exact source.

## 3. Publication and deployment

- [ ] 3.1 Merge through the protected `dev` PR after required checks pass; publish immutable 0.3.33 artifacts.
- [ ] 3.2 Install the exact released package in `/etc/kolla` and verify its wheel hash and role bytes.
- [ ] 3.3 Provision the shard-root through Kolla Toolbox on the preconfigured backend, then run canonical compute and five-service control reconfigure.
- [ ] 3.4 Verify all deployed images, container health and HTTPS/API routes; append exact evidence to the handoff.
