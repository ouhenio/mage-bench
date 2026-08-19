package mage.player.ai;

import mage.abilities.Ability;
import mage.abilities.SpellAbility;
import mage.abilities.common.PassAbility;
import mage.constants.RangeOfInfluence;
import mage.game.Game;
import mage.game.combat.CombatGroup;
import mage.util.ThreadUtils;
import mage.game.permanent.Permanent;
import mage.players.PlayerImpl;
import mage.util.ShortIdRegistry;
import org.apache.log4j.Logger;

import java.io.BufferedWriter;
import java.io.IOException;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Publishes what the engine AI *would* do at a seat that is actually played by
 * someone else, so an LLM seat's decisions can be labelled by a stronger player.
 * <p>
 * Off unless -Dxmage.hint.seats is set, so normal play is unaffected.
 * <p>
 * The hint is computed by a DETACHED {@link ComputerPlayer7} that is never seated in
 * the game. That works because {@code calculateActions} only ever reads the passed
 * game: it calls {@code createSimulation(game)}, which copies via
 * {@code game.createSimulationForAI()} and rebuilds every player from the real game's
 * own player map, using this object's {@code playerId} solely to mark which seat is
 * "me". Nothing here mutates the live game, and the hint player is never visible to it.
 * <p>
 * Two consequences worth stating because they bound what the labels mean:
 * <ul>
 * <li>The search is full-information -- {@code SimulatedPlayer2} evaluates with every
 * hand visible. Some hints are therefore justified by information that is absent from
 * the prompt the LLM was shown. That caps the achievable policy and cannot be filtered
 * away.</li>
 * <li>On timeout this emits an explicit error record and NEVER a pass. A fabricated
 * "the teacher passed" is the exact defect that made an existing recorder label 1073 of
 * 1295 attack decisions as Pass, and a pass is the single most common action, so a
 * fabricated one is invisible in aggregate.</li>
 * </ul>
 */
public final class AiHintProvider {

    private static final Logger logger = Logger.getLogger(AiHintProvider.class);

    private static final String SEATS_PROPERTY = "xmage.hint.seats";
    private static final String DIR_PROPERTY = "xmage.hint.dir";
    private static final String SKILL_PROPERTY = "xmage.hint.skill";
    /** Hard ceiling per hint. The AI's own budget is skill*3s; this bounds the tail. */
    private static final String TIMEOUT_PROPERTY = "xmage.hint.timeoutSecs";

    private static final AtomicInteger SEQ = new AtomicInteger();

    /**
     * Aliases already written to a game's side file, so each is written once.
     * <p>
     * Keyed by game id and never evicted. Bounded in practice because a hinted JVM hosts
     * exactly one game -- the same property that currently makes the global {@link #SEQ}
     * above harmless. If that ever stops being true, this map and SEQ both need a
     * per-game home, and they should move together.
     */
    private static final Map<UUID, Set<String>> EMITTED_ALIASES = new ConcurrentHashMap<>();

    /**
     * Single-threaded and daemon: hints run one at a time so they cannot contend with
     * the AI's own simulation pool (threadPoolSimulations is a static 5, shared), and
     * daemon so a hung search can never hold the server open.
     */
    private static final ExecutorService POOL = Executors.newSingleThreadExecutor(r -> {
        // NAME MATTERS: ThreadUtils.isRunGameThread() whitelists by thread-name PREFIX, and
        // GameImpl.checkConcede -> ensureRunInGameThread throws for anything else. A bare
        // "ai-hint" is not whitelisted, so any hint path that reaches checkStateAndTriggered
        // died with "game related code must run in GAME thread" -- which is what made
        // declare_blockers fail on 9 of 9 attempts while attackers passed 23 of 23, since
        // only the block path calls CombatUtil.willItSurviveSimple.
        //
        // THREAD_PREFIX_AI_SIMULATION_MAD is the sanctioned prefix for exactly this: AI
        // simulation running game-related code off the game thread. ComputerPlayer6:65 --
        // the class HintPlayer extends -- names its own simulation pool with it. This is
        // the engine's own mechanism, not a bypass of its guard.
        Thread t = new Thread(r, ThreadUtils.THREAD_PREFIX_AI_SIMULATION_MAD + "-hint");
        t.setDaemon(true);
        return t;
    });

