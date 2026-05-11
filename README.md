# re-pair

Repair broken decypharr-served symlinks in a media library by deleting the
stale file record in radarr/sonarr and triggering a fresh search. Designed
for migrations off decypharr (with the decypharr indexer disabled in the
arrs, replacement releases get grabbed via your other indexers, e.g.
nzbdav).

## What it does

For every symlink under `RE_PAIR_MEDIA_LINKS_ROOT` whose target lives under
`RE_PAIR_DECYPHARR_MOUNT`:

1. Probes the target with `ffprobe` (header + one packet) to check it can
   actually be played.
2. If the probe fails (broken link, zero bytes, EIO, ffprobe timeout), looks
   up the owning radarr/sonarr instance by matching root folders.
3. Deletes the `moviefile` / `episodefile` record via the arr API.
4. Fires `MoviesSearch` / `EpisodeSearch` for that entity.
5. Optionally polls the arr's command + history endpoints to confirm the
   search completed and a release was grabbed.

A bounded producer/consumer/verifier pipeline runs all three stages
concurrently with backpressure.

## First run

Prebuilt multi-arch images are published to
[`ghcr.io/pukabyte/re-pair`](https://ghcr.io/pukabyte/re-pair) on every
push to `main` and tagged release.

```sh
git clone https://github.com/Pukabyte/re-pair.git
cd re-pair
docker compose run --rm re-pair
```

The first run creates `./config/.env` and `./config/arrs.json` from
templates and exits. Edit them, then run the same command again.

To build the image locally instead of pulling from GHCR, uncomment the
`build: .` line in `docker-compose.yml`.

`arrs.json` schema:

```json
[
  { "name": "radarr",   "url": "http://radarr:7878",   "api_key": "...", "type": "radarr" },
  { "name": "sonarr4k", "url": "http://sonarr4k:8989", "api_key": "...", "type": "sonarr" }
]
```

## Configuration

All knobs live in `./config/.env`. CLI flags override env vars at runtime.

| Env var                       | Default               | Meaning |
|-------------------------------|-----------------------|---------|
| `RE_PAIR_MEDIA_LINKS_ROOT`    | `/mnt/medialinks`     | Symlink tree the arrs see. Must match arr rootfolder paths exactly. |
| `RE_PAIR_DECYPHARR_MOUNT`     | `/mnt/remote/realdebrid` | Prefix that identifies decypharr-served targets. |
| `RE_PAIR_ARRS_FILE`           | `/config/arrs.json`   | Path to arr config JSON. |
| `RE_PAIR_WORKERS`             | `16`                  | Parallel ffprobe probes. |
| `RE_PAIR_DELAY`               | `3.0`                 | Seconds to sleep after each successful repair. |
| `RE_PAIR_LIMIT`               | `0`                   | Stop after N bad items pushed (0 = unlimited). Useful for tests. |
| `RE_PAIR_DRY_RUN`             | `false`               | Report only; no deletes or searches. |
| `RE_PAIR_QUEUE_SIZE`          | `256`                 | Bounded queue between scanner and repairer. |
| `RE_PAIR_VERIFY`              | `false`               | Poll the arr after each repair to confirm search + grab. |
| `RE_PAIR_VERIFY_DEADLINE`     | `300`                 | Per-item verifier deadline in seconds. |
| `RE_PAIR_VERIFY_POLL`         | `10`                  | Verifier poll interval in seconds. |
| `RE_PAIR_PROBE_TIMEOUT`       | `20`                  | Per-file ffprobe timeout in seconds. |
| `RE_PAIR_HTTP_TIMEOUT`        | `60`                  | Per-request HTTP timeout in seconds. |

## Requirements

- Container needs network access to each arr (the default
  `docker-compose.yml` joins the `saltbox` external network; replace with
  `extra_hosts` or a different network if you don't use saltbox).
- Read-only bind mounts for the medialink tree and the decypharr FUSE
  mount (`rslave` propagation so decypharr restarts on the host are
  reflected inside the container).
- `ffprobe` is installed in the image (ffmpeg package).

## Safety notes

- `--dry-run` reports every repair it would make without touching the arr
  APIs. Use it first.
- Path-based ownership matching: when two arr instances claim the same
  root folder (e.g. `radarr` + `radarrforeign` both listing `Movies -
  Foreign`), the script asks each candidate which one actually has the
  file and only acts on the real owner.
- The arr API keys are kept out of the image via `.dockerignore` and
  `.gitignore`. They live only in `./config/` on the host and are
  mounted in read-only at runtime.
