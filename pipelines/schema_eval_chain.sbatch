#!/usr/bin/env bash
#SBATCH --job-name=schema-eval-chain
#SBATCH --partition=cenia
#SBATCH --account=default-account
#SBATCH --qos=regular
#SBATCH --nodelist=lascar
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:10:00
#SBATCH --chdir=/workspace1/users/ouhenio/mtg
#SBATCH --output=/workspace1/users/ouhenio/mtg/slurm-logs/slurm-%x-%j.out
#
# Submit the GUARDED arm once the UNGUARDED one has finished, sized from what it actually used.
#
#   sbatch --dependency=afterany:<unguarded id> pipelines/schema_eval_chain.sbatch <id> <label>
#
# ARMS THE ACTION, NOT ONLY THE ALARM. LEDGER records a marker chain that fired correctly and
# then wrote a file a human had to read -- a smoke detector in an empty house. This one submits.
#
# `afterany`, not `afterok`: the first arm can end badly in ways that still leave a measurable
# evidence dir, and this job's own checks decide whether to proceed. A dependency that only
# fires on success would silently do nothing in exactly the cases worth reacting to.
#
# IT REFUSES TO LOOP. It submits at most one job and never itself. A chain that can resubmit a
# permanently broken job is worse than no chain.
set -uo pipefail

PREV_ID="${1:?usage: $0 <previous job id> <previous label>}"
PREV_LABEL="${2:?usage: $0 <previous job id> <previous label>}"
OVER=/workspace1/users/ouhenio/mtg-evidence-overflow
WT=/workspace1/users/ouhenio/mb-wt-schema
E=$OVER/$PREV_LABEL

log() { echo "[chain $(date +%H:%M:%S)] $*"; }
log "previous: $PREV_ID ($PREV_LABEL)"
log "state: $(sacct -j "$PREV_ID" -n -o State%20 2>/dev/null | head -1 | tr -d ' ')"

# --- DID IT BANK ANYTHING? The artifact, not the exit code. -----------------------------------
# A suite can end for many reasons and its wrapper's code reports on the wrapper. What decides
# whether a second arm is worth six hours is whether the first one produced games.
if [ ! -d "$E" ]; then
  log "REFUSING: no evidence dir at $E. The first arm banked nothing; a second arm would have"
  log "  nothing to pair against."
  exit 1
fi
ENDED=$(find "$E" -name 'server_game_events.jsonl' -exec grep -l '"game_end"' {} + 2>/dev/null | wc -l)
DIRS=$(find "$E" -maxdepth 1 -name 'g*' -type d 2>/dev/null | wc -l)
log "first arm: $DIRS game dirs, $ENDED with an engine game_end"
if [ "$ENDED" -lt 100 ]; then
  log "REFUSING: only $ENDED games reached game_end, of 200 expected. Pairing a full guarded arm"
  log "  against a half-finished baseline answers a different question than the one registered."
  log "  Investigate the first arm; resubmit by hand when it is understood."
  exit 1
fi

# --- SIZE THE SECOND ARM FROM THE FIRST, NOT FROM A DIFFERENT RUN -----------------------------
USED_GB=$(( $(du -s --block-size=1G "$E" 2>/dev/null | cut -f1) ))
NEED_GB=$(( USED_GB * 12 / 10 ))          # measured + 20%
AVAIL_GB=$(( $(df --output=avail -k "$OVER" | tail -1 | tr -d ' ') / 1024 / 1024 ))
log "first arm used ${USED_GB} GB; need >= ${NEED_GB} GB (measured +20%); free at destination ${AVAIL_GB} GB"
if [ "$AVAIL_GB" -lt "$NEED_GB" ]; then
  log "REFUSING: ${AVAIL_GB} GB free is under the ${NEED_GB} GB the first arm's own size implies."
  log "  The second arm would die partway with a full quota, which is the failure this chain"
  log "  exists to prevent. Free space at $OVER (or reopen the data root) and submit by hand:"
  log "    sbatch $WT/pipelines/schema_eval_lascar.sbatch guarded"
  exit 1
fi

# --- one submission, never itself --------------------------------------------------------------
if squeue -u ouhenio -n schema-eval -h -o '%j %T' 2>/dev/null | grep -q guarded; then
  log "REFUSING: a guarded arm is already queued or running. Not submitting a second."
  exit 0
fi
NEW=$(sbatch --parsable "$WT/pipelines/schema_eval_lascar.sbatch" guarded 2>&1)
rc=$?
if [ $rc -ne 0 ]; then log "FATAL: sbatch failed: $NEW"; exit 1; fi
log "submitted GUARDED arm as $NEW"
log "  pair at readout by seed against $PREV_LABEL"
exit 0