    private AiHintProvider() {
    }

    public static boolean isEnabled() {
        String seats = System.getProperty(SEATS_PROPERTY);
        return seats != null && !seats.isEmpty();
    }

    /** Hints are per seat name, so only the LLM seat pays the cost. */
    public static boolean isHintedSeat(String playerName) {
        if (!isEnabled() || playerName == null) {
            return false;
        }
        for (String s : System.getProperty(SEATS_PROPERTY).split(",")) {
            if (playerName.equals(s.trim())) {
                return true;
            }
        }
        return false;
    }

    private static int intProperty(String key, int fallback) {
        try {
            String v = System.getProperty(key);
            return v == null ? fallback : Integer.parseInt(v.trim());
        } catch (NumberFormatException e) {
            return fallback;
        }
    }

    /**
     * Entry point for the hook sites. Never throws: a hint is diagnostic data, and
     * taking a live game down to produce it would be a poor trade.
     *
     * @param kind "priority", "declare_attackers" or "declare_blockers" -- which
     *             prompt this hint is meant to label.
     */
    public static void hint(Game game, UUID playerId, String playerName, String kind, String site) {
        if (!isHintedSeat(playerName)) {
            return;
        }
        int seq = SEQ.getAndIncrement();
        // Read on the GAME thread, before POOL.submit, and with the NON-MUTATING peek.
        // nextGameSeq() would consume the number the decision event is about to take
        // (GameController.java:194), corrupting the very log this stamp exists to join.
        int gameSeq = game.getGameSeq();
        long started = System.currentTimeMillis();
        emitAliasDelta(game, gameSeq);
        try {
            emit(game, "hints-", compute(game, playerId, playerName, kind, site, seq, started, gameSeq));
        } catch (Throwable t) {
            // Includes the timeout path. Record the failure rather than dropping it:
            // a silent gap in the hint stream is indistinguishable from a decision the
            // hook never fired on, and the consumer must be able to drop those games.
            logger.warn("hint failed for " + playerName + " (" + kind + "): " + t, t);
            emit(game, "hints-", errorRecord(playerName, kind, site, seq, started, gameSeq, t));
        }
    }

    /**
     * Append the aliases minted since the last hint to {@code aliases-<gameId>.jsonl}.
     * <p>
     * DELTAS, to a SIDE FILE, and both halves of that were measured. The alternative --
     * stamping the whole map on every hint row -- costs 52 entries x 45 B = 2,352 B per
     * row against 823,814 recorded rows: +1.94 GB, 11x the entire 193.7 MB hint corpus,
     * which is what constraint "no per-row bloat" forbids. Deltas cost ~3.2 KB per game,
     * +13.5 MB corpus-wide (+7.0%).
     * <p>
     * A side file rather than the hint row because the map is per GAME, not per decision,
     * and rather than a single dump at game end because 423 of 4,215 recorded games
     * (10.0%) have a truncated event log: a game-end-only dump loses the whole map for
     * one game in ten, while deltas are already on disk when the JVM dies.
     * <p>
     * The delta written at decision N cannot contain the aliases minted FOR decision N --
     * GameView.assignShortIds runs after this. That is by design and is why the reader
     * must treat the file as a game-scoped map, not a per-decision one: register() has no
     * callers, so an alias is permanent and an alias first seen at decision N+1 was
     * already the right answer at N.
     */
    private static void emitAliasDelta(Game game, int gameSeq) {
        try {
            Set<String> seen = EMITTED_ALIASES.computeIfAbsent(game.getId(), k -> ConcurrentHashMap.newKeySet());
            List<String> fresh = new ArrayList<>();
            StringBuilder sb = new StringBuilder(128);
            for (Map.Entry<String, UUID> e : game.getShortIdRegistry().snapshotAssignments().entrySet()) {
                if (seen.contains(e.getKey())) {
                    continue;
                }
                fresh.add(e.getKey());
                if (sb.length() > 0) {
                    sb.append(',');
                }
                quoted(sb, e.getKey(), String.valueOf(e.getValue()));
            }
            if (fresh.isEmpty()) {
                return;
            }
            // Mark as emitted only AFTER the write lands. Marking first is the obvious
            // one-pass version and it is wrong: emit() swallows an IOException by design
            // (a hint must not take a game down), so a transient failure would silently
            // retire those aliases forever and leave a permanent hole in the map for
            // exactly the objects the failed decision was about. Caught by a probe that
            // called this before xmage.hint.dir was set: alias p1 was marked emitted and
            // never appeared in the file.
            if (emit(game, "aliases-", "{\"game_seq\":" + gameSeq + ",\"new\":{" + sb + "}}")) {
                seen.addAll(fresh);
            }
        } catch (Throwable t) {
            // Same contract as hint() itself, and stated there: a hint is diagnostic data
            // and taking a live game down to produce it would be a poor trade. Logged, not
            // swallowed silently -- and deliberately NOT folded into hint()'s own catch,
            // because a failure to write aliases must not also discard the hint.
            logger.warn("could not write alias delta: " + t, t);
        }
    }

