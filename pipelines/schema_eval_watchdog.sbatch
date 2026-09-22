#!/usr/bin/env bash
#SBATCH --job-name=schema-eval-watchdog
#SBATCH --partition=cenia
#SBATCH --account=default-account
#SBATCH --qos=regular
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=00:30:00
#SBATCH --no-requeue
#SBATCH --open-mode=append
#SBATCH --chdir=/workspace1/users/ouhenio/mtg
#SBATCH --output=/workspace1/users/ouhenio/mtg/slurm-logs/slurm-%x-%j.out
#
# Resubmit one schema-eval arm after it ends badly. karn-research's runB_watchdog, per arm.
#
#   sbatch --dependency=afternotok:<job> schema_eval_watchdog.sbatch <job> <arm> <generation>
#
# A STRAND THAT ACTS, NOT ONE THAT RECORDS. Run B's resume ended NODE_FAIL with 20 seeds
# unplayed, the marker chain fired exactly as designed, and then nothing happened for two days.
# A marker reports a failure; it does not resubmit.
#
# `afternotok` is what makes a preemptible partition usable for a 6-hour run: preemption is a
# non-zero exit, this fires, the arm resumes from the engine record, and the cost of being
# killed at hour one is a resubmission rather than the run.
#
# SAFETY, because a watchdog that resubmits a permanently broken job is worse than none:
#   * generation cap;
#   * NO-PROGRESS GUARD -- if the complement did not SHRINK, the last attempt banked nothing
#     and resubmitting repeats it. Stop and say so.
#   * it exits if this arm still has a job PENDING or RUNNING: the loser of a one-per-partition
#     race exits CANCELLED, which is `notok`, and must not be read as the run having failed.
#   * it NEVER resubmits the other arm. The arms are separate measurements.
set -uo pipefail
JOB=${1:?usage: schema_eval_watchdog.sbatch <watched job> <guarded|unguarded> <generation>}
ARM=${2:?arm}
GEN=${3:?generation}
GEN_MAX=4
MTG=/workspace1/users/ouhenio/mtg
WT=/workspace1/users/ouhenio/mb-wt-schema
M=$(bash -c "source $MTG/env.sh >/dev/null 2>&1; echo \$MTG_DATA_ROOT")
PY=$MTG/.venv/bin/python
MARK=$M/evidence/markers; mkdir -p "$MARK"
say() { echo "[schema-watchdog $ARM gen$GEN] $*"; }
say "watching $JOB; state $(sacct -j "$JOB" -n -o State 2>/dev/null | head -1 | tr -d ' ')"

# 1. Someone else may still be running this arm -- including the winner of a partition race.
# BY NAME, NOT BY `scontrol`. A batch script's ARGUMENTS are not in `scontrol show job` -- only
# its path is -- so the first version of this grepped for "schema_eval_lascar.sbatch $ARM",
# matched nothing, and would have resubmitted an arm whose twin was running. The arm lives in
# the job NAME now and every submission passes `-J schema-eval-<arm>`.
live=$(squeue -h -u "$USER" -n "schema-eval-$ARM" -o '%i' 2>/dev/null | wc -l)
if [ "${live:-0}" -gt 0 ]; then
  say "another $ARM job is still PENDING/RUNNING -- not my turn, exiting"; exit 0
fi

# 2. THE BASE SUITE, rebuilt the same way the eval builds it, because the complement needs a
#    denominator and a hard-coded 200 would not notice if the base ever changed.
BASE=$MARK/schema-eval-base-suite.json
SUITE_IN=$M/evidence/eval/v3-h2h-4452-serve/suite.json
[ -f "$SUITE_IN" ] || { say "FATAL: 4452 suite unreadable at $SUITE_IN"; exit 3; }
$PY - "$SUITE_IN" "$BASE" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1]))
assert s["n_games"] == 200, f"expected 200, got {s['n_games']}"
stripped = sum(1 for g in s["games"] if g.pop("players", None) is not None)
assert stripped == 200, f"expected to strip all 200, stripped {stripped}"
json.dump(s, open(sys.argv[2], "w"), indent=1)
PYEOF
[ $? -eq 0 ] || { say "FATAL: base suite"; exit 3; }

# 3. The complement from the ENGINE record, keyed on the game index (this suite is
#    mirror-paired; a seed key drops the partner of every banked game).
COMP=$($PY "$WT/pipelines/schema_eval_complement.py" --arm "$ARM" \
          --evidence-root "$M/evidence/opd" --score-suite-dir "$MTG/pipelines/eval" \
          --base "$BASE" --count 2>&1)
say "unplayed games by the engine record: ${COMP:-UNKNOWN}"
case "$COMP" in ''|*[!0-9]*) say "FATAL: complement did not compute: $COMP"; exit 3 ;; esac
if [ "$COMP" -eq 0 ]; then say "arm=$ARM is COMPLETE -- nothing to resume"; exit 0; fi

