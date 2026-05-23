# Forge Platform

Forge is a small CI/CD platform with an integrated artifact registry. It exposes one HTTP API through nginx, with an engine service for pipeline runs and a registry service for immutable artifacts and dependency resolution.

Public URL: http://136.118.0.162

## Pipeline YAML Schema

```yaml
name: build-lib-http            # required, string
version: 1.0.0                  # required, semver
dependencies:                  # optional
  - name: lib-core              # required
    version: "^1.0.0"           # exact, ^, ~, or comparator range
jobs:                          # required
  build:
    needs: []                   # optional list of job names
    runtime: alpine:3.18        # optional Docker image
    resources:                  # optional
      cpu: 1.0
      memory: 512Mi
    steps:
      - name: test
        run: "sh ./test.sh"
      - name: package
        run: "tar czf out.tar.gz src/"
artifacts:                     # optional, auto-published after success
  - name: lib-http
    version: 1.0.0
    path: ./out.tar.gz
```

Unknown fields and missing required fields are rejected by the parser. Dependencies are resolved and pulled into `./deps/<name>/` before any job step runs. Each job receives `FORGE_TOKEN` and `FORGE_URL`.

## Architecture

The engine parses pipeline YAML, resolves dependencies through the registry, writes a deterministic lockfile, builds a job DAG, and executes jobs in Docker containers. The registry stores blobs by SHA-256 and stores metadata in SQLite.

The scheduler is implemented in `engine/scheduler.py`. It builds `needs` edges, detects cycles with DFS before running jobs, topologically sorts with Kahn's algorithm, and runs independent jobs in parallel up to `FORGE_MAX_CONCURRENCY`. If a job fails, dependent jobs are marked `skipped`.

Isolation is handled in `engine/runner.py` with Docker containers. Jobs run with their own workspace mounted at `/workspace`, a read-only root filesystem, dropped Linux capabilities, `no-new-privileges`, PID limits, CPU limits, memory limits, no swap, and an internal Docker network that contains the registry.

The storage layer is in `registry/storage.py` and `registry/metadata.py`. Blobs are content-addressed under `blobs/<sha-prefix>/<sha256>`. Metadata is stored in SQLite with `(name, version)` as the primary key, so a second publish of the same coordinate fails instead of overwriting the first.

The resolver is in `registry/resolver.py`. It parses exact, caret, tilde, and comparator range constraints without a semver resolver library. It walks transitive metadata, combines all constraints for each package, selects the highest satisfying version, and sorts the lockfile by package name for deterministic byte-for-byte output for the same input and registry state.

Logs are persisted as JSON lines on disk. `GET /runs/{id}/logs?follow=true` streams Server-Sent Events by reading files line-by-line and polling for new content, so large logs are not loaded fully into memory.

Two pipelines racing to publish the same `(name, version)` are handled by the SQLite primary key. Both uploads may compute/store the blob, but only one metadata insert can commit; the loser receives `409`.

## HTTP API

Writes require `Authorization: Bearer <token>`.

- `POST /runs`
- `GET /runs/{id}`
- `GET /runs/{id}/lockfile`
- `GET /runs/{id}/logs?follow=true`
- `POST /artifacts/{name}/{version}`
- `GET /artifacts/{name}/{version}`
- `GET /artifacts/{name}/{version}/meta`
- `GET /artifacts/{name}`

Run statuses: `queued`, `running`, `succeeded`, `failed`, `integrity_failure`, `conflict_failure`, `cycle_failure`.

## Fresh VPS Setup

1. Install Docker and Docker Compose on the VPS.
2. Clone the repository.
3. Set Slack variables if available:

```bash
export FORGE_SLACK_WEBHOOK="https://hooks.slack.com/services/..."
export FORGE_SLACK_ONCALL="<@U12345> <@U67890>"
```

4. Start the platform:

```bash
docker compose up -d --build
```

5. Create the first admin token from the host:

```bash
docker compose exec registry python auth.py create admin
```

6. Install and login with the CLI:

```bash
pip install -e ./cli
forge login http://YOUR_PUBLIC_IP
```

Paste the token printed in step 5 when prompted.

## Required Capability Test Plan

Run these in order:

```bash
forge run examples/build-lib-core.yaml
forge run examples/build-lib-http.yaml
forge run examples/build-service-api.yaml
forge resolve examples/conflict-pipeline.yaml
forge run examples/security-probe.yaml
forge run examples/large-log.yaml
```

For wrong checksum:

```bash
echo hello > bad.txt
curl -i -H "Authorization: Bearer $FORGE_TOKEN" \
  -F file=@bad.txt \
  -F checksum=sha256:0000000000000000000000000000000000000000000000000000000000000000 \
  http://YOUR_PUBLIC_IP/artifacts/bad/1.0.0
```

Expected result: `400`.

For duplicate publish, run `examples/build-lib-core.yaml` twice. The second publish should fail with `409`.

## Slack Alerts

Slack webhook config is read from `FORGE_SLACK_WEBHOOK`. Integrity alerts include `FORGE_SLACK_ONCALL` tags.

Screenshot: TODO before submission.