    private static String compute(Game game, UUID playerId, String playerName, String kind, String site,
                                  int seq, long started, int gameSeq) throws Exception {
        int skill = intProperty(SKILL_PROPERTY, 1);
        int timeout = intProperty(TIMEOUT_PROPERTY, Math.max(10, skill * 3 + 5));

        Future<HintResult> f = POOL.submit(() -> {
            HintPlayer hp = new HintPlayer("ai-hint", RangeOfInfluence.ALL, skill, playerId);
            return hp.computeHint(game, kind);
        });
        HintResult r;
        try {
            r = f.get(timeout, TimeUnit.SECONDS);
        } catch (Exception e) {
            f.cancel(true);
            throw e;
        }

        StringBuilder sb = new StringBuilder(256);
        sb.append('{');
        field(sb, "seq", seq).append(',');
        quoted(sb, "seat", playerName).append(',');
        quoted(sb, "kind", kind).append(',');
        quoted(sb, "site", site).append(',');
        field(sb, "turn", game.getTurnNum()).append(',');
        quoted(sb, "phase", String.valueOf(game.getPhase() == null ? "" : game.getPhase().getType())).append(',');
        quoted(sb, "step", String.valueOf(game.getStep() == null ? "" : game.getStep().getType())).append(',');
        field(sb, "skill", skill).append(',');
        field(sb, "elapsed_ms", System.currentTimeMillis() - started).append(',');
        sb.append("\"abilities\":").append(jsonArray(r.abilityRules)).append(',');
        sb.append("\"source_ids\":").append(jsonArray(r.sourceIds)).append(',');
        sb.append("\"attackers\":").append(jsonArray(r.attackers)).append(',');
        sb.append("\"blockers\":").append(jsonArray(r.blockers)).append(',');
        // An empty ability list is a real, meaningful answer: the AI would pass. It is
        // reported as such and is distinguishable from the error record above.
        sb.append("\"pass\":").append(r.abilityRules.isEmpty()).append(',');
        // APPENDED, never inserted: every field above keeps its byte position, so the
        // existing prefix of every record is unchanged and readers that pin key order or
        // slice the row are unaffected.
        //
        // game_seq is an OBSERVATION -- the seq the game had when this hint fired.
        // decision_seq is a CLAIM about which decision row this hint labels, and it is
        // emitted ONLY at the publish site, where it is provably exact: between
        // aiHint(...,"publish") and the decision's own stamp there is exactly one
        // nextGameSeq() call, at GameController.java:194, on this same thread. Measured
        // over 363 complete games, publish hints and SELECT prompts match 363/363 (100%).
        //
        // The entry sites get no decision_seq. They fire once per METHOD CALL while the
        // loop below them publishes 0..N queries: declare_attackers matched in 131/363
        // games (574 hints -> 1130 prompts), declare_blockers in 25/363 (1257 -> 306,
        // because the possibleBlockersCount==0 early return sits after the hint). A
        // blocker hint that returns without prompting would take a seq belonging to some
        // later, unrelated decision -- an off-by-one join is worse than no join.
        field(sb, "game_seq", gameSeq);
        if ("publish".equals(site)) {
            sb.append(',');
            field(sb, "decision_seq", gameSeq + 1);
        }
        sb.append(',').append("\"source_aliases\":").append(jsonArrayOrNull(r.sourceAliases));
        sb.append('}');
        return sb.toString();
    }

