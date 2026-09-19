#!/usr/bin/env bash
#SBATCH --job-name=schema-controls
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=12
#SBATCH --mem-per-gpu=80G
#SBATCH --time=01:00:00
#SBATCH --chdir=/workspace1/users/ouhenio/mtg
#SBATCH --output=/workspace1/users/ouhenio/mtg/slurm-logs/slurm-%x-%j.out
#
# Controls (a) and (b) for the bound-choice decision schema, ONE RUN'S FRAMES PER JOB.
#
# CHUNKED BY RUN because this is `cenia/regular`, which is preemptible with a one-hour
# exemption: a job that fits inside the hour cannot be preempted before it finishes, and if
# one is killed anyway it costs one chunk rather than all three. Run B lost an hour of work to
# exactly this (job 7927, preempted 17 seconds after its exemption lapsed), so `--no-requeue`
# and `--open-mode=append` are passed on the command line and a chunk is sized to finish inside
# the window rather than to use it.
#
#   sbatch --partition=cenia --account=default-account --qos=regular \
#          --no-requeue --open-mode=append \
#          pipelines/decision_schema_controls.sbatch <frames.jsonl> <label>
set -uo pipefail

MTG=/workspace1/users/ouhenio/mtg
WT=/workspace1/users/ouhenio/mb-wt-schema
SERVE_VENV=/workspace1/users/ouhenio/vllm-serve2/.venv
MODEL=$(ls -d /workspace1/projects/posttrainlatamgpt/hub/models--Qwen--Qwen3.5-4B/snapshots/* 2>/dev/null | head -1)
PORT=8188

FRAMES="${1:?usage: $0 <frames.jsonl> <label>}"
LABEL="${2:?usage: $0 <frames.jsonl> <label>}"
OUT=/workspace1/users/ouhenio/mtg-evidence-overflow/schema-controls/$LABEL

echo "frames: $FRAMES"
echo "out:    $OUT"
echo "model:  $MODEL"

# --- the output path must exist AND have room, asserted AT THE OUTPUT PATH -------------------
# /workspace1 is xfs with PROJECT QUOTAS. `df /workspace1` reports 34 TB free while
# `df /workspace1/users/ouhenio` reports its own 200 GB allocation -- so a free-space check run
# against the filesystem passes while the actual destination is full. The data root
# (/workspace1/projects/posttrainlatamgpt) hit 100% with 4 KB free on 2026-09-19 and the first
# symptom was a 2 MB write failing with ENOSPC. A job that loads a 4B and then dies at its first
# write is the shape this exists to prevent.
mkdir -p "$OUT" || { echo "FATAL: cannot create $OUT"; exit 1; }
AVAIL_KB=$(df --output=avail -k "$OUT" | tail -1 | tr -d ' ')
if [ "${AVAIL_KB:-0}" -lt 2097152 ]; then
  echo "FATAL: $OUT has $((AVAIL_KB/1024)) MB free; need >= 2048 MB."
  echo "  This is the DESTINATION's quota, not the filesystem's -- /workspace1 may well report"
  echo "  terabytes. Free space here or point --out somewhere with room."
  exit 1
fi
echo "free at output: $((AVAIL_KB/1024/1024)) GB"

[ -f "$FRAMES" ] || { echo "FATAL: no frames at $FRAMES"; exit 1; }
[ -n "$MODEL" ]  || { echo "FATAL: no 4B snapshot found"; exit 1; }

# --- provenance: which bytes are about to run ------------------------------------------------
for f in src/magebench/pilot/decision_schema.py pipelines/decision_schema_controls.py; do
  disk=$(git -C "$WT" hash-object "$WT/$f"); head=$(git -C "$WT" rev-parse "HEAD:$f" 2>/dev/null || echo "?")
  printf "blob %-46s %s %s\n" "$f" "${disk:0:12}" "$([ "$disk" = "$head" ] && echo MATCHES-HEAD || echo DIFFERS-FROM-HEAD)"
done
echo "branch $(git -C "$WT" rev-parse --abbrev-ref HEAD)  commit $(git -C "$WT" rev-parse --short HEAD)"

# --- GATE: the grammar must still reject an out-of-enum id, BEFORE a card is spent -----------
# A guard that does not reject is worse than no guard, because the arm is labelled guarded.
"$SERVE_VENV/bin/python" "$WT/pipelines/prove_decision_grammar.py" \
    --model "$MODEL" --json "$OUT/grammar_proof.json"
GATE=$?
if [ $GATE -ne 0 ]; then
  echo "FATAL: the compiled-grammar proof failed (rc=$GATE). Refusing to serve."
  exit 1
fi

# --- serve ------------------------------------------------------------------------------------
export OPD_LOG_DIR=$OUT/serve; mkdir -p "$OPD_LOG_DIR"
# serve_student.sh's signature is <gpu|slurm> <port> [model_dir] [served_name], and it
# HARDCODES the 4B's served name -- that is why it is a separate file from serve_teacher.sh.
# Passing a served name here would either be redundant or silently override the one the
# presets resolve against, so it is omitted deliberately rather than by oversight.
bash "$MTG/pipelines/opd/serve_student.sh" slurm "$PORT" "$MODEL" \
  || { echo "FATAL: serve failed"; exit 1; }

# WAIT ON THE ARTIFACT -- the server answering /v1/models -- not on a sleep. A fixed sleep
# reports on the clock rather than on the server.
for _ in $(seq 1 90); do
  curl -s -m 5 "http://127.0.0.1:$PORT/v1/models" >/dev/null && break
  sleep 10
done
curl -s -m 5 "http://127.0.0.1:$PORT/v1/models" >/dev/null || { echo "FATAL: server never answered"; exit 1; }

# --- the controls -------------------------------------------------------------------------------
# CONC 8: the draws are independent (seeds are per (frame, draw), not per order) and vLLM
# batches, so this is prefill-bound at ~30k-token prompts. The runner prints its MEASURED rate
# at 100/250/500 generations so the next chunk's --time comes from a number rather than a guess.
"$WT/.venv/bin/python" "$WT/pipelines/decision_schema_controls.py" \
    --frames "$FRAMES" --out "$OUT" --base-url "http://127.0.0.1:$PORT/v1" \
    --draws 8 --conc 8
RC=$?
echo "controls returned $RC -- the WRAPPER's code is not the verdict; read $OUT/verdict.json"

PGID=$(cat "$OPD_LOG_DIR/serve_$PORT.pgid" 2>/dev/null)
[ -n "$PGID" ] && kill -- -"$PGID" 2>/dev/null
exit $RC
