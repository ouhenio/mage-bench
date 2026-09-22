#!/usr/bin/env bash
#SBATCH --job-name=schema-eval
#SBATCH --partition=lascar
#SBATCH --account=latamgpt
#SBATCH --qos=lascar-unlimit
#SBATCH --nodelist=lascar
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=32
#SBATCH --mem-per-gpu=80G
#SBATCH --time=08:00:00
#SBATCH --no-requeue
#SBATCH --open-mode=append
# t2i OUTRANKS MTG BY CONSTRUCTION (CLAUDE.md 998f90d). The start-and-exit yield below hands a
# freed card to whichever job slurm ranks next, not to t2i; a low priority is what actually
# puts t2i first. The pending-t2i gate stays as belt-and-braces. Measured when this went in:
# it moves this job's --test-only start estimate from 2026-09-22 08:21 to 2026-10-02 03:51,
# because it ranks below every other tenant as well as below t2i. --nice=100 did not move it.
#SBATCH --nice=10000
#SBATCH --chdir=/workspace1/users/ouhenio/mtg
#SBATCH --output=/workspace1/users/ouhenio/mtg/slurm-logs/slurm-%x-%j.out
#
# ckpt-265 vs the engine on 4452's seeds, ONE ARM PER JOB.
#
#   sbatch ... pipelines/schema_eval_lascar.sbatch <guarded|unguarded>
#
# THE ONE VARIABLE IS THE PER-DECISION ENUM. Both arms carry the name guard
# (MAGEBENCH_TOOL_NAME_GUARD=structural_tag), the same preset, the same seeds, the same
# concurrency, the same engine opponent. Only MAGEBENCH_DECISION_SCHEMA differs.
#
# WHY TWO JOBS AND NOT TWO SEATS. The schema is read from the process environment, so a single
# game cannot host one guarded seat and one unguarded one. The arms are therefore two suites on
# the SAME seed set, paired at readout by seed -- which is also how 4056's arms were compared.
#
# THE ENGINE IS AT SKILL 1, as every eval in this project has been. karn-research established on
# 2026-09-19 that the search budget was never SET, not merely never recorded, so skill 1 is the
# engine's floor: maxNodes 5000, maxThinkTimeSecs 3, maxDepth 4. That is not a neutral choice and
# the readout says so -- the budget curve is a later decision and this eval does not pre-empt it.
set -uo pipefail

ARM="${1:?usage: $0 <guarded|unguarded> [game limit; 0 = all, 1 = the one-deal smoke]}"
case "$ARM" in guarded|unguarded) : ;; *) echo "FATAL: arm must be guarded|unguarded"; exit 2 ;; esac
# THE ONE-DEAL SMOKE IS THIS JOB WITH N=1, not a second script. Four submissions in a row died
# only once games were launching -- no jars, a display collision, the base preset, an
# unregistered model -- and every one of them would have shown itself in a single game. The
# watchdog covers the first two minutes; a smoke covers the first GAME, and it must reach an
# engine `game_end` to mean anything. Running it on this exact sbatch is the point: a smoke on
# a different script proves that script works.
LIMIT="${2:-0}"
case "$LIMIT" in ''|*[!0-9]*) echo "FATAL: game limit must be a non-negative integer"; exit 2 ;; esac

# --- GPU BUDGET: AT MOST 2 RUNNING GPU JOBS ACROSS BOTH NODES --------------------------------
# Eugenio, 2026-09-21, relayed by the orchestrator: "U can use at most 2 gpus." Queued twins are
# fine; what is capped is what RUNS. lascar and ranokau are ONE slurm cluster, so a single
# squeue sees both and no cross-node bookkeeping is needed.
#
# SUBMIT THIS JOB AS `-J schema-eval-<arm>`. `scontrol show job` does NOT carry a batch script's
# arguments -- only its path -- so the arm is invisible to every check unless it is in the NAME.
# An earlier version of the watchdog grepped `scontrol` for the arm, matched nothing, and would
# have resubmitted an arm while its twin was running.
NODE=$(hostname -s)

