package mage.player.ai;

import mage.constants.PhaseStep;
import mage.game.Game;
import org.apache.log4j.Logger;

import java.io.FileWriter;
import java.io.IOException;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/**
 * Measurement hook: at a probed seat's priority, run {@link RolloutCounter} at several N, several
 * independent repeats each, and append one JSON line per call. It exists to measure the reward's
 * SD against N (position-environment.md section 4), the no-result fraction beside it, and the
 * wall-clock per position, which is the N x B cost model's first real data point.
 * <p>
 * Off unless -Dxmage.rollout.seats is set. Reached reflectively from HumanPlayer.priority, like
 * AiHintProvider, on the GAME thread at priority entry: the live game is quiescent there, so the
 * copy reads a consistent state, and nothing crosses threads until the copy exists.
 * <p>
 * POSITIONS: the first time in each turn that the seat receives priority in PRECOMBAT_MAIN with an
 * empty stack, up to xmage.rollout.positions per game. One per turn, so the positions spread over
 * the game rather than bunching in the opening.
 * <p>
 * MODES (xmage.rollout.mode, required): "count" is the above. "spread" runs {@link ActionSpread}
 * instead -- every legal action applied, N rollouts from each result -- at the first priority the
 * seat receives in each (turn, step), in ANY step, counting only positions with more than one
 * legal action, up to xmage.rollout.positions per seat per game. Spread takes exactly one N.
 * <p>
 * Every setting is REQUIRED once seats is set -- a measurement whose N or budget came from a
 * default is not a registered measurement. The game seed is required too: without it the
 * rollouts are not reproducible, which is the property being measured.
 */
public final class RolloutProbe {

    private static final Logger logger = Logger.getLogger(RolloutProbe.class);

    // per game: probed turns and positions so far. Games in one JVM run sequentially here.
    private static final Map<UUID, Integer> positionsByGame = new HashMap<>();
    private static final Map<UUID, Integer> lastTurnByGame = new HashMap<>();

    private RolloutProbe() {
    }

    private static String required(String key) {
        String v = System.getProperty(key);
        if (v == null || v.trim().isEmpty()) {
            throw new IllegalStateException("xmage.rollout.seats is set but " + key + " is not");
        }
        return v.trim();
    }

    public static boolean isProbedSeat(String playerName) {
        String seats = System.getProperty("xmage.rollout.seats");
        if (seats == null) {
            return false;
        }
        for (String s : seats.split(",")) {
            if (s.trim().equals(playerName)) {
                return true;
            }
        }
        return false;
    }

    // disjoint seed families per (game, position, N, repeat)
    static long seedBase(long gameSeed, int position, int n, int repeat) {
        return RolloutCounter.mix(RolloutCounter.mix(RolloutCounter.mix(RolloutCounter.mix(gameSeed) + position) + n) + repeat);
    }

