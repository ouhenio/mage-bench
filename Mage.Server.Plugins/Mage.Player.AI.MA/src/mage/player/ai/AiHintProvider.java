package mage.player.ai;

import mage.abilities.Ability;
import mage.abilities.common.PassAbility;
import mage.constants.RangeOfInfluence;
import mage.game.Game;
import mage.game.combat.CombatGroup;
import mage.game.permanent.Permanent;
import mage.players.PlayerImpl;
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
import java.util.UUID;
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
     * Single-threaded and daemon: hints run one at a time so they cannot contend with
     * the AI's own simulation pool (threadPoolSimulations is a static 5, shared), and
     * daemon so a hung search can never hold the server open.
     */
    private static final ExecutorService POOL = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "ai-hint");
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
        long started = System.currentTimeMillis();
        try {
            emit(game, compute(game, playerId, playerName, kind, site, seq, started));
        } catch (Throwable t) {
            // Includes the timeout path. Record the failure rather than dropping it:
            // a silent gap in the hint stream is indistinguishable from a decision the
            // hook never fired on, and the consumer must be able to drop those games.
            logger.warn("hint failed for " + playerName + " (" + kind + "): " + t, t);
            emit(game, errorRecord(playerName, kind, site, seq, started, t));
        }
    }

    private static String compute(Game game, UUID playerId, String playerName, String kind, String site,
                                  int seq, long started) throws Exception {
        int skill = intProperty(SKILL_PROPERTY, 1);
        int timeout = intProperty(TIMEOUT_PROPERTY, Math.max(10, skill * 3 + 5));

        Future<HintResult> f = POOL.submit(() -> {
            HintPlayer hp = new HintPlayer("ai-hint", RangeOfInfluence.ALL, skill, playerId);
            return hp.computeHint(game);
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
        sb.append("\"pass\":").append(r.abilityRules.isEmpty());
        sb.append('}');
        return sb.toString();
    }

    private static String errorRecord(String playerName, String kind, String site, int seq, long started, Throwable t) {
        StringBuilder sb = new StringBuilder(128);
        sb.append('{');
        field(sb, "seq", seq).append(',');
        quoted(sb, "seat", playerName).append(',');
        quoted(sb, "kind", kind).append(',');
        quoted(sb, "site", site).append(',');
        field(sb, "elapsed_ms", System.currentTimeMillis() - started).append(',');
        quoted(sb, "error", t.getClass().getSimpleName() + ": " + String.valueOf(t.getMessage()));
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
    private static synchronized void emit(Game game, String json) {
        String dir = System.getProperty(DIR_PROPERTY);
        if (dir == null || dir.isEmpty()) {
            logger.info("AI_HINT " + json);
            return;
        }
        try {
            Path out = Paths.get(dir);
            Files.createDirectories(out);
            try (BufferedWriter w = Files.newBufferedWriter(out.resolve("hints-" + game.getId() + ".jsonl"),
                    StandardCharsets.UTF_8, StandardOpenOption.CREATE, StandardOpenOption.APPEND)) {
                w.write(json);
                w.newLine();
            }
        } catch (IOException e) {
            logger.warn("could not write hint: " + e, e);
        }
    }

    /** What the AI would do, flattened to strings the Python side can join on. */
    private static final class HintResult {
        final List<String> abilityRules = new ArrayList<>();
        final List<String> sourceIds = new ArrayList<>();
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

        HintResult computeHint(Game game) {
            // Fresh state per call. getNextAction() resumes a previous search from
            // `root`, and actionCache suppresses repeated zero-cost actions across
            // calls -- both are right for a seat that acts and wrong for one that only
            // ever answers "what would you do here".
            this.actions.clear();
            this.actionCache.clear();
            this.root = null;
            this.combat = null;

            calculateActions(game);

            HintResult r = new HintResult();
            for (Ability a : this.actions) {
                // A planned PassAbility renders as an empty rule with a null source.
                // Counting it as an action makes every pass look like a real play, so
                // it is dropped here and reported through the `pass` flag instead.
                if (a instanceof PassAbility || a.getRule() == null || a.getRule().isEmpty()) {
                    continue;
                }
                r.abilityRules.add(a.getRule());
                r.sourceIds.add(String.valueOf(a.getSourceId()));
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