# --- T2I HAS PRIORITY ON THE NODE WE ARE ABOUT TO TAKE A CARD ON --------------------------------
# Eugenio, verbatim: "2 for u, 2 for t2i (although they have the priority)". So an MTG GPU job
# yields to a t2i job that is WAITING FOR CAPACITY. Running arms are never cancelled.
#
# "WAITING FOR CAPACITY" IS THE WHOLE CHECK, and a gate on "any t2i job PENDING" would be a
# permanent block rather than a courtesy: measured at 22:2x, all EIGHT pending t2i jobs read
# `(Dependency)` -- they are chained behind 9809 and are not competing for a card at all. That
# chain always has pending dependents, so blocking on their existence would mean MTG never runs
# again. Only the reasons that mean "there is no room" count.
T2I_BLOCKING=$(squeue -u "$USER" -h -t PENDING -o '%i|%j|%R' 2>/dev/null \
  | awk -F'|' '$2 ~ /^t2i/ && $3 ~ /^\((Resources|Priority|ReqNodeNotAvail|Nodes required)/ {print $1}' \
  | while read -r i; do
      req=$(scontrol show job "$i" 2>/dev/null | grep -oP 'ReqNodeList=\K[^ ]+')
      case "${req:-any}" in "$NODE"|any|"(null)") echo "$i" ;; esac
    done | tr '\n' ' ')
if [ -n "${T2I_BLOCKING// /}" ]; then
  echo "YIELDING: t2i job(s)${T2I_BLOCKING:+ }$T2I_BLOCKING are PENDING for capacity on $NODE and"
  echo "  t2i has priority (Eugenio, 2026-09-21). Not taking a card."
  echo "  Exiting NON-ZERO so the afternotok watchdog retries this arm when t2i is served."
  echo "  NOTE: the instruction said 'exit 0; the watchdog retries'. Those are incompatible --"
  echo "  an afternotok dependency does not fire on 0, so exit 0 would DROP the arm silently,"
  echo "  which is LEDGER 75 exactly. Exit 0 is reserved for the race loser, where there is"
  echo "  genuinely nothing left to do because the other instance is already running."
  exit 10
fi

# THE CAP IS SCOPED TO MTG WORK, AND THE TOTAL IS RECORDED BESIDE IT.
# Eugenio's words were "U can use at most 2 gpus"; the orchestrator scoped that to "RUNNING MTG
# GPU jobs sum to <= 2 across both nodes". The same unix user also runs the t2i work, which held
# one GPU when these arms were submitted -- so the two readings differ by exactly that job and
# the difference decides whether the second arm may start. This gate enforces the scoped reading
# and LOGS BOTH NUMBERS, so if the total was meant, the evidence says so in the job's own record
# rather than having to be reconstructed.
BUDGET=2
MTG_JOB_RE='^(schema-eval|budget-curve|teacher-|corpus-|opd-|prepare-harness)'
gpu_running() {   # $1 = "mtg" or "all"; our RUNNING GPU jobs, excluding this one
  squeue -u "$USER" -h -t RUNNING -o '%i|%j|%b' 2>/dev/null \
    | awk -F'|' -v self="${SLURM_JOB_ID:-0}" -v scope="$1" -v re="$MTG_JOB_RE" '
        $1 == self || $3 !~ /gpu/ { next }
        scope == "mtg" && $2 !~ re { next }
        { n=$3; sub(/.*gpu[:=]/,"",n); sub(/[^0-9].*/,"",n); s += (n=="" ? 1 : n) }
        END { print s+0 }'
}
TWIN=$(squeue -u "$USER" -h -t RUNNING -n "schema-eval-$ARM" -o '%i' 2>/dev/null \
         | grep -v "^${SLURM_JOB_ID:-0}$" | head -1)
if [ -n "$TWIN" ]; then
  echo "arm=$ARM is already RUNNING as job $TWIN -- I am the loser of the partition race."
  echo "Exiting 0: this is not a failure and no watchdog should resubmit it."
  exit 0
fi
RUN=$(gpu_running mtg)
ALL=$(gpu_running all)
echo "GPU in use by this user: $RUN MTG, $ALL total (cap $BUDGET, scoped to MTG)"
if [ "$RUN" -ge "$BUDGET" ]; then
  echo "FATAL: $RUN MTG GPU job(s) are RUNNING and the cap is $BUDGET."
  echo "  Exiting NON-ZERO on purpose, so the afternotok watchdog resubmits this arm later"
  echo "  rather than the arm being silently dropped."
  exit 9
