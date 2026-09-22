package mage.player.ai;

import mage.constants.PhaseStep;
import mage.game.Game;
import org.apache.log4j.Logger;

import java.io.FileWriter;
import java.io.IOException;
import java.util.HashMap;
import java.util.Map;
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

    // SplitMix64 finaliser: disjoint seed families per (game, position, N, repeat)
    private static long mix(long x) {
        x += 0x9E3779B97F4A7C15L;
        x = (x ^ (x >>> 30)) * 0xBF58476D1CE4E5B9L;
        x = (x ^ (x >>> 27)) * 0x94D049BB133111EBL;
        return x ^ (x >>> 31);
    }

    static long seedBase(long gameSeed, int position, int n, int repeat) {
        return mix(mix(mix(mix(gameSeed) + position) + n) + repeat);
    }

    public static synchronized void probe(Game game, UUID playerId, String playerName) {
        if (!isProbedSeat(playerName)) {
            return;
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
                RolloutCounter.Result r = RolloutCounter.count(game, playerId, base, n, budgetMs, threads);
                write(out, json(game, playerName, gameSeed, position, repeat, r));
            }
        }
        logger.info("rollout probe: game " + gameId + " position " + position + " (turn " + turn + ") done in "
                + (System.nanoTime() - t0) / 1_000_000L + " ms");
    }

    private static String json(Game game, String seat, long gameSeed, int position, int repeat, RolloutCounter.Result r) {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"game_id\":\"").append(game.getId()).append("\",\"game_seed\":").append(gameSeed)
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
