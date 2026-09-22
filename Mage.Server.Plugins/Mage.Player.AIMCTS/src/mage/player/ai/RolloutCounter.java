package mage.player.ai;

import mage.MageObject;
import mage.cards.Card;
import mage.constants.WatcherScope;
import mage.constants.Zone;
import mage.game.Game;
import mage.game.events.BatchEvent;
import mage.game.events.GameEvent;
import mage.players.Player;
import mage.util.RandomUtil;
import mage.util.ThreadUtils;
import mage.watchers.Watcher;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * "From this state, play N rollouts and count the outcomes": MCTSNode.simulate generalised from
 * one win/loss bit to a count over N, for the position environment's reward (mtg repo,
 * docs/eval/rollout-primitive-scoping.md). A training-time signal only -- never offered to a
 * playing policy as a tool.
 * <p>
 * Each rollout copies the live game (createSimulationForAI), replaces every seat with
 * SimulatedPlayerMCTS (uniform random over playables) and resamples hidden information as
 * MCTSNode.randomizePlayers does, then plays to game end. The live game is never mutated:
 * throwing the copy away is the restore. So a rollout samples the INFORMATION SET as the seat
 * sees it, not the true position.
 * <p>
 * THREE CONTRACTS, each tested in Mage.Tests RolloutCounterTest:
 * <ul>
 * <li>SEEDED. Rollout k of a call runs on its own RandomUtil thread stream seeded
 * {@code seedBase * 1_000_003 + k}, so the same seedBase replays the same outcomes AND the same
 * per-rollout transcripts, and the live game's own stream does not move.</li>
 * <li>BUDGETED, AND A CUT IS NOT A LOSS. Each rollout's playout gets {@code budgetMillis}; at the
 * deadline its thread is interrupted, which is the cut MCTS itself uses (invokeAll's timeout).
 * checkIfGameIsOver then returns true WITHOUT ending the game, so hasEnded() tells a result from a
 * cut. MCTSNode.simulate scores a cut as -1; here it is {@link Outcome#NO_RESULT}, counted
 * separately. At skill-1 budgets 68% of playouts were cut (job 10103), so scoring cuts as losses
 * would bias every value toward losing, more so the shorter the budget.</li>
 * <li>THE BUDGET IS PART OF THE RESULT. It sets the no-result rate, so it is recorded with the
 * counts rather than left implicit.</li>
 * </ul>
 * Rollouts run on threads named AI-SIM-MCTS, the only name ThreadUtils accepts for simulated
 * game code outside the game thread besides AI-SIM-MAD.
 */
public final class RolloutCounter {

    public enum Outcome {WIN, LOSS, DRAW, NO_RESULT}

    public static final class Rollout {
        public final int index;
        public final long seed;
        public final Outcome outcome;
        public final String cutReason;   // "" unless NO_RESULT: "deadline" or "stopped_uninterrupted"
        public final int actions;
        public final int endTurn;
        public final int events;
        public final String transcriptHash;
        public final long playoutMillis;

        Rollout(int index, long seed, Outcome outcome, String cutReason, int actions, int endTurn,
                int events, String transcriptHash, long playoutMillis) {
            this.index = index;
            this.seed = seed;
            this.outcome = outcome;
            this.cutReason = cutReason;
            this.actions = actions;
            this.endTurn = endTurn;
            this.events = events;
            this.transcriptHash = transcriptHash;
            this.playoutMillis = playoutMillis;
        }
    }

    public static final class Result {
        public final long seedBase;
        public final int n;
        public final long budgetMillis;
        public final int threads;
        public final long wallMillis;
        public final List<Rollout> rollouts;

        Result(long seedBase, int n, long budgetMillis, int threads, long wallMillis, List<Rollout> rollouts) {
            this.seedBase = seedBase;
            this.n = n;
            this.budgetMillis = budgetMillis;
            this.threads = threads;
            this.wallMillis = wallMillis;
            this.rollouts = rollouts;
        }

        public int count(Outcome o) {
            int c = 0;
            for (Rollout r : rollouts) {
                if (r.outcome == o) {
                    c++;
                }
            }
            return c;
        }
    }

    private RolloutCounter() {
    }

    public static long rolloutSeed(long seedBase, int k) {
        return seedBase * 1_000_003L + k;
    }

    /**
     * Play n rollouts of {@code live} for {@code seatId}. Call it where the live game is quiescent
     * -- the game thread, e.g. at a priority decision -- since the copy reads live state.
     */
    public static Result count(Game live, UUID seatId, long seedBase, int n, long budgetMillis, int threads) {
        if (n < 1 || threads < 1) {
            throw new IllegalArgumentException("n and threads must be >= 1, got n=" + n + " threads=" + threads);
        }
        if (budgetMillis < 0) {
            throw new IllegalArgumentException("budgetMillis must be >= 0, got " + budgetMillis);
        }
        if (live.getPlayer(seatId) == null) {
            throw new IllegalArgumentException("seat " + seatId + " is not a player of this game");
        }
        AtomicInteger names = new AtomicInteger();
        ExecutorService pool = Executors.newFixedThreadPool(Math.min(n, threads),
                r -> new Thread(r, ThreadUtils.THREAD_PREFIX_AI_SIMULATION_MCTS + " - rollout-" + names.incrementAndGet()));
        ScheduledExecutorService watchdog = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "rollout-deadline");
            t.setDaemon(true);
            return t;
        });
        long t0 = System.nanoTime();
        try {
            List<Future<Rollout>> futures = new ArrayList<>();
            for (int k = 0; k < n; k++) {
                final int index = k;
                final long seed = rolloutSeed(seedBase, k);
                futures.add(pool.submit(() -> RandomUtil.withThreadSeed(seed,
                        () -> one(live, seatId, index, seed, budgetMillis, watchdog))));
            }
            List<Rollout> out = new ArrayList<>();
            for (Future<Rollout> f : futures) {
                try {
                    out.add(f.get());
                } catch (ExecutionException e) {
                    throw new IllegalStateException("a rollout failed", e.getCause());
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw new IllegalStateException("interrupted while waiting for rollouts", e);
                }
            }
            long wall = (System.nanoTime() - t0) / 1_000_000L;
            return new Result(seedBase, n, budgetMillis, Math.min(n, threads), wall, out);
        } finally {
            pool.shutdownNow();
            watchdog.shutdownNow();
        }
    }

    private static Rollout one(Game live, UUID seatId, int index, long seed, long budgetMillis,
                               ScheduledExecutorService watchdog) {
        Game sim = createSimulation(live, seatId);
        TranscriptWatcher watcher = new TranscriptWatcher();
        sim.getState().addWatcher(watcher);

        Thread me = Thread.currentThread();
        Object lock = new Object();
        boolean[] done = {false};
        long start = System.nanoTime();
        java.util.concurrent.ScheduledFuture<?> deadline = null;
        boolean interruptedAtReturn;
        try {
            if (budgetMillis == 0) {
                // B=0 through the real cut path, not a special case: the playout starts interrupted.
                me.interrupt();
            } else {
                deadline = watchdog.schedule(() -> {
                    synchronized (lock) {
                        if (!done[0]) {
                            me.interrupt();
                        }
                    }
                }, budgetMillis, TimeUnit.MILLISECONDS);
            }
            sim.resume();
        } finally {
            // The deadline must not fire into the NEXT task on this pooled thread: mark done under
            // the lock the deadline takes, then consume any interrupt it already delivered.
            synchronized (lock) {
                done[0] = true;
            }
            if (deadline != null) {
                deadline.cancel(false);
            }
            interruptedAtReturn = Thread.interrupted();
        }
        long playoutMillis = (System.nanoTime() - start) / 1_000_000L;

        Outcome outcome;
        String cutReason = "";
        if (!sim.hasEnded()) {
            outcome = Outcome.NO_RESULT;
            // an unended game that was NOT interrupted stopped for some other reason; named, not guessed
            cutReason = interruptedAtReturn ? "deadline" : "stopped_uninterrupted";
        } else {
            List<UUID> winners = new ArrayList<>();
            for (Player p : sim.getState().getPlayers().values()) {
                if (p.hasWon()) {
                    winners.add(p.getId());
                }
            }
            outcome = winners.contains(seatId) ? Outcome.WIN : winners.isEmpty() ? Outcome.DRAW : Outcome.LOSS;
        }
        int actions = 0;
        for (Player p : sim.getState().getPlayers().values()) {
            actions += ((SimulatedPlayerMCTS) p).getActionCount();
        }
        return new Rollout(index, seed, outcome, cutReason, actions, sim.getTurnNum(), watcher.lines.size(),
                watcher.hash(outcome + "|turn=" + sim.getTurnNum()), playoutMillis);
    }

    // MCTSNode.createSimulation + randomizePlayers, but restoring from getRealPlayer() so a live
    // seat that wraps a PlayerImpl (as Mage.Tests' TestPlayer does) restores from the PlayerImpl.
    static Game createSimulation(Game live, UUID seatId) {
        Game sim = live.createSimulationForAI();
        // a stop condition copied from the live game's options would halt the playout early
        sim.getOptions().stopOnTurn = null;
        for (Player old : new ArrayList<>(sim.getState().getPlayers().values())) {
            Player orig = live.getState().getPlayers().get(old.getId()).getRealPlayer().copy();
            SimulatedPlayerMCTS random = new SimulatedPlayerMCTS(old, true);
            random.restore(orig);
            sim.getState().getPlayers().put(old.getId(), random);
        }
        for (Player player : sim.getState().getPlayers().values()) {
            if (!player.getId().equals(seatId)) {
                int handSize = player.getHand().size();
                player.getLibrary().addAll(player.getHand().getCards(sim), sim);
                player.getHand().clear();
                player.getLibrary().shuffle();
                for (int i = 0; i < handSize; i++) {
                    Card card = player.getLibrary().drawFromTop(sim);
                    card.setZone(Zone.HAND, sim);
                    player.getHand().add(card);
                }
            } else {
                player.getLibrary().shuffle();
            }
        }
        return sim;
    }

    /**
     * Every game event of the playout, objects NAMED rather than by UUID: tokens and copies get
     * UUID.randomUUID() ids no seed controls. A BatchEvent refuses getTargetId() (it throws, and
     * the engine's error handler then ends the game as a draw), so its members are recorded,
     * SORTED because getEvents() is an identity-hashed HashSet. One constructor only:
     * Watcher.copy() reflects over exactly one.
     */
    static final class TranscriptWatcher extends Watcher {

        final List<String> lines = new ArrayList<>();

        TranscriptWatcher() {
            super(WatcherScope.GAME);
        }

        @Override
        public void watch(GameEvent event, Game game) {
            if (event instanceof BatchEvent) {
                List<String> members = new ArrayList<>();
                for (Object member : ((BatchEvent<?>) event).getEvents()) {
                    members.add(line((GameEvent) member, game));
                }
                Collections.sort(members);
                lines.add(game.getTurnNum() + "|" + game.getTurnStepType() + "|" + event.getType() + "|batch" + members);
                return;
            }
            lines.add(line(event, game));
        }

        private static String line(GameEvent event, Game game) {
            return game.getTurnNum() + "|" + game.getTurnStepType() + "|" + event.getType()
                    + "|t=" + name(event.getTargetId(), game) + "|s=" + name(event.getSourceId(), game)
                    + "|p=" + name(event.getPlayerId(), game) + "|a=" + event.getAmount()
                    + "|f=" + event.getFlag();
        }

        private static String name(UUID id, Game game) {
            if (id == null) {
                return "-";
            }
            Player player = game.getPlayer(id);
            if (player != null) {
                return "P:" + player.getName();
            }
            MageObject object = game.getObject(id);
            return object == null ? "?" : object.getName();
        }

        String hash(String end) {
            try {
                MessageDigest md = MessageDigest.getInstance("SHA-256");
                for (String l : lines) {
                    md.update(l.getBytes(StandardCharsets.UTF_8));
                    md.update((byte) '\n');
                }
                md.update(end.getBytes(StandardCharsets.UTF_8));
                StringBuilder sb = new StringBuilder();
                for (byte b : md.digest()) {
                    sb.append(String.format("%02x", b));
                }
                return sb.toString();
            } catch (NoSuchAlgorithmException e) {
                throw new IllegalStateException(e);
            }
        }
    }
}
