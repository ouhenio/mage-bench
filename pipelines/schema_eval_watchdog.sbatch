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
live=$(squeue -h -u "$USER" -n schema-eval -o '%i' 2>/dev/null | while read -r i; do
         scontrol show job "$i" 2>/dev/null | grep -q "schema_eval_lascar.sbatch $ARM" && echo "$i"; done | wc -l)
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
say "resubmitting arm=$ARM (gen $((GEN+1)))"
NEXT=$(sbatch --parsable "$WT/pipelines/schema_eval_lascar.sbatch" "$ARM") || { say "FATAL: sbatch"; exit 7; }
say "resubmitted as $NEXT"
sbatch --parsable --dependency=afternotok:"$NEXT" "$WT/pipelines/schema_eval_watchdog.sbatch" \
       "$NEXT" "$ARM" $((GEN+1)) && say "next-generation watchdog chained"
echo "schema-eval $ARM watchdog gen$GEN $(date -Is): $JOB failed with $COMP unplayed; resubmitted $NEXT" \
  > "$MARK/schema-eval-$ARM-watchdog-gen$GEN-acted"