fi
echo "GPU budget: $RUN MTG running of $BUDGET ($ALL total incl. other projects); taking one for arm=$ARM"

MTG=/workspace1/users/ouhenio/mtg
SRC=$MTG
M=$(bash -c "source $MTG/env.sh >/dev/null 2>&1; echo \$MTG_DATA_ROOT")

# --- PER-NODE PATHS, RESOLVED AND REFUSED, NEVER GUESSED ---------------------------------------
# lascar and ranokau have SEPARATE filesystems at the SAME paths, and each reaches the other only
# over the cross-mount. Three things this job needs live on lascar and must be reached differently
# depending on where it runs: the harness tree, its maven repo, and the checkpoint + 4452's suite.
# Nothing here falls back -- a node this script has not been taught about refuses, because the
# failure mode of guessing is reading the OTHER node's path and silently running other bytes.
# NODE was resolved above, for the t2i priority check.
case "$NODE" in
  lascar)
    WT=/workspace1/users/ouhenio/mb-wt-schema
    M2=/workspace1/users/ouhenio/m2-schema
    MODEL_ROOT=$MTG/models
    SUITE_SRC=$M                                  # 4452 lives on lascar's own data root
    OTHER_OPD=/workspace1-ranokau/projects/posttrainlatamgpt/ouhenio/mtg/evidence/opd ;;
  ranokau)
    WT=/workspace1/users/ouhenio/mtg/wt-schema
    M2=/workspace1/users/ouhenio/mtg/m2-schema
    # vLLM reads weights over NFS; the load is slower and the job log records how much.
    MODEL_ROOT=/workspace1-lascar/users/ouhenio/mtg/models
    SUITE_SRC=/workspace1-lascar/projects/posttrainlatamgpt/users/ouhenio/mtg
    OTHER_OPD=/workspace1-lascar/projects/posttrainlatamgpt/users/ouhenio/mtg/evidence/opd ;;
  *) echo "FATAL: this script knows lascar and ranokau; it is running on '$NODE'"; exit 2 ;;
esac
[ -d "$WT" ] || { echo "FATAL: no harness tree at $WT on $NODE"; exit 2; }
[ -d "$OTHER_OPD" ] || { echo "FATAL: the other node's evidence is unreadable at $OTHER_OPD."; \
  echo "  The complement needs EVERY root: one skipped is banked games replayed."; exit 2; }
PY=$MTG/.venv/bin/python
# LABEL IS SET AFTER THE COMPLEMENT, because whether this is a fresh run or a resume is a fact
# about what the arm has already banked, not something the caller declares. A job that names
# itself before it has looked can name itself wrong.

# EVIDENCE UNDER THE DATA ROOT, which reopened on 2026-09-21 (445 GB free after a prune).
#
# AND THE PATH IS THE RUNNER'S, NOT MINE. Job 9396 exported MAGEBENCH_EVIDENCE_DIR expecting the
# suite to land on an overflow path; `run_suite_slots.sh` derives its own from the label and the
# data root and does not read that variable, so the redirection was INERT -- the run wrote to the
# data root anyway and the flag gate afterwards looked in the empty place and said "no such
# evidence dir". An env var the consumer does not read is indistinguishable from one that works
# until something downstream reads the other path. This now names the runner's own path.
# SERVE is keyed on the job id alone, so it exists before the label does and two jobs never
# share it.
SERVE=$M/evidence/eval/schema-eval-$ARM-$SLURM_JOB_ID-serve
mkdir -p "$SERVE" || { echo "FATAL: cannot create $SERVE"; exit 1; }

log() { echo "[schema-eval $(date +%H:%M:%S)] $*"; }