    public static synchronized void probe(Game game, UUID playerId, String playerName) {
        if (!isProbedSeat(playerName)) {
            return;
        }
        String mode = required("xmage.rollout.mode");
        if (mode.equals("spread")) {
            spread(game, playerId, playerName);
            return;
        }
        if (!mode.equals("count")) {
            throw new IllegalStateException("xmage.rollout.mode must be count or spread, got " + mode);
        }
        if (game.getTurnStepType() != PhaseStep.PRECOMBAT_MAIN || !game.getStack().isEmpty()) {
            return;
        }
        UUID gameId = game.getId();
        int turn = game.getTurnNum();
        Integer last = lastTurnByGame.get(gameId);
        if (last != null && last == turn) {
            return;
        }
        int maxPositions = Integer.parseInt(required("xmage.rollout.positions"));
        int done = positionsByGame.containsKey(gameId) ? positionsByGame.get(gameId) : 0;
        if (done >= maxPositions) {
            return;
        }
        String out = required("xmage.rollout.out");
        long budgetMs = Long.parseLong(required("xmage.rollout.budgetMs"));
        int repeats = Integer.parseInt(required("xmage.rollout.repeats"));
        int threads = Integer.parseInt(required("xmage.rollout.threads"));
        String[] ns = required("xmage.rollout.ns").split(",");
        Long gameSeed = game.getOptions().gameSeed;
        if (gameSeed == null) {
            String prop = System.getProperty("xmage.game.seed");
            if (prop == null || prop.trim().isEmpty()) {
                throw new IllegalStateException("rollout probe needs a seeded game: no per-game seed and no xmage.game.seed");
            }
            gameSeed = Long.parseLong(prop.trim());
        }
        lastTurnByGame.put(gameId, turn);
        positionsByGame.put(gameId, done + 1);
        int position = done;

        long t0 = System.nanoTime();
        for (String nText : ns) {
            int n = Integer.parseInt(nText.trim());
            for (int repeat = 0; repeat < repeats; repeat++) {
                long base = seedBase(gameSeed, position, n, repeat);
                RolloutCounter.Result r = RolloutCounter.count(game, playerId, base, n, budgetMs, threads,
                        RolloutCounter.Critic.parse(required("xmage.rollout.critic")));
                write(out, json(game, playerName, gameSeed, position, repeat, r));
            }
        }
        logger.info("rollout probe: game " + gameId + " position " + position + " (turn " + turn + ") done in "
                + (System.nanoTime() - t0) / 1_000_000L + " ms");
    }

    // spread mode: per (game, seat) positions banked, and the (turn, step)s already looked at
    private static final Map<String, Integer> spreadPositions = new HashMap<>();
    private static final Set<String> spreadSeen = new HashSet<>();

    private static long gameSeed(Game game) {
        Long gameSeed = game.getOptions().gameSeed;
        if (gameSeed != null) {
            return gameSeed;
        }
        String prop = System.getProperty("xmage.game.seed");
        if (prop == null || prop.trim().isEmpty()) {
            throw new IllegalStateException("rollout probe needs a seeded game: no per-game seed and no xmage.game.seed");
        }
        return Long.parseLong(prop.trim());
    }

    private static void spread(Game game, UUID playerId, String seat) {
        String key = game.getId() + "|" + seat;
        if (!spreadSeen.add(key + "|" + game.getTurnNum() + "|" + game.getTurnStepType())) {
            return;
        }
        int maxPositions = Integer.parseInt(required("xmage.rollout.positions"));
        int done = spreadPositions.containsKey(key) ? spreadPositions.get(key) : 0;
        if (done >= maxPositions) {
            return;
        }
        String ns = required("xmage.rollout.ns");
        if (ns.contains(",")) {
            throw new IllegalStateException("spread mode takes exactly one N, got xmage.rollout.ns=" + ns);
        }
        int n = Integer.parseInt(ns);
        long budgetMs = Long.parseLong(required("xmage.rollout.budgetMs"));
        int threads = Integer.parseInt(required("xmage.rollout.threads"));
        String out = required("xmage.rollout.out");
        long gs = gameSeed(game);
        // the seat's name enters the seed so both probed seats of one game draw disjoint families
        long base = RolloutCounter.mix(seedBase(gs, done, n, 0) + seat.hashCode());
        RolloutCounter.Critic critic = RolloutCounter.Critic.parse(required("xmage.rollout.critic"));
        ActionSpread.Spread sp = ActionSpread.measure(game, playerId, base, n, budgetMs, threads, critic);
        if (sp == null) {
            return; // one legal action: no choice to measure, not a position
        }
        spreadPositions.put(key, done + 1);
        StringBuilder sb = new StringBuilder();
        sb.append("{\"mode\":\"spread\",\"critic\":\"").append(critic).append("\",\"game_id\":\"").append(game.getId()).append("\",\"game_seed\":").append(gs)
                .append(",\"seat\":\"").append(seat).append("\",\"position\":").append(done)
                .append(",\"turn\":").append(game.getTurnNum())
                .append(",\"step\":\"").append(game.getTurnStepType()).append('"')
                .append(",\"active_player\":\"").append(game.getPlayer(game.getActivePlayerId()).getName()).append('"')
                .append(",\"stack\":").append(game.getStack().size())
                .append(",\"n\":").append(n).append(",\"budget_ms\":").append(budgetMs).append(",\"threads\":").append(threads)
                .append(",\"seed_base\":").append(base).append(",\"k\":").append(sp.actions.size())
                .append(",\"wall_ms\":").append(sp.wallMillis).append(",\"actions\":[");
        for (int i = 0; i < sp.actions.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            actionJson(sb, sp.actions.get(i));
        }
        sb.append("],\"placebo_passes\":[");
        for (int i = 0; i < sp.placeboPasses.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            actionJson(sb, sp.placeboPasses.get(i));
        }
        sb.append("]}");
        write(out, sb.toString());
    }