    /**
     * A non-empty label for an ability, so an action can never be mistaken for a pass.
     *
     * getRule() is the EFFECTS text and is empty for a vanilla creature spell.
     * SpellAbility.getRule(true) appends the card name, which is what makes a cast
     * expressible at all; for everything else the effects text is already the useful
     * label. The final fallback is the class name -- honest about being unresolved,
     * which an empty string is not.
     */
    private static String describeAbility(Ability a) {
        String rule = a.getRule();
        if (rule != null && !rule.isEmpty()) {
            return rule;
        }
        if (a instanceof SpellAbility) {
            String full = ((SpellAbility) a).getRule(true);
            if (full != null && !full.isEmpty()) {
                return full;
            }
        }
        return "unresolved:" + a.getClass().getSimpleName();
    }

    private static String errorRecord(String playerName, String kind, String site, int seq, long started,
                                      int gameSeq, Throwable t) {
        StringBuilder sb = new StringBuilder(128);
        sb.append('{');
        field(sb, "seq", seq).append(',');
        quoted(sb, "seat", playerName).append(',');
        quoted(sb, "kind", kind).append(',');
        quoted(sb, "site", site).append(',');
        field(sb, "elapsed_ms", System.currentTimeMillis() - started).append(',');
        quoted(sb, "error", t.getClass().getSimpleName() + ": " + String.valueOf(t.getMessage())).append(',');
        // A timed-out hint must stay joinable. Without the stamp the consumer can only
        // drop the whole GAME; with it, it drops exactly the one decision it has no
        // label for.
        field(sb, "game_seq", gameSeq);
        if ("publish".equals(site)) {
            sb.append(',');
            field(sb, "decision_seq", gameSeq + 1);
        }
        sb.append('}');
        return sb.toString();
    }

    private static StringBuilder field(StringBuilder sb, String k, long v) {
        return sb.append('"').append(k).append("\":").append(v);
    }

    private static StringBuilder quoted(StringBuilder sb, String k, String v) {
        return sb.append('"').append(k).append("\":\"").append(escape(v)).append('"');
    }