# --- SPACE, ASSERTED AT THE DESTINATION AND SIZED FROM A REAL RUN ----------------------------
# 4452 -- the same 200-deal mirror-paired suite, the same models -- produced 12 GB of evidence.
# So one arm needs ~12 GB and the pair needs ~24 GB. /workspace1 is xfs with PROJECT QUOTAS:
# `df /workspace1` reports terabytes while this destination has its own 200 GB allocation, so
# the check must run HERE. A job that dies at 80% of a 6-hour suite because the quota filled is
# the worst version of the failure we already had at 2 MB.
NEED_GB=30
AVAIL_GB=$(( $(df --output=avail -k "$M" | tail -1 | tr -d ' ') / 1024 / 1024 ))
log "free at $M: ${AVAIL_GB} GB (need >= ${NEED_GB}; 4452 measured 12 GB for this suite)"
[ "$AVAIL_GB" -ge "$NEED_GB" ] || { log "FATAL: not enough room at the data root"; exit 1; }

# --- provenance -------------------------------------------------------------------------------
for f in src/magebench/pilot/decision_schema.py src/magebench/pilot/tool_name_guard.py; do
  d=$(git -C "$WT" hash-object "$WT/$f"); h=$(git -C "$WT" rev-parse "HEAD:$f" 2>/dev/null || echo "?")
  printf "blob %-46s %s %s\n" "$f" "${d:0:12}" "$([ "$d" = "$h" ] && echo MATCHES-HEAD || echo DIFFERS)"
done
log "harness $WT @ $(git -C "$WT" rev-parse --short HEAD) ($(git -C "$WT" rev-parse --abbrev-ref HEAD))"

export HARNESS_REPO=$WT MAVEN_REPO_LOCAL=$M2
FLOOR=$("$HARNESS_REPO/.venv/bin/python" -c \
  "from magebench.pilot.context_segments import SERVE_MIN_MODEL_LEN as m; print(m)" 2>&1) \
  || { log "FATAL: HARNESS_REPO cannot read the harness floor: $FLOOR"; exit 1; }
log "harness floor $FLOOR"

# THE JARS THE GAME JVMs RESOLVE FROM, ASSERTED BEFORE A GPU IS SPENT ON THEM.
# Job 9494 ran twelve games at 240 s each and banked ZERO: every XMage server died on
# "Could not find artifact org.mage:mage:jar:1.4.58 ... in $MAVEN_REPO_LOCAL". The repo
# existed -- 51 MB of third-party deps -- and held NO org.mage module at all, because this
# HARNESS_REPO is a commit-only submodule worktree, which by this repo's own rule "has no
# JDK, Maven, Xvfb, venv or built jars". The controls never caught it because they replay
# recorded frames against a served model and start no engine; this is the first job on the
# branch that launches games.
#
# A DIRECTORY THAT EXISTS IS NOT A REPOSITORY THAT RESOLVES. The failure is also reported
# per game, 240 s at a time, so without this check the job spends its whole allocation
# rediscovering one missing build.
MAGE_ARTIFACTS=$(ls "$MAVEN_REPO_LOCAL/org/mage" 2>/dev/null | wc -l)
log "maven repo $MAVEN_REPO_LOCAL: $MAGE_ARTIFACTS org.mage artifacts"
[ "$MAGE_ARTIFACTS" -ge 38 ] || {
  log "FATAL: $MAVEN_REPO_LOCAL holds $MAGE_ARTIFACTS org.mage artifacts, expected >= 38."
  log "  Build the tree first:  sbatch $SRC/pipelines/eval/prepare_harness_lascar.sbatch \\"
  log "                             $WT $MAVEN_REPO_LOCAL <expect_fail> <required tests>"
  exit 1
}

# --- the one variable, and everything held constant around it ---------------------------------
export MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY=1
export MAGEBENCH_DECK_BLOCK=on
export MAGEBENCH_CONTEXT_LIMIT=143360
export MAGEBENCH_MULLIGAN=engine-rule
export MAGEBENCH_GAME_TIMEOUT=3600
unset MAGEBENCH_INJECT_CONTENT
# BOTH ARMS CARRY THE NAME GUARD. It is deployed, it is not what this eval tests, and holding it
# constant is what makes the enum the single variable.
export MAGEBENCH_TOOL_NAME_GUARD=structural_tag

