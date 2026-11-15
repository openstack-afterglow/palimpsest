# Hub Git build example

A Dockerfile and one file, meant to be built by a Palimpsest Hub from a pinned
commit of a public Git repository (`POST /v1/git-builds`).

The Hub operator must have allowed the Git host (`palimpsest_git_allowed_hosts`)
and the base image repository (`palimpsest_image_source_policy`, here
`docker.io/library/ubuntu`). Without either setting the request is refused
before any Git or registry call.

```sh
palimpsest --url https://HUB --cloud CLOUD image git-build \
  --repo-url https://github.com/openstack-afterglow/palimpsest \
  --revision <full 40-character commit SHA that contains this directory> \
  --context-subdir examples/git-build \
  --dockerfile-path examples/git-build/Dockerfile \
  --request-id "$(uuidgen | tr A-Z a-z)" \
  --name git-build-example --os-family ubuntu --os-version 24.04

palimpsest --url https://HUB --cloud CLOUD image git-job <job id>
```

`git-job` shows the job status and the commit it was built from. The build runs
without network access, so the Dockerfile uses only its base image and the
checked-out files. Private repositories are not supported.