PREV_F=$MARK/schema-eval-$ARM-prev-complement
PREV=$(cat "$PREV_F" 2>/dev/null || echo 999)
if [ "$COMP" -ge "$PREV" ]; then
  say "NO PROGRESS: complement $COMP did not shrink below $PREV -- the last attempt banked"
  say "  nothing, so resubmitting would repeat it. STOPPING. Human attention needed."
  echo "schema-eval $ARM watchdog STOPPED gen$GEN $(date -Is): complement $COMP >= previous $PREV" \
    > "$MARK/schema-eval-$ARM-watchdog-STOPPED-no-progress"
  exit 4
fi
echo "$COMP" > "$PREV_F"
if [ "$GEN" -ge "$GEN_MAX" ]; then
  say "generation cap $GEN_MAX reached with $COMP games left -- STOPPING rather than looping"
  echo "schema-eval $ARM watchdog STOPPED gen$GEN $(date -Is): generation cap, $COMP games left" \
    > "$MARK/schema-eval-$ARM-watchdog-STOPPED-gen-cap"
  exit 5
fi

# 4. Resubmit. The arm's sbatch recomputes the complement itself, so this passes no suite --
#    one source for what is unplayed, which is the job that will run it.
# THE GPU CAP IS 2 RUNNING ACROSS BOTH NODES (Eugenio, 2026-09-21). A watchdog that resubmits
# into a full budget does not break the cap -- the arm's own gate refuses and exits 9 -- but it
# burns a generation doing it, and the generation cap is what stops a loop. So wait instead:
# requeue THIS watchdog with a delay and try again, which costs nothing and keeps the count.
# T2I PRIORITY, CHECKED HERE TOO. The arm's own gate yields and exits 10, which is notok and
# would fire the NEXT watchdog -- so resubmitting into a t2i wait burns a generation per attempt
# and the generation cap, which exists to stop runaway loops, would instead stop the run. Waiting
# is not a retry, so this waits at the SAME generation, exactly as it does for a full budget.
# Only capacity-wait reasons count: every pending t2i job read `(Dependency)` on 2026-09-21, and
# blocking on those would be permanent rather than polite.
T2I=$(squeue -u "$USER" -h -t PENDING -o '%j|%R' 2>/dev/null \
      | awk -F'|' '$1 ~ /^t2i/ && $2 ~ /^\((Resources|Priority|ReqNodeNotAvail|Nodes required)/' | wc -l)
if [ "${T2I:-0}" -gt 0 ]; then
  if [ "$GEN" -ge "$GEN_MAX" ]; then
    say "t2i is waiting for capacity and I am at the generation cap -- STOPPING"; exit 5
  fi
  say "$T2I t2i job(s) waiting for capacity; t2i has priority. Waiting 30 min at gen $GEN."
  sbatch --parsable --begin=now+30minutes "$WT/pipelines/schema_eval_watchdog.sbatch" \
         "$JOB" "$ARM" "$GEN" && say "waiting watchdog queued"
  exit 0
fi

BUDGET=2
RUN=$(squeue -u "$USER" -h -t RUNNING -o '%b' 2>/dev/null \
      | awk '$0 ~ /gpu/ { n=$0; sub(/.*gpu[:=]/,"",n); sub(/[^0-9].*/,"",n);
                          s += (n=="" ? 1 : n) } END { print s+0 }')
say "GPU budget: $RUN running of $BUDGET"
if [ "$RUN" -ge "$BUDGET" ]; then
  if [ "$GEN" -ge "$GEN_MAX" ]; then
    say "budget full and at the generation cap -- STOPPING rather than waiting forever"; exit 5
  fi
  say "budget full; requeueing myself in 30 min at the SAME generation (waiting is not a retry)"
  sbatch --parsable --begin=now+30minutes "$WT/pipelines/schema_eval_watchdog.sbatch" \
         "$JOB" "$ARM" "$GEN" && say "waiting watchdog queued"
  exit 0
fi

say "resubmitting arm=$ARM (gen $((GEN+1)))"
NEXT=$(sbatch --parsable -J "schema-eval-$ARM" "$WT/pipelines/schema_eval_lascar.sbatch" "$ARM") \
  || { say "FATAL: sbatch"; exit 7; }
say "resubmitted as $NEXT"
sbatch --parsable --dependency=afternotok:"$NEXT" "$WT/pipelines/schema_eval_watchdog.sbatch" \
       "$NEXT" "$ARM" $((GEN+1)) && say "next-generation watchdog chained"
echo "schema-eval $ARM watchdog gen$GEN $(date -Is): $JOB failed with $COMP unplayed; resubmitted $NEXT" \
  > "$MARK/schema-eval-$ARM-watchdog-gen$GEN-acted"