# THE PRESET, SET EXPLICITLY, BECAUSE THE RUNNER HAS A SILENT DEFAULT AND THE SUITE NO
# LONGER OVERRIDES IT. `run_suite_slots.sh:148` reads
# `MTG_PILOT_PRESET="${MTG_PILOT_PRESET:-q35-4b-local-strategy}"`, and 4452 never set it
# because its `players` key named the preset per seat. Stripping `players` -- which this
# job must do, since 4452 was a head-to-head and this is one model against the engine --
# also removed the only thing that named `q35-4b-ckpt265-strategy`, so job 9494 fell back
# to the BASE 4B preset. The two presets differ in exactly one field, the model, so every
# line the log prints about them is identical; the fall-back is invisible by construction.
# karn-research hit the same fall-back on the budget curve, from the same `players` strip.
export MTG_PILOT_PRESET=q35-4b-ckpt265-strategy
if [ "$ARM" = guarded ]; then
  export MAGEBENCH_DECISION_SCHEMA=bound_choice
else
  unset MAGEBENCH_DECISION_SCHEMA
fi
log "arm=$ARM  MAGEBENCH_DECISION_SCHEMA=${MAGEBENCH_DECISION_SCHEMA:-<unset>}  guard=structural_tag"

# --- 4452's seeds, sliced from 4452's own suite ------------------------------------------------
SUITE_IN=$SUITE_SRC/evidence/eval/v3-h2h-4452-serve/suite.json
[ -f "$SUITE_IN" ] || { log "FATAL: 4452 suite unreadable at $SUITE_IN"; exit 1; }
SUITE_FULL=$SERVE/suite_full.json
$PY - "$SUITE_IN" "$SUITE_FULL" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1]))
assert s["n_games"] == 200, f"expected 4452's 200 games, got {s['n_games']}"
lo, hi = s["games"][0]["seed"], s["games"][-1]["seed"]
# STRIP THE SEATS. 4452 was a HEAD-TO-HEAD, so every game names two policy seats and the second
# is `provider: local_b`. run_suite_slots.sh uses a suite's `players` verbatim when present --
# deliberately, so a suite that names its seats owns them -- and this eval serves ONE model
# against the engine, so `local_b` resolves to nothing: job 9396 died in 2.5 minutes with
# `AssertionError: Unknown player provider 'local_b'` on all 36 games it launched.
#
# "4452's suite unmodified" was my phrase and it was wrong: what this eval reuses is 4452's
# SEEDS AND DECKS, not its seating. Removing `players` makes the runner fall back to
# pilot@local vs the XMage AI, which is the comparison registered.
stripped = 0
for g in s["games"]:
    if g.pop("players", None) is not None:
        stripped += 1
print(f"stripped h2h seats from {stripped} games -> pilot@local vs XMage AI")
assert stripped == s["n_games"], f"expected to strip all {s['n_games']}, stripped {stripped}"
s["derived_from"] = ("v3-h2h-4452 suite: seeds and decks reused, h2h seats REMOVED so the "
                     "runner seats ckpt-265 against the engine")
json.dump(s, open(sys.argv[2], "w"), indent=1)
print(f"suite: {s['n_games']} games, seeds {lo}-{hi}, mirror={s.get('mirror')}")
PYEOF
[ $? -eq 0 ] || { log "FATAL: suite slice"; exit 1; }

# --- RESUME: the complement of what THIS ARM has already banked, by the engine record ---------
# karn-research's run B pattern, generalised to an arm. A preemptible partition is only usable
# for a 6-hour run if being killed at hour one costs a resubmission rather than the run, and
# that is entirely a property of this block: without it the job refuses on "evidence dir
# exists" and the banked games are stranded (run B, 7927).
#
# BANKED IS THE ENGINE'S `game_end`, never the `exit` marker, which run B measured wrong in both
# directions. And the key is the GAME INDEX, not the seed: this suite is mirror-paired, two
# games per seed, so a seed-keyed complement marks a seed done when either half finishes and
# drops the other. Measured on 4452's evidence: 187 banked games over 98 seeds, and the
# seed-keyed version called that 4 games unplayed instead of 13.
SUITE=$SERVE/suite.json
# GAMES KNOWN TO FAIL DETERMINISTICALLY are excluded, never counted as banked. Seeds 1200062 and
# 1200068 freeze the XMage AI's search in both arms and both seatings (Ad Nauseam mirrors), and
# 1200082 dies without a freeze warning (KikiPod) -- base games 122,123,135,162,163. Replaying them
# re-fails them and burns a GPU each. Passed explicitly and printed, so the job record says which
# games the design gave up on and the readout's n stays honestly short of 200.
EXCLUDE="${MAGEBENCH_EXCLUDE_GAMES:-}"
[ -n "$EXCLUDE" ] && log "EXCLUDING base games $EXCLUDE (known deterministic failures; not banked)"
COMP_ARGS=(--arm "$ARM" --evidence-root "$M/evidence/opd" --evidence-root "$OTHER_OPD" \
           --score-suite-dir "$SRC/pipelines/eval" --base "$SUITE_FULL" \
           ${EXCLUDE:+--exclude-games "$EXCLUDE"})