    private static String jsonArray(List<String> items) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append('"').append(escape(items.get(i))).append('"');
        }
        return sb.append(']').toString();
    }

    /**
     * Like {@link #jsonArray}, but a null element renders as JSON {@code null} rather
     * than the string "null". The distinction is load-bearing: a null here means "this
     * object had no alias yet when the hint fired", which the consumer resolves from the
     * alias side file, and it must not be confused with an object whose alias is absent
     * for any other reason.
     */
    private static String jsonArrayOrNull(List<String> items) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            String v = items.get(i);
            if (v == null) {
                sb.append("null");
            } else {
                sb.append('"').append(escape(v)).append('"');
            }
        }
        return sb.append(']').toString();
    }

    private static String escape(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.toString();
    }

    // One file per game. A single shared hints.jsonl is overwritten by the next run,
    // which is why "was the old hook closer?" could not be answered without re-running.
    /** @return true when the record was durably recorded (written, or logged as fallback). */
    private static synchronized boolean emit(Game game, String basename, String json) {
        String dir = System.getProperty(DIR_PROPERTY);
        if (dir == null || dir.isEmpty()) {
            logger.info("AI_HINT " + basename.replace("-", "") + " " + json);
            return true;
        }
        try {
            Path out = Paths.get(dir);
            Files.createDirectories(out);
            try (BufferedWriter w = Files.newBufferedWriter(out.resolve(basename + game.getId() + ".jsonl"),
                    StandardCharsets.UTF_8, StandardOpenOption.CREATE, StandardOpenOption.APPEND)) {
                w.write(json);
                w.newLine();
            }
            return true;
        } catch (IOException e) {
            logger.warn("could not write hint: " + e, e);
            return false;
        }
    }

    /** What the AI would do, flattened to strings the Python side can join on. */
    private static final class HintResult {
        final List<String> abilityRules = new ArrayList<>();
        final List<String> sourceIds = new ArrayList<>();
        /** Parallel to sourceIds: the alias for the same object, or null if unassigned. */
        final List<String> sourceAliases = new ArrayList<>();
        final List<String> attackers = new ArrayList<>();
        final List<String> blockers = new ArrayList<>();
    }

    /**
     * A ComputerPlayer7 that is bound to another seat's id and never enters the game.
     * <p>
     * Subclassing is what makes this possible at all: {@code playerId} is protected on
     * PlayerImpl, so only a subclass can rebind it, and only a class in this package
     * can reach {@code calculateActions}, {@code actions} and {@code combat}.
     */
    private static final class HintPlayer extends ComputerPlayer7 {

        HintPlayer(String name, RangeOfInfluence range, int skill, UUID seatId) throws Exception {
            super(name, range, skill);
            // Rebind to the seat being hinted. createSimulation() uses playerId only to
            // mark which simulated player is "me"; the players themselves are rebuilt
            // from the real game's map, so this object is never part of any game.
            //
            // playerId is `protected final` on PlayerImpl and every ComputerPlayer
            // constructor chains to PlayerImpl(String, RangeOfInfluence), which mints a
            // fresh id. The engine's own way to bind an existing id is a constructor --
            // SimulatedPlayer2 does `super(originalPlayer.getId())` -- but that path is
            // not exposed on ComputerPlayer6/7. Reflection here keeps the change to a
            // single new file instead of adding constructors to two engine classes on a
            // hot path. No graceful fallback: if this ever stops working the hint would
            // silently describe the wrong seat, which is worse than not running.
            Field f = PlayerImpl.class.getDeclaredField("playerId");
            f.setAccessible(true);
            f.set(this, seatId);
            if (!seatId.equals(this.getId())) {
                throw new IllegalStateException("could not rebind hint player to seat " + seatId);
            }
        }

        @Override
        public HintPlayer copy() {
            throw new UnsupportedOperationException("hint player must never be copied into a game");
        }

        HintResult computeHint(Game game, String kind) {
            // The LIVE registry. calculateActions() copies the game internally, but UUIDs
            // survive the copy and the aliases the prompt shows are the live game's.
            ShortIdRegistry registry = game.getShortIdRegistry();
            // Fresh state per call. getNextAction() resumes a previous search from
            // `root`, and actionCache suppresses repeated zero-cost actions across
            // calls -- both are right for a seat that acts and wrong for one that only
            // ever answers "what would you do here".
            this.actions.clear();
            this.actionCache.clear();
            this.root = null;
            this.combat = null;

            // COMBAT IS NOT AN ABILITY SEARCH, and calculateActions cannot answer it.
            //
            // calculateActions computes a sequence of ABILITIES to play. A combat
            // declaration is not an ability, so at a declare_attackers hook it returns
            // nothing: measured over six games, all 15 declare_attackers hints carried
            // an empty abilities list AND an empty attacker list, and reported pass:true
            // while the seat had creatures on the battlefield. `combat` is only ever
            // assigned from `root.combat` (ComputerPlayer7:139, ComputerPlayer6:332),
            // and a fresh search that never reaches a combat node leaves it null.
            // The engine's own answer to "who attacks" is selectAttackers.
            //
            // ON A SIMULATION COPY, NEVER THE PASSED GAME. declareAttackers fires
            // DECLARE_ATTACKERS_STEP_PRE and declares into game.getCombat(), so calling
            // it on the live object would make the hint MUTATE THE GAME IT ADVISES --
            // the attack would appear as the seat's own decision and be invisible in the
            // artefact, which is how the previous two defects in this file hid. The
            // whole provider rests on calculateActions being read-only; this is the one
            // path that would break that, so it gets its own copy.
            //
            // UUIDs survive createSimulationForAI, which is why the ids read off the
            // copy still resolve against the live game in nameAndId and peekShortId.
            if ("declare_attackers".equals(kind) || "declare_blockers".equals(kind)) {
                Game sim = game.createSimulationForAI();
                if ("declare_attackers".equals(kind)) {
                    selectAttackers(sim, sim.getActivePlayerId());
                } else {
                    // Reachable only because the pool thread now carries the AI-SIM-MAD
                    // prefix: declareBlockers -> CombatUtil.blockWithGoodTrade2 ->
                    // getBlockersThatWillSurvive2 -> willItSurviveSimple ->
                    // checkStateAndTriggered -> checkConcede -> ensureRunInGameThread.
                    selectBlockers(null, sim, this.playerId);
                }
                this.combat = sim.getCombat();
            } else {
                calculateActions(game);
            }

            HintResult r = new HintResult();
            for (Ability a : this.actions) {
                // DROP ONLY A REAL PASS. This condition used to also drop any ability
                // whose rule text was empty, which is far broader than the intent and
                // silently deleted the primary action of every aggro deck.
                //
                // AbilityImpl.getRule() returns the EFFECTS text. A vanilla creature
                // spell has none, so `getRule().isEmpty()` was true for it, the cast was
                // skipped, and `pass = abilityRules.isEmpty()` then reported that the
                // engine PASSED on a decision where it had chosen to deploy a creature.
                // Measured by karn-research over 6,337 non-pass hint rows across 150
                // files and every deck: hints starting with "Cast" = 0, hints naming any
                // Boros creature = 0. And the control that settles it -- same 84 games,
                // pilot advised at skill 8 fielded a creature in 7 (8%), the opposing
                // real ComputerPlayer at skill 1 in 63 (75%). The engine is not passive;
                // this channel could not say "cast that".
                //
                // AiDecisionRecorder.describe() already guards this exact case ("Never
                // emit an empty label. As a training target, '' is indistinguishable from
                // a genuine pass"). The same defect was fixed in one recorder and left in
                // the other.
                if (a instanceof PassAbility) {
                    continue;
                }
                r.abilityRules.add(describeAbility(a));
                UUID sourceId = a.getSourceId();
                r.sourceIds.add(String.valueOf(sourceId));
                // THIS is what makes a hint joinable to a prompt option by object
                // identity. `abilities` is rules text ("{T}, Sacrifice {this}: ...") and
                // the prompt lists names with aliases ("Sacred Foundry [id=p6, land]"):
                // two namespaces, and matching them by text resolved only 31.9% of hints,
                // which is why a text-keyed generator acted on 3.79% of decisions where
                // the engine acted on 11.74% (11.74% x 31.9% = 3.745%, the mechanism).
                // source_ids already carried the real UUID on 100.0% of publish-site
                // action rows; this is the other half of the join, resolved in the hint
                // itself. Costs 28 B on the 6.8% of rows that are action rows: +1.6 MB
                // corpus-wide (+0.8%).
                //
                // peekShortId, never getOrAssign: minting here would advance nextId and
                // renumber every alias the renderer assigns afterwards, i.e. change the
                // text of every later prompt.
                r.sourceAliases.add(sourceId == null ? null : registry.peekShortId(sourceId));
            }
            if (this.combat != null) {
                for (UUID id : this.combat.getAttackers()) {
                    r.attackers.add(nameAndId(game, id));
                }
                for (CombatGroup g : this.combat.getGroups()) {
                    for (UUID blockerId : g.getBlockers()) {
                        // blocker -> the attacker it is assigned to, matching the
                        // `blockers=p5:p1` grammar the LLM is shown.
                        for (UUID attackerId : g.getAttackers()) {
                            r.blockers.add(nameAndId(game, blockerId) + ">" + nameAndId(game, attackerId));
                        }
                    }
                }
            }
            return r;
        }

        private static String nameAndId(Game game, UUID id) {
            Permanent p = game.getPermanent(id);
            return (p == null ? "?" : p.getName()) + "#" + id;
        }
    }
}
