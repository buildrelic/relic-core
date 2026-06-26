#!/usr/bin/env bash
# eval/ingest_bench.sh — A/B the ingest LOAD path for REL-61: sequential vs bulk.
#
# Fetches a repo's episodes ONCE, then loads them twice into a freshly-wiped graph:
#   A = baseline  (today's default: the sequential per-episode add_episode loop)
#   B = bulk      (add_episode_bulk + wider concurrency)
# Reports wall-clock speedup AND runs eval/graph_stats.py before/after, so a speed
# win is shown not to regress dedup_forking / citation_integrity (the quality gate).
#
# Run from the repo root, FalkorDB up (OrbStack), OpenAI + GitHub creds in .env:
#   REPO=buildrelic/relic-core LIMIT=15 ./eval/ingest_bench.sh
#
# Cost note: an A/B re-pays LLM extraction TWICE. Keep LIMIT small (10-15) for the
# first read; raise it once you trust the ratio. SKIP_FETCH=1 reuses spooled episodes.
#
# Knobs (env, defaults):
#   REPO              owner/name to benchmark                         (required)
#   LIMIT             episodes per run                                [15]
#   BASE_COROUTINES   sequential run's GRAPHITI_MAX_COROUTINES        [20]
#   BULK_BATCH        bulk run's BULK_BATCH_SIZE                      [15]
#   BULK_COROUTINES   bulk run's GRAPHITI_MAX_COROUTINES             [30]
#   RELIC / PYRUN     how to invoke cli / python    [uv run --no-sync ...]
#   FALKOR_HOST/PORT/PASSWORD   for the between-run graph wipe
#   WIPE_CMD          override the whole wipe command if your setup differs
#
# The WIPE between runs drops the repo's FalkorDB graph (GRAPH.DELETE on the engram
# database = repo slug) so each run measures only itself. It is destructive — confirm
# the key with Abhinav before pointing this at anything but a scratch graph.
set -euo pipefail

REPO="${REPO:?set REPO=owner/name}"
LIMIT="${LIMIT:-15}"
BASE_COROUTINES="${BASE_COROUTINES:-20}"
BULK_BATCH="${BULK_BATCH:-15}"
BULK_COROUTINES="${BULK_COROUTINES:-30}"
RELIC="${RELIC:-uv run --no-sync relic}"   # --no-sync dodges the editable-install flapping
PYRUN="${PYRUN:-uv run --no-sync python}"
FALKOR_HOST="${FALKOR_HOST:-localhost}"
FALKOR_PORT="${FALKOR_PORT:-6379}"

OUT="$(mktemp -d)"
GROUP_ID="$($PYRUN -c "from relic.ingest import repo_group_id; print(repo_group_id('$REPO'))")"
echo ">> repo=$REPO  group_id=$GROUP_ID  limit=$LIMIT  artifacts=$OUT"

wipe() {  # drop the repo graph so the next load starts from empty
  if [ -n "${WIPE_CMD:-}" ]; then eval "$WIPE_CMD"; return; fi
  if ! command -v redis-cli >/dev/null 2>&1; then
    echo "!! redis-cli not found — wipe the '$GROUP_ID' graph manually, or set WIPE_CMD." >&2
    exit 1
  fi
  if [ -n "${FALKOR_PASSWORD:-}" ]; then
    redis-cli -h "$FALKOR_HOST" -p "$FALKOR_PORT" -a "$FALKOR_PASSWORD" GRAPH.DELETE "$GROUP_ID" >/dev/null 2>&1 || true
  else
    redis-cli -h "$FALKOR_HOST" -p "$FALKOR_PORT" GRAPH.DELETE "$GROUP_ID" >/dev/null 2>&1 || true
  fi
}

run_load() {  # $1 label; the knobs + BULK_FLAG are exported by the caller
  local label="$1" log="$OUT/$label.load.log"
  SECONDS=0  # portable timer (macOS date has no %N)
  if ! $RELIC load --repo "$REPO" --limit "$LIMIT" --fresh ${BULK_FLAG:-} --no-progress >"$log" 2>&1; then
    cat "$log" >&2; echo "!! load failed ($label)" >&2; exit 1
  fi
  cat "$log" >&2   # surface the CLI's own per-episode timing block to the user
  echo "$SECONDS"  # stdout = elapsed seconds only (captured by the caller)
}

echo "== fetch + spool once (shared, identical input for both runs) =="
if [ "${SKIP_FETCH:-0}" = "0" ]; then
  $RELIC ingest --repo "$REPO" --limit "$LIMIT" --no-load --no-progress >"$OUT/fetch.log" 2>&1 \
    || { cat "$OUT/fetch.log" >&2; echo "!! fetch failed"; exit 1; }
  tail -3 "$OUT/fetch.log"
else
  echo "(SKIP_FETCH=1 — reusing already-spooled episodes)"
fi

echo "== A: baseline (sequential, coroutines=$BASE_COROUTINES) =="
wipe
export BULK_LOAD=false GRAPHITI_MAX_COROUTINES="$BASE_COROUTINES"; unset BULK_BATCH_SIZE 2>/dev/null || true
BULK_FLAG=""; A_WALL="$(run_load baseline)"
$PYRUN eval/graph_stats.py --repo "$REPO" --json "$OUT/before.json" >/dev/null
echo ">> A wall-clock: ${A_WALL}s"

echo "== B: bulk (batch=$BULK_BATCH, coroutines=$BULK_COROUTINES) =="
wipe
export BULK_LOAD=true BULK_BATCH_SIZE="$BULK_BATCH" GRAPHITI_MAX_COROUTINES="$BULK_COROUTINES"
BULK_FLAG="--bulk"; B_WALL="$(run_load bulk)"
$PYRUN eval/graph_stats.py --repo "$REPO" --json "$OUT/after.json" >/dev/null
echo ">> B wall-clock: ${B_WALL}s"

A_WALL="$A_WALL" B_WALL="$B_WALL" $PYRUN - "$OUT/before.json" "$OUT/after.json" <<'PY'
import json, os, sys
before = json.load(open(sys.argv[1])); after = json.load(open(sys.argv[2]))
a = float(os.environ["A_WALL"]); b = float(os.environ["B_WALL"]) or 1e-9
st_b, st_a = before.get("structure", {}), after.get("structure", {})
tot = lambda d, k: sum(d.get(k, {}).values())
fr = lambda d: d.get("dedup_forking", {}).get("fork_rate")
ci = lambda d: d.get("citation_integrity", {}).get("integrity")
print("\n================= REL-61 ingest A/B =================")
print(f"wall-clock    A(seq)={a:>6.0f}s    B(bulk)={b:>6.0f}s    speedup={a/b:.2f}x")
print("\n-- quality gate (B must not regress vs A) --")
print(f"{'fork_rate':>22}:  A={fr(before)}   B={fr(after)}   (lower is better)")
print(f"{'citation_integrity':>22}:  A={ci(before)}   B={ci(after)}   (stay ~1.0)")
print(f"{'episodes':>22}:  A={st_b.get('episode_count')}   B={st_a.get('episode_count')}")
print(f"{'entities (total)':>22}:  A={tot(st_b,'entities_by_type')}   B={tot(st_a,'entities_by_type')}")
print(f"{'edges (total)':>22}:  A={tot(st_b,'edges_by_relation')}   B={tot(st_a,'edges_by_relation')}")
print("\nverdict: keep bulk if speedup>1 AND fork_rate not up AND citation_integrity not down.")
print(f"artifacts: {os.path.dirname(sys.argv[1])}\n")
PY