UNPLAYED=$("$PY" "$WT/pipelines/schema_eval_complement.py" "${COMP_ARGS[@]}" --count 2>&1) \
  || { log "FATAL: complement did not compute: $UNPLAYED"; exit 1; }
case "$UNPLAYED" in ''|*[!0-9]*) log "FATAL: complement returned '$UNPLAYED'"; exit 1 ;; esac
# NOT `200 - UNPLAYED`. With games excluded, that arithmetic counts every excluded game as banked
# -- it would have logged "already banked 190 of 200" when 185 were -- which is the one
# confusion the complement's three-way partition exists to prevent. Banked comes from the
# complement's own count of banked games, read off its summary line.
N_EXCL=0; [ -n "$EXCLUDE" ] && N_EXCL=$(printf '%s' "$EXCLUDE" | tr ',' '\n' | grep -c .)
BANKED=$(( 200 - UNPLAYED - N_EXCL ))
log "arm=$ARM already banked $BANKED of 200 games; $UNPLAYED unplayed; $N_EXCL excluded"
if [ "$UNPLAYED" -eq 0 ]; then
  log "arm=$ARM is COMPLETE -- nothing to run. Exiting 0 so a watchdog does not resubmit it."
  exit 0
fi
"$PY" "$WT/pipelines/schema_eval_complement.py" "${COMP_ARGS[@]}" \
      --out "$SUITE" --limit "$LIMIT" 2>&1 | sed 's/^/    /'
[ "${PIPESTATUS[0]}" -eq 0 ] || { log "FATAL: complement suite"; exit 1; }

# THE LABEL NOW SAYS WHICH IT IS, and a resume never reuses a dir -- the readout pools by seed
# across every dir the arm owns, so each attempt's evidence has to stay separable.
if [ "$BANKED" -gt 0 ]; then LABEL=schema-eval-$ARM-resume-$SLURM_JOB_ID
else                         LABEL=schema-eval-$ARM-$SLURM_JOB_ID; fi
[ "$LIMIT" -gt 0 ] && LABEL=$LABEL-smoke$LIMIT
E=$M/evidence/opd/$LABEL
[ -e "$E" ] && { log "FATAL: $E exists"; exit 2; }
log "label $LABEL  evidence $E"

# --- serve ckpt-265 -----------------------------------------------------------------------------
. "$SRC/pipelines/resolve_venv.sh"
SERVE_VENV=$(resolve_vllm_venv) || exit 3
MODEL=$MODEL_ROOT/mtg-v3-sft-checkpoint-265
SERVED_NAME=mtg-ckpt-265
PORT=$(( 8400 + SLURM_JOB_ID % 200 ))

# PRESET.MODEL == SERVED NAME, ASSERTED BEFORE A GPU IS SPENT.
# The preset names the model string the pilot puts in every request; the serve names what
# the server will answer to. NOTHING JOINS THEM. When they disagree the job still starts,
# still serves, still launches games, and the disagreement surfaces only as a 404 per LLM
# call (job 9396) -- or, if the wrong name happens to resolve, as 200 games of a model
# nobody registered. A subject is not the subject because the serve log names it.
PRESET_MODEL=$("$WT/.venv/bin/python" "$WT/pipelines/preset_model.py" \
                     "$WT" "$MTG_PILOT_PRESET" 2>&1) \
  || { log "FATAL: $PRESET_MODEL"; exit 1; }