    private static void actionJson(StringBuilder sb, ActionSpread.ActionResult a) {
        sb.append("{\"i\":").append(a.index).append(",\"action\":\"").append(esc(a.action))
                .append("\",\"pass\":").append(a.isPass).append(",\"activated\":").append(a.activated);
        for (RolloutCounter.Outcome o : RolloutCounter.Outcome.values()) {
            sb.append(",\"").append(o.name().toLowerCase()).append("\":").append(a.result.count(o));
        }
        sb.append(",\"wall_ms\":").append(a.result.wallMillis).append('}');
    }

    private static String esc(String s) {
        StringBuilder sb = new StringBuilder();
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c < 0x20) {
                sb.append(' ');
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    private static String json(Game game, String seat, long gameSeed, int position, int repeat, RolloutCounter.Result r) {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"critic\":\"").append(System.getProperty("xmage.rollout.critic")).append("\",\"game_id\":\"").append(game.getId()).append("\",\"game_seed\":").append(gameSeed)
                .append(",\"seat\":\"").append(seat).append("\",\"position\":").append(position)
                .append(",\"turn\":").append(game.getTurnNum())
                .append(",\"active_player\":\"").append(game.getPlayer(game.getActivePlayerId()).getName()).append('"')
                .append(",\"n\":").append(r.n).append(",\"repeat\":").append(repeat)
                .append(",\"budget_ms\":").append(r.budgetMillis).append(",\"threads\":").append(r.threads)
                .append(",\"seed_base\":").append(r.seedBase).append(",\"wall_ms\":").append(r.wallMillis);
        for (RolloutCounter.Outcome o : RolloutCounter.Outcome.values()) {
            sb.append(",\"").append(o.name().toLowerCase()).append("\":").append(r.count(o));
        }
        sb.append(",\"rollouts\":[");
        for (int i = 0; i < r.rollouts.size(); i++) {
            RolloutCounter.Rollout x = r.rollouts.get(i);
            if (i > 0) {
                sb.append(',');
            }
            sb.append("{\"k\":").append(x.index).append(",\"outcome\":\"").append(x.outcome.name().toLowerCase())
                    .append("\",\"cut\":\"").append(x.cutReason).append("\",\"end_turn\":").append(x.endTurn)
                    .append(",\"actions\":").append(x.actions).append(",\"playout_ms\":").append(x.playoutMillis)
                    .append(",\"end\":\"").append(x.endState).append('"')
                    .append(",\"hash\":\"").append(x.transcriptHash, 0, 16).append("\"}");
        }
        sb.append("]}");
        return sb.toString();
    }

    private static void write(String path, String line) {
        try (FileWriter w = new FileWriter(path, true)) {
            w.write(line);
            w.write(System.lineSeparator());
        } catch (IOException e) {
            // a measurement that silently loses records reads as fewer positions, so this is fatal
            throw new IllegalStateException("rollout probe could not write " + path, e);
        }
    }
}
