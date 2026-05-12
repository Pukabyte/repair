#!/usr/bin/env python3
"""
Repair: for every symlink under /mnt/medialinks that points at the
decypharr mount and is broken or unplayable, delete the file record in
the owning arr and trigger a fresh search. With decypharr disabled in
the arrs, the new release lands on nzbdav.
"""

import argparse
import concurrent.futures
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests


def _parse_dotenv(path: str) -> dict:
    """Minimal .env parser: KEY=VALUE per line, # comments, quotes stripped."""
    out: dict[str, str] = {}
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            if k:
                out[k] = v
    return out


def bootstrap_config():
    """
    Load <config_dir>/.env into os.environ (without overriding existing vars)
    and point REPAIR_ARRS_FILE at <config_dir>/arrs.json when it exists.
    If neither file is present, emit a hint pointing at the example files
    in the repo and exit so the user can populate config/ on the host.
    """
    config_dir = os.environ.get("REPAIR_CONFIG_DIR", "/config")
    if not os.path.isdir(config_dir):
        return
    env_path = os.path.join(config_dir, ".env")
    arrs_path = os.path.join(config_dir, "arrs.json")
    if not os.path.exists(env_path) and not os.path.exists(arrs_path):
        print(
            f"no config found in {config_dir}. Copy the examples on the host:\n"
            f"  cp .env.example {config_dir}/.env\n"
            f"  cp arrs.json.example {config_dir}/arrs.json\n"
            "then fill in api keys / paths and re-run.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    if os.path.exists(env_path):
        for k, v in _parse_dotenv(env_path).items():
            os.environ.setdefault(k, v)
    if os.path.exists(arrs_path):
        os.environ.setdefault("REPAIR_ARRS_FILE", arrs_path)


bootstrap_config()


def _env(name: str, default):
    """Read env var, fall back to default. Empty string treated as unset."""
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


MEDIA_LINKS_ROOT = _env("REPAIR_MEDIA_LINKS_ROOT", "/mnt/medialinks")
DECYPHARR_MOUNT = _env("REPAIR_DECYPHARR_MOUNT", "/mnt/remote/realdebrid")
PLAYABILITY_TIMEOUT_S = int(_env("REPAIR_PROBE_TIMEOUT", "20"))
HTTP_TIMEOUT_S = int(_env("REPAIR_HTTP_TIMEOUT", "60"))
FFPROBE = _env("REPAIR_FFPROBE", "/usr/bin/ffprobe")


def load_arrs() -> list[dict]:
    """
    Load arr config from either:
      - JSON file at REPAIR_ARRS_FILE, or
      - inline JSON in REPAIR_ARRS
    One of them is required. Each entry needs: name, url, api_key, type.
    """
    path = os.environ.get("REPAIR_ARRS_FILE", "").strip()
    inline = os.environ.get("REPAIR_ARRS", "").strip()
    if path:
        with open(path) as f:
            arrs = json.load(f)
    elif inline:
        arrs = json.loads(inline)
    else:
        raise SystemExit(
            "no arr config: set REPAIR_ARRS_FILE=/path/to/arrs.json "
            "or REPAIR_ARRS='[{...}]'"
        )
    required = {"name", "url", "api_key", "type"}
    for a in arrs:
        missing = required - a.keys()
        if missing:
            raise ValueError(f"arr config entry missing fields {missing}: {a}")
        if a["type"] not in ("radarr", "sonarr"):
            raise ValueError(f"arr type must be radarr|sonarr: {a}")
    return arrs


ARRS: list[dict] = []


@dataclass
class BadLink:
    symlink: str
    target: str
    reason: str
    arr: Optional[dict] = field(default=None)


@dataclass
class RepairResult:
    status: str               # "ok" | "NOT_OWNER" | "no_episodes"
    msg: str = ""
    command_id: Optional[int] = None
    entity_ids: list = field(default_factory=list)
    title: str = ""


@dataclass
class VerifyTask:
    arr: dict
    kind: str                 # "radarr" | "sonarr"
    command_id: int
    entity_ids: list
    title: str
    started_at: str           # ISO8601 'Z'
    deadline: float           # monotonic clock


def api_get(arr, endpoint, params=None):
    r = requests.get(
        f"{arr['url']}/api/v3/{endpoint}",
        headers={"X-Api-Key": arr["api_key"]},
        params=params,
        timeout=HTTP_TIMEOUT_S,
    )
    r.raise_for_status()
    return r.json()


def api_post(arr, endpoint, payload):
    r = requests.post(
        f"{arr['url']}/api/v3/{endpoint}",
        headers={"X-Api-Key": arr["api_key"], "Content-Type": "application/json"},
        json=payload,
        timeout=HTTP_TIMEOUT_S,
    )
    r.raise_for_status()
    return r.json() if r.text else None


def api_delete(arr, endpoint):
    r = requests.delete(
        f"{arr['url']}/api/v3/{endpoint}",
        headers={"X-Api-Key": arr["api_key"]},
        timeout=HTTP_TIMEOUT_S,
    )
    r.raise_for_status()


def load_root_paths():
    for arr in ARRS:
        try:
            roots = api_get(arr, "rootfolder")
            arr["root_paths"] = [r["path"].rstrip("/") for r in roots]
        except Exception as e:
            print(f"[{arr['name']}] failed to load rootfolders: {e}")
            arr["root_paths"] = []


def candidate_arrs_for_path(path: str) -> list[dict]:
    """
    Every arr whose root folder is a prefix of the symlink path.
    Multiple arrs may legitimately claim the same root (e.g. radarr +
    radarrforeign both list Movies - Foreign); we return all of them
    sorted by longest-match-first and let the caller pick the one that
    actually owns the file.
    """
    matches = []
    for arr in ARRS:
        for root in arr.get("root_paths", []):
            if path == root or path.startswith(root + "/"):
                matches.append((len(root), arr))
                break
    matches.sort(key=lambda x: -x[0])
    return [arr for _, arr in matches]


def path_covered_by_any_arr(path: str) -> bool:
    return bool(candidate_arrs_for_path(path))


_movie_cache: dict[str, list] = {}
_series_cache: dict[str, list] = {}


def get_movies(arr) -> list:
    key = arr["name"]
    if key not in _movie_cache:
        _movie_cache[key] = api_get(arr, "movie")
    return _movie_cache[key]


def get_series(arr) -> list:
    key = arr["name"]
    if key not in _series_cache:
        _series_cache[key] = api_get(arr, "series")
    return _series_cache[key]


def resolve_target(link_path: str) -> str:
    target = os.readlink(link_path)
    if not os.path.isabs(target):
        target = os.path.abspath(os.path.join(os.path.dirname(link_path), target))
    return target


def points_at_decypharr(target: str) -> bool:
    return target == DECYPHARR_MOUNT or target.startswith(DECYPHARR_MOUNT + "/")


def check_playable(link_path: str, target: str) -> tuple[bool, str]:
    """
    Returns (is_ok, reason_if_bad). We delegate playability to ffprobe:
    if it can parse the container header and demux one packet (which
    forces a tail-seek for mp4 with moov-at-end), the file is good
    enough that decypharr is serving real bytes for it.
    """
    if not os.path.exists(target):
        return False, "broken (target missing)"
    try:
        st = os.stat(target)
    except OSError as e:
        return False, f"stat failed: {e}"
    if st.st_size == 0:
        return False, "zero-byte file"
    try:
        proc = subprocess.run(
            [
                FFPROBE,
                "-v", "error",
                "-read_intervals", "%+#1",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                target,
            ],
            capture_output=True,
            text=True,
            timeout=PLAYABILITY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"ffprobe timeout after {PLAYABILITY_TIMEOUT_S}s"
    except OSError as e:
        return False, f"ffprobe spawn failed: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        msg = err[-1] if err else f"exit {proc.returncode}"
        return False, f"ffprobe: {msg}"
    if not (proc.stdout or "").strip():
        return False, "ffprobe: no duration"
    return True, ""


def gather_candidates(root: str, require_arr_root: bool = True) -> tuple[list[tuple[str, str]], int]:
    candidates: list[tuple[str, str]] = []
    skipped_no_arr = 0
    for dirpath, _, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                continue
            try:
                tgt = resolve_target(p)
            except OSError as e:
                print(f"readlink failed for {p}: {e}", flush=True)
                continue
            if not points_at_decypharr(tgt):
                continue
            if require_arr_root and not path_covered_by_any_arr(p):
                skipped_no_arr += 1
                continue
            candidates.append((p, tgt))
    return candidates, skipped_no_arr


def scan_producer(
    candidates: list[tuple[str, str]],
    workers: int,
    bad_q: "queue.Queue[Optional[BadLink]]",
    stop_event: threading.Event,
    limit: int,
    progress_every: int = 1000,
):
    """
    Probe each candidate with ffprobe in a thread pool. Bad items are
    pushed onto bad_q as they're discovered. Pushes a single None
    sentinel when finished so the consumer knows to drain and stop.
    """
    bad_count = 0
    done = 0
    total = len(candidates)
    last_log = time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(check_playable, sp, tp): (sp, tp) for sp, tp in candidates}
            try:
                for fut in concurrent.futures.as_completed(futures):
                    if stop_event.is_set():
                        break
                    sp, tp = futures[fut]
                    done += 1
                    try:
                        ok, reason = fut.result()
                    except Exception as e:
                        ok, reason = False, f"check raised: {e}"
                    if not ok:
                        bad_q.put(BadLink(symlink=sp, target=tp, reason=reason))
                        bad_count += 1
                        if limit and bad_count >= limit:
                            print(f"\n[scan] hit --limit {limit} after probing {done}/{total}, stopping", flush=True)
                            break
                    now = time.monotonic()
                    if done % progress_every == 0 or (now - last_log) > 30:
                        last_log = now
                        print(f"[scan] {done}/{total} probed, {bad_count} bad so far", flush=True)
            finally:
                for f in futures:
                    f.cancel()
    finally:
        print(f"[scan] done. probed {done}/{total}, {bad_count} bad", flush=True)
        bad_q.put(None)


@dataclass
class Counters:
    repaired: int = 0
    skipped: int = 0
    errored: int = 0


def repair_consumer(
    bad_q: "queue.Queue[Optional[BadLink]]",
    dry_run: bool,
    repair_delay: float,
    counters: Counters,
    verify_q: "Optional[queue.Queue[Optional[VerifyTask]]]",
    verify_deadline: float,
):
    while True:
        b = bad_q.get()
        if b is None:
            if verify_q is not None:
                verify_q.put(None)
            return
        candidates = candidate_arrs_for_path(b.symlink)
        if not candidates:
            print(f"\nSKIP no arr root: {b.symlink}", flush=True)
            counters.skipped += 1
            continue
        print(f"\n{b.reason}", flush=True)
        print(f"  link:   {b.symlink}", flush=True)
        print(f"  target: {b.target}", flush=True)
        owner_arr = None
        owner_result: Optional[RepairResult] = None
        last_error = None
        for arr in candidates:
            try:
                if arr["type"] == "radarr":
                    r = repair_radarr(arr, b.symlink, dry_run)
                else:
                    r = repair_sonarr(arr, b.symlink, dry_run)
            except requests.HTTPError as e:
                body = e.response.text[:200] if e.response is not None else ""
                last_error = f"[{arr['name']}] http {e}: {body}"
                continue
            except Exception as e:
                last_error = f"[{arr['name']}] {e}"
                continue
            if r.status == "NOT_OWNER":
                continue
            owner_arr, owner_result = arr, r
            break
        if owner_arr and owner_result:
            print(f"  [{owner_arr['name']}] -> {owner_result.msg}", flush=True)
            counters.repaired += 1
            if verify_q is not None and not dry_run and owner_result.command_id and owner_result.entity_ids:
                verify_q.put(VerifyTask(
                    arr=owner_arr,
                    kind=owner_arr["type"],
                    command_id=owner_result.command_id,
                    entity_ids=list(owner_result.entity_ids),
                    title=owner_result.title,
                    started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    deadline=time.monotonic() + verify_deadline,
                ))
            if not dry_run and repair_delay > 0:
                time.sleep(repair_delay)
        elif last_error:
            print(f"  ERROR: {last_error}", flush=True)
            counters.errored += 1
        else:
            tried = ",".join(a["name"] for a in candidates)
            print(f"  SKIP not owned by any candidate arr ({tried})", flush=True)
            counters.skipped += 1


def _history_grabbed_for_entity(task: VerifyTask) -> Optional[dict]:
    """
    Return the most-recent grabbed-history record for the entity since
    task.started_at, or None if no grab has happened yet.
    """
    params = {
        "pageSize": 50,
        "sortKey": "date",
        "sortDirection": "descending",
        "eventType": 1,           # 1 = grabbed (both radarr v3 + sonarr v3)
    }
    if task.kind == "radarr":
        params["movieIds"] = task.entity_ids[0]
    try:
        hist = api_get(task.arr, "history", params=params)
    except Exception as e:
        print(f"  [verify] history poll error: {e}", flush=True)
        return None
    records = hist.get("records", hist) if isinstance(hist, dict) else hist
    for rec in records:
        if rec.get("date", "") <= task.started_at:
            continue
        if task.kind == "radarr":
            if rec.get("movieId") in task.entity_ids:
                return rec
        else:
            if rec.get("episodeId") in task.entity_ids:
                return rec
    return None


def _command_done(task: VerifyTask) -> tuple[bool, str]:
    try:
        cmd = api_get(task.arr, f"command/{task.command_id}")
    except Exception as e:
        return False, f"poll err: {e}"
    return cmd.get("status") in ("completed", "failed", "aborted"), cmd.get("status", "?")


def verify_worker(
    verify_q: "queue.Queue[Optional[VerifyTask]]",
    poll_interval: float,
):
    in_flight: list[tuple[VerifyTask, bool]] = []   # (task, search_done)
    sentinel_seen = False
    while True:
        try:
            while True:
                t = verify_q.get_nowait()
                if t is None:
                    sentinel_seen = True
                else:
                    in_flight.append((t, False))
        except queue.Empty:
            pass

        if not in_flight:
            if sentinel_seen:
                return
            time.sleep(min(1.0, poll_interval))
            continue

        still: list[tuple[VerifyTask, bool]] = []
        for task, search_done in in_flight:
            tag = f"[verify {task.arr['name']}:{task.entity_ids} '{task.title[:40]}']"
            if not search_done:
                done, status = _command_done(task)
                if done:
                    print(f"  {tag} search {status}", flush=True)
                    search_done = True
                elif time.monotonic() > task.deadline:
                    print(f"  {tag} search did not complete before deadline (last status={status})", flush=True)
                    continue
                else:
                    still.append((task, search_done))
                    continue
            grab = _history_grabbed_for_entity(task)
            if grab:
                src = grab.get("sourceTitle") or grab.get("title") or "?"
                indexer = (grab.get("data") or {}).get("indexer") or "?"
                print(f"  {tag} GRABBED via {indexer}: {src}", flush=True)
                continue
            if time.monotonic() > task.deadline:
                print(f"  {tag} no grab before deadline, giving up", flush=True)
                continue
            still.append((task, search_done))
        in_flight = still
        time.sleep(poll_interval)


def _find_movie_for_symlink(arr, sym: str):
    """Return the movie dict from this arr whose movieFile.path matches sym, else None."""
    for movie in get_movies(arr):
        folder = (movie.get("path") or "").rstrip("/")
        if not folder or not sym.startswith(folder + "/"):
            continue
        mf = movie.get("movieFile") or {}
        if mf.get("path") == sym:
            return movie
    return None


def repair_radarr(arr, sym: str, dry_run: bool) -> RepairResult:
    movie = _find_movie_for_symlink(arr, sym)
    if not movie:
        return RepairResult(status="NOT_OWNER")
    mf = movie["movieFile"]
    mfid = mf["id"]
    movie_id = movie["id"]
    title = movie.get("title") or ""
    if dry_run:
        return RepairResult(status="ok", msg=f"would delete moviefile {mfid} and search movieId {movie_id} ('{title}')",
                            entity_ids=[movie_id], title=title)
    api_delete(arr, f"moviefile/{mfid}")
    cmd = api_post(arr, "command", {"name": "MoviesSearch", "movieIds": [movie_id]}) or {}
    _movie_cache.pop(arr["name"], None)
    return RepairResult(
        status="ok",
        msg=f"deleted moviefile {mfid}, searched movieId {movie_id} ('{title}') cmd={cmd.get('id')}",
        command_id=cmd.get("id"),
        entity_ids=[movie_id],
        title=title,
    )


def _find_series_for_symlink(arr, sym: str):
    for series in get_series(arr):
        folder = (series.get("path") or "").rstrip("/")
        if folder and sym.startswith(folder + "/"):
            return series
    return None


def repair_sonarr(arr, sym: str, dry_run: bool) -> RepairResult:
    series = _find_series_for_symlink(arr, sym)
    if not series:
        return RepairResult(status="NOT_OWNER")
    series_id = series["id"]
    ep_files = api_get(arr, "episodefile", params={"seriesId": series_id})
    match = next((ef for ef in ep_files if ef.get("path") == sym), None)
    if not match:
        return RepairResult(status="NOT_OWNER")
    efid = match["id"]
    title = series.get("title") or ""
    episodes = api_get(arr, "episode", params={"seriesId": series_id})
    ep_ids = [e["id"] for e in episodes if e.get("episodeFileId") == efid]
    if not ep_ids:
        return RepairResult(status="no_episodes",
                            msg=f"episodefile {efid} present but no episodes link to it",
                            title=title)
    if dry_run:
        return RepairResult(status="ok",
                            msg=f"would delete episodefile {efid} and search episodes {ep_ids} ('{title}')",
                            entity_ids=ep_ids, title=title)
    api_delete(arr, f"episodefile/{efid}")
    cmd = api_post(arr, "command", {"name": "EpisodeSearch", "episodeIds": ep_ids}) or {}
    return RepairResult(
        status="ok",
        msg=f"deleted episodefile {efid}, searched episodes {ep_ids} ('{title}') cmd={cmd.get('id')}",
        command_id=cmd.get("id"),
        entity_ids=ep_ids,
        title=title,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=MEDIA_LINKS_ROOT)
    ap.add_argument("--workers", type=int, default=int(_env("REPAIR_WORKERS", "16")), help="parallel ffprobe probes")
    ap.add_argument("--dry-run", action="store_true", default=_env_bool("REPAIR_DRY_RUN", False),
                    help="report only, no deletes or searches")
    ap.add_argument("--limit", type=int, default=int(_env("REPAIR_LIMIT", "0")),
                    help="stop after N bad items pushed (0 = unlimited)")
    ap.add_argument("--delay", type=float, default=float(_env("REPAIR_DELAY", "3.0")),
                    help="seconds to sleep after each successful repair (radarr + sonarr)")
    ap.add_argument("--queue-size", type=int, default=int(_env("REPAIR_QUEUE_SIZE", "256")),
                    help="bounded queue size between scanner and repairer")
    ap.add_argument("--verify", action="store_true", default=_env_bool("REPAIR_VERIFY", False),
                    help="poll the arr after each repair to confirm search completed and a release was grabbed")
    ap.add_argument("--verify-deadline", type=float, default=float(_env("REPAIR_VERIFY_DEADLINE", "300.0")),
                    help="seconds to wait per item for a grab before giving up")
    ap.add_argument("--verify-poll", type=float, default=float(_env("REPAIR_VERIFY_POLL", "10.0")),
                    help="seconds between verifier polls")
    args = ap.parse_args()

    ARRS.clear()
    ARRS.extend(load_arrs())
    src = "REPAIR_ARRS_FILE" if os.environ.get("REPAIR_ARRS_FILE") else "REPAIR_ARRS"
    print(f"Loaded {len(ARRS)} arr instance(s) from {src}", flush=True)

    print("Loading arr root folders...", flush=True)
    load_root_paths()
    for arr in ARRS:
        print(f"  {arr['name']}: {arr.get('root_paths') or 'NONE'}", flush=True)

    print(f"\nWalking {args.root} for decypharr symlinks...", flush=True)
    candidates, skipped_no_arr = gather_candidates(args.root)
    print(f"  found {len(candidates)} decypharr symlinks under arr roots (skipped {skipped_no_arr} outside arr roots)", flush=True)
    if not candidates:
        return 0

    print(
        f"\nProbing in background (workers={args.workers}) "
        f"while repairer drains queue (delay={args.delay}s, limit={args.limit or 'none'}, dry_run={args.dry_run})...",
        flush=True,
    )

    bad_q: "queue.Queue[Optional[BadLink]]" = queue.Queue(maxsize=args.queue_size)
    verify_q: Optional["queue.Queue[Optional[VerifyTask]]"] = (
        queue.Queue() if args.verify and not args.dry_run else None
    )
    stop_event = threading.Event()
    counters = Counters()

    producer = threading.Thread(
        target=scan_producer,
        args=(candidates, args.workers, bad_q, stop_event, args.limit),
        name="scan-producer",
        daemon=True,
    )
    consumer = threading.Thread(
        target=repair_consumer,
        args=(bad_q, args.dry_run, args.delay, counters, verify_q, args.verify_deadline),
        name="repair-consumer",
        daemon=True,
    )
    verifier = None
    if verify_q is not None:
        verifier = threading.Thread(
            target=verify_worker,
            args=(verify_q, args.verify_poll),
            name="verify-worker",
            daemon=True,
        )

    producer.start()
    consumer.start()
    if verifier:
        verifier.start()

    try:
        producer.join()
        consumer.join()
        if verifier:
            verifier.join()
    except KeyboardInterrupt:
        print("\nInterrupted, stopping...", flush=True)
        stop_event.set()
        try:
            bad_q.put_nowait(None)
        except queue.Full:
            pass
        if verify_q is not None:
            try:
                verify_q.put_nowait(None)
            except queue.Full:
                pass
        producer.join(timeout=10)
        consumer.join(timeout=10)
        if verifier:
            verifier.join(timeout=10)

    print(
        f"\nDone. repaired={counters.repaired} skipped={counters.skipped} "
        f"errored={counters.errored} (dry_run={args.dry_run})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