log "preset $MTG_PILOT_PRESET -> model '$PRESET_MODEL'; this job serves '$SERVED_NAME'"
[ "$PRESET_MODEL" = "$SERVED_NAME" ] || {
  log "FATAL: the preset asks for '$PRESET_MODEL' and this job serves '$SERVED_NAME'."
  log "  Those must be the same string or every LLM call 404s. The registered subject is"
  log "  ckpt-265; job 9494 ran ten minutes with the base-4B preset on exactly this gap."
  exit 1
}
"$SERVE_VENV/bin/python" "$SRC/pipelines/rlvr/check_servable.py" "$MODEL" 2>&1 \
  | tee "$SERVE/check_servable.txt" || { log "FATAL: check_servable"; exit 3; }
MTG_SERVED_NAME=$SERVED_NAME OPD_LOG_DIR=$SERVE \
  bash "$SRC/pipelines/opd/serve_student.sh" slurm "$PORT" "$MODEL" \
  || { log "FATAL: serve"; exit 1; }
log "ckpt-265 up on $PORT (weights from $MODEL_ROOT; on ranokau that is NFS)"; cat "$SERVE"/served_*.id 2>/dev/null | sed 's/^/    served: /'

# --- PIN THE RUNNER BYTES THIS JOB ACTUALLY RAN ------------------------------------------------
# The sbatch comes from $WT, whose commit is recorded above, but the RUNNER and its helpers come
# from $SRC -- the executing node's TRUNK CHECKOUT, which no session here owns and which another
# session may `pull` mid-run. "main @ <sha>" is not a record of what ran: trunk moves, and a job
# runs whatever is on disk, committed or not.
#
# So the bytes are pinned two ways. A MANIFEST of blob hashes for every trunk script this job
# invokes, and a verbatim COPY of the runner itself -- because a hash tells you the bytes
# changed and a copy tells you what they were.
PIN=$SERVE/runner-pin
mkdir -p "$PIN"
{
  echo "node       $NODE"
  echo "trunk      $SRC"
  echo "trunk HEAD $(git -C "$SRC" rev-parse HEAD 2>/dev/null) ($(git -C "$SRC" rev-parse --abbrev-ref HEAD 2>/dev/null))"
  echo "tracked files modified in trunk: $(git -C "$SRC" status --porcelain --untracked-files=no | wc -l)"
  echo "harness    $WT @ $(git -C "$WT" rev-parse HEAD)"
  echo "--- blob of every trunk script this job invokes ---"
  for f in pipelines/opd/run_suite_slots.sh pipelines/eval/run_suite.sh \
           pipelines/opd/serve_student.sh pipelines/rlvr/check_servable.py \
           pipelines/resolve_venv.sh pipelines/synth/watch_autoresolve.sh \
           pipelines/eval/score_suite.py; do
    printf "%-44s %s\n" "$f" "$(git -C "$SRC" hash-object "$SRC/$f" 2>/dev/null || echo MISSING)"
  done
} > "$PIN/manifest.txt"
cp "$SRC/pipelines/opd/run_suite_slots.sh" "$PIN/run_suite_slots.sh.asrun" 2>/dev/null \
  || log "WARNING: could not copy the runner into the pin"
log "runner pinned at $PIN"
sed 's/^/    /' "$PIN/manifest.txt"

CONC=12
export MAGEBENCH_DISP_WIDTH=$((CONC * 4))    # 4x, not 2x -- 4056 lost 64 games to lapping

# THE FIFTH ARGUMENT IS THE DISPLAY BASE, NOT THE WIDTH. `run_suite_slots.sh` reads
# `DISP0="${5:-90}"` and takes the width from MAGEBENCH_DISP_WIDTH in the environment.
# Job 9495 passed the WIDTH (48) in the base's position, so both arms allocated :48-:95
# identically; 9494 started a second earlier and took :48-:59, and 9495 died in 3 m 48 s
# printing eleven "FATAL: display :NN is live".
#
# The port was already derived per job and the display was not -- and a shared resource
# derived on one axis only is not derived. Both are now functions of the job id, which is
# also what makes a running arm killable by identity rather than by name.
DISP0=$(( 1000 + (SLURM_JOB_ID % 20) * 60 ))   # stride 60 > width 48, so adjacent job ids
                                               # cannot overlap; >= 1000 clears :100-:275
                                               # (the budget curve's five arms), :200-:455
                                               # and :500-:671 (corpus blocks).
log "displays :$DISP0-:$((DISP0 + MAGEBENCH_DISP_WIDTH - 1))  port $PORT"
MTG_DATA_ROOT=$M \
  bash "$SRC/pipelines/opd/run_suite_slots.sh" "$SUITE" "$LABEL" "$PORT" "$CONC" "$DISP0"
rc=$?
log "runner returned $rc -- the wrapper's code is NOT the verdict; read the artifacts"

# The pin also lands IN the evidence dir, which is what a later reader opens. Best-effort and
# after the fact, because the runner owns that dir and refuses if it already exists -- so
# $SERVE/runner-pin above is the copy that is guaranteed to exist even if the job is preempted.
[ -d "$E" ] && cp -r "$PIN" "$E/runner-pin" 2>/dev/null && log "pin copied into $E"

# --- A SMOKE MUST PROVE THE MECHANISM, NOT MERELY SURVIVE -------------------------------------
# Job 9960 ran 200 games with MAGEBENCH_DECISION_SCHEMA=bound_choice and exited cleanly while
# binding NOTHING -- 487 of 487 decisions recorded `no decision blob` -- so a "guarded" arm was a
# second copy of the control. An `afterok` chain on that smoke would have been satisfied.
#
# So when this job is a smoke (LIMIT>0) on the guarded arm, it FAILS unless the coverage rows
# show at least one bound decision. That makes exit 0 mean "the mechanism ran", which is the only
# reading under which chaining the 200-deal arm behind it is honest. The unguarded arm emits no
# coverage rows by design and is not checked.
if [ "$LIMIT" -gt 0 ] && [ "$ARM" = guarded ]; then
  BOUND=$("$PY" - "$E" <<'PYEOF'
import json, pathlib, sys
bound = unbound = 0
reasons = {}
for f in pathlib.Path(sys.argv[1]).rglob("*_llm.jsonl"):
    for line in f.open():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("type") != "decision_schema_coverage":
            continue
        if r.get("bound"):
            bound += 1
        else:
            unbound += 1
            reasons[r.get("reason")] = reasons.get(r.get("reason"), 0) + 1
# TWO LINES, not one: the reasons are JSON and JSON contains spaces. Printed on one line and
# split with `set --`, `{"respond_with declares no choice=id form": 5}` became `{"respond_with`
# in the job log -- the count was parsed correctly and the human-readable half was silently
# truncated at the first space. A log that is wrong in a way the code does not notice is the
# shape this whole eval keeps tripping over.
print(f"{bound} {unbound}")
print(json.dumps(reasons))
PYEOF
)
  BOUND_N=$(printf '%s\n' "$BOUND" | sed -n 1p | cut -d' ' -f1)
  UNBOUND_N=$(printf '%s\n' "$BOUND" | sed -n 1p | cut -d' ' -f2)
  log "smoke coverage: bound=$BOUND_N unbound=$UNBOUND_N"
  log "smoke unbound reasons: $(printf '%s\n' "$BOUND" | sed -n 2p)"
  if [ "${BOUND_N:-0}" -lt 1 ] ; then
    log "FATAL: the guarded smoke bound ZERO decisions. The schema env was set and the"
    log "  mechanism did not engage -- this is job 9960's defect, where the binding site read a"
    log "  field the message builder had already consumed. Refusing to let an afterok chain"
    log "  start a 200-deal arm that would measure the control twice."
    exit 11
  fi
  log "smoke PROVED the mechanism: $BOUND_N bound decision(s) in one game"
fi

log "flag gate"; bash "$SRC/pipelines/synth/watch_autoresolve.sh" "$E" 2>&1 | sed 's/^/    /'
log "free at $M after the run: $(df -h --output=avail "$M" | tail -1 | tr -d ' ')"
log "schema eval end -- arm=$ARM evidence $E"
PGID=$(cat "$SERVE/serve_$PORT.pgid" 2>/dev/null)
[ -n "$PGID" ] && kill -- -"$PGID" 2>/dev/null
exit $rc
