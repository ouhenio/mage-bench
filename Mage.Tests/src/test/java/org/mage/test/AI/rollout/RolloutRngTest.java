package org.mage.test.AI.rollout;

import mage.MageObject;
import mage.cards.Card;
import mage.constants.PhaseStep;
import mage.constants.WatcherScope;
import mage.constants.Zone;
import mage.game.Game;
import mage.game.events.BatchEvent;
import mage.game.events.GameEvent;
import mage.player.ai.SimulatedPlayerMCTS;
import mage.players.Player;
import mage.util.RandomUtil;
import mage.util.ThreadUtils;
import mage.watchers.Watcher;
import org.junit.Assert;
import org.junit.Test;
import org.mage.test.serverside.base.CardTestPlayerBase;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Per-rollout RNG: a rollout seeded (gameSeed, k) must replay exactly, rollouts k and k+1 must
 * differ, and seeding a rollout must not touch the live game's stream. Specified in the mtg repo,
 * docs/eval/instance-rng-estimate.md, which also says the test comes first and must fail against
 * the unmodified engine: RandomUtil held one process-global Random, so "seed this rollout"
 * reseeded the host game and every concurrent rollout with it.
 * <p>
 * A rollout here is MCTSNode.simulate without the tree: copy the position with
 * createSimulationForAI, replace every seat with SimulatedPlayerMCTS (random play), resample hidden
 * information as MCTSNode.randomizePlayers does, and resume to game end. It runs on an AI-SIM-MCTS
 * thread because that is where MCTS runs it (and ThreadUtils rejects game code anywhere else).
 * <p>
 * The comparison is over a TRANSCRIPT HASH -- every game event of the playout, with objects named
 * rather than by UUID -- not over the win/loss bit, which two unrelated playouts share half the
 * time. Objects are named because tokens and copies get fresh UUID.randomUUID() ids that no seed
 * controls; if names alone still diverge under one seed, that is the spec's hazard 2 (iteration
 * order over UUID-keyed maps), which the sequential test isolates from the concurrency one.
 */
public class RolloutRngTest extends CardTestPlayerBase {

    private static final long GAME_SEED = 3_000_001L;
    private static final int ROLLOUTS = 4;

    // (gameSeed, rollout index) -> seed, the pattern ComputerPlayer6.beginTiebreakSequence uses
    private static long rolloutSeed(long gameSeed, int k) {
        return gameSeed * 1_000_003L + k;
    }

    // Records the playout. One constructor only: Watcher.copy() reflects over exactly one.
    private static final class TranscriptWatcher extends Watcher {

        private final List<String> lines = new ArrayList<>();

        TranscriptWatcher() {
            super(WatcherScope.GAME);
        }

        @Override
        public void watch(GameEvent event, Game game) {
            // A BatchEvent refuses getTargetId() by design (it throws, and the engine's error
            // handler then ENDS the game -- run 10108's "draw at turn 3"). Record its members.
            //
            // Members are SORTED: getEvents() is a HashSet of identity-hashed events, so its order
            // differs run to run and would put recording noise into the hash. If the ENGINE's
            // behaviour depends on that order, it still shows up in the events that follow.
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
    }

    private static final class Transcript {
        final String hash;
        final int events;
        final String end;

        Transcript(List<String> lines, String end) {
            this.events = lines.size();
            this.end = end;
            try {
                MessageDigest md = MessageDigest.getInstance("SHA-256");
                for (String line : lines) {
                    md.update(line.getBytes(StandardCharsets.UTF_8));
                    md.update((byte) '\n');
                }
                md.update(end.getBytes(StandardCharsets.UTF_8));
                StringBuilder sb = new StringBuilder();
                for (byte b : md.digest()) {
                    sb.append(String.format("%02x", b));
                }
                this.hash = sb.toString();
            } catch (java.security.NoSuchAlgorithmException e) {
                throw new IllegalStateException(e);
            }
        }

        @Override
        public String toString() {
            return hash.substring(0, 12) + " (" + events + " events, " + end + ")";
        }
    }

    private Game position() {
        // a mid-game position with creatures on both sides and real 60-card libraries (RB Aggro)
        addCard(Zone.BATTLEFIELD, playerA, "Mountain", 3);
        addCard(Zone.BATTLEFIELD, playerA, "Grizzly Bears", 2);
        addCard(Zone.BATTLEFIELD, playerB, "Swamp", 3);
        addCard(Zone.BATTLEFIELD, playerB, "Hill Giant", 1);
        setStopAt(3, PhaseStep.PRECOMBAT_MAIN);
        execute();
        return currentGame;
    }

    private static Transcript rollout(Game position, UUID seatId, long seed) {
        return RandomUtil.withThreadSeed(seed, () -> {
            Game sim = position.createSimulationForAI();
            // the options were copied with the host's stop condition, which would halt the playout
            // at the next PRECOMBAT_MAIN instead of game end
            sim.getOptions().stopOnTurn = null;
            for (Player old : new ArrayList<>(sim.getState().getPlayers().values())) {
                Player orig = position.getState().getPlayers().get(old.getId()).getRealPlayer().copy();
                SimulatedPlayerMCTS random = new SimulatedPlayerMCTS(old, true);
                random.restore(orig);
                sim.getState().getPlayers().put(old.getId(), random);
            }
            // MCTSNode.randomizePlayers: the opponent's hand is resampled from its library
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
            TranscriptWatcher watcher = new TranscriptWatcher();
            sim.getState().addWatcher(watcher);
            String before = describe(sim);
            Assert.assertFalse("the copied position is already over: " + before, sim.getState().isGameOver());
            sim.resume();
            // POSITIVE CONTROL. Red run 10104 ended every rollout as a draw at turn 3 after 11
            // events and the hasEnded() check alone passed it: "ended" is not "played". A playout
            // that took no action is not a rollout, and hashes of it prove nothing.
            int actions = 0;
            for (Player player : sim.getState().getPlayers().values()) {
                actions += ((SimulatedPlayerMCTS) player).getActionCount();
            }
            // ...and one action is not a playout either: 10108 passed "actions > 0" with every
            // rollout ended by an exception after 1-3 actions. A random playout from 20 life ends
            // with a LOSER on a later turn; the engine's error handler ends it as a draw instead.
            boolean someoneLost = false;
            for (Player player : sim.getState().getPlayers().values()) {
                someoneLost |= player.hasLost();
            }
            Assert.assertTrue("the playout did not play to a result. before: " + before + " after: " + describe(sim)
                    + " actions=" + actions + " transcript tail: "
                    + watcher.lines.subList(Math.max(0, watcher.lines.size() - 10), watcher.lines.size()),
                    actions > 0 && someoneLost && sim.getTurnNum() > position.getTurnNum());
            Assert.assertTrue("the playout must reach game end, not stop early", sim.hasEnded());
            Transcript t = new Transcript(watcher.lines,
                    "winner=" + sim.getWinner() + " turn=" + sim.getTurnNum() + " actions=" + actions);
            // one line per rollout in the job log: the evidence that each one played, and how much
            System.out.println("ROLLOUT seed=" + seed + " thread=" + Thread.currentThread().getName() + " " + t);
            return t;
        });
    }

    private static String describe(Game sim) {
        StringBuilder sb = new StringBuilder("turn=" + sim.getTurnNum() + " step=" + sim.getTurnStepType()
                + " paused=" + sim.isPaused() + " over=" + sim.getState().isGameOver() + " ended=" + sim.hasEnded()
                + " stopOnTurn=" + sim.getOptions().stopOnTurn);
        for (Player player : sim.getState().getPlayers().values()) {
            sb.append(" [").append(player.getName()).append(' ').append(player.getClass().getSimpleName())
                    .append(" life=").append(player.getLife()).append(" left=").append(player.hasLeft())
                    .append(" lost=").append(player.hasLost()).append(" lib=").append(player.getLibrary().size())
                    .append(" hand=").append(player.getHand().size()).append(']');
        }
        return sb.toString();
    }

    private static ExecutorService simPool(int threads) {
        AtomicInteger n = new AtomicInteger();
        return Executors.newFixedThreadPool(threads,
                r -> new Thread(r, ThreadUtils.THREAD_PREFIX_AI_SIMULATION_MCTS + " - rng-test-" + n.incrementAndGet()));
    }

    // k -> transcript, all rollouts running at once, as MCTS runs them (one per pool thread)
    private static Map<Integer, Transcript> concurrentRollouts(Game position, UUID seatId,
                                                               AtomicBoolean hostDrawing) throws Exception {
        ExecutorService pool = simPool(ROLLOUTS);
        // the live game keeps drawing from its own stream while the rollouts run
        Thread host = new Thread(() -> {
            while (hostDrawing.get()) {
                RandomUtil.nextInt(1000);
            }
        }, "host-game-draws");
        try {
            host.start();
            Map<Integer, Future<Transcript>> futures = new HashMap<>();
            for (int k = 0; k < ROLLOUTS; k++) {
                long seed = rolloutSeed(GAME_SEED, k);
                futures.put(k, pool.submit(() -> rollout(position, seatId, seed)));
            }
            Map<Integer, Transcript> out = new HashMap<>();
            for (Map.Entry<Integer, Future<Transcript>> e : futures.entrySet()) {
                out.put(e.getKey(), e.getValue().get(10, TimeUnit.MINUTES));
            }
            return out;
        } finally {
            hostDrawing.set(false);
            host.join();
            pool.shutdownNow();
        }
    }

    private static Transcript onSimThread(Game position, UUID seatId, long seed) throws Exception {
        ExecutorService pool = simPool(1);
        try {
            return pool.submit(() -> rollout(position, seatId, seed)).get(10, TimeUnit.MINUTES);
        } finally {
            pool.shutdownNow();
        }
    }

    /**
     * Test 1a, SEQUENTIAL. One rollout at a time and nothing else drawing, so even a global stream
     * reseeded per rollout replays. If THIS fails, the seed is not the only nondeterminism: the
     * spec's hazard 2 (UUID-keyed iteration order), which is a far larger change than the RNG.
     */
    @Test
    public void test_sameSeedReplays_sequential() throws Exception {
        Game position = position();
        UUID seat = playerA.getId();
        Transcript first = onSimThread(position, seat, rolloutSeed(GAME_SEED, 0));
        Transcript second = onSimThread(position, seat, rolloutSeed(GAME_SEED, 0));
        Assert.assertEquals("same seed, one rollout at a time: " + first + " vs " + second,
                first.hash, second.hash);
    }

    /**
     * Test 1, the one the global stream fails: the same seeds replay while N rollouts and the live
     * game draw concurrently.
     */
    @Test
    public void test_sameSeedReplays_concurrent() throws Exception {
        Game position = position();
        UUID seat = playerA.getId();
        Map<Integer, Transcript> run1 = concurrentRollouts(position, seat, new AtomicBoolean(true));
        Map<Integer, Transcript> run2 = concurrentRollouts(position, seat, new AtomicBoolean(true));
        for (int k = 0; k < ROLLOUTS; k++) {
            Assert.assertEquals("rollout k=" + k + " replayed differently: " + run1.get(k) + " vs " + run2.get(k),
                    run1.get(k).hash, run2.get(k).hash);
        }
    }

    /**
     * Test 2: different seeds differ. Without it, an implementation that pins every rollout to
     * one stream passes test 1 and turns N rollouts into one.
     */
    @Test
    public void test_differentSeedsDiffer() throws Exception {
        Game position = position();
        UUID seat = playerA.getId();
        Map<Integer, Transcript> run = concurrentRollouts(position, seat, new AtomicBoolean(true));
        for (int k = 0; k + 1 < ROLLOUTS; k++) {
            Assert.assertNotEquals("rollouts k=" + k + " and k=" + (k + 1) + " are one playout: " + run.get(k),
                    run.get(k).hash, run.get(k + 1).hash);
        }
    }

    /**
     * Test 3: seeding a rollout leaves the live game's stream exactly where it was -- on another
     * thread and on the host's own thread. The scoping note's first consequence: with one global
     * Random, seeding a rollout reseeded the live game and destroyed its reproducibility.
     */
    @Test
    public void test_hostStreamUntouched() throws Exception {
        Game position = position();
        UUID seat = playerA.getId();

        RandomUtil.setSeed(77);
        int[] expected = new int[50];
        for (int i = 0; i < expected.length; i++) {
            expected[i] = RandomUtil.nextInt(1_000_000);
        }

        RandomUtil.setSeed(77);
        int[] got = new int[50];
        for (int i = 0; i < 25; i++) {
            got[i] = RandomUtil.nextInt(1_000_000);
        }
        onSimThread(position, seat, rolloutSeed(GAME_SEED, 0));
        // a seeded draw ON the host's own thread must not leak into its stream either
        RandomUtil.withThreadSeed(rolloutSeed(GAME_SEED, 1), () -> RandomUtil.nextInt(1_000_000));
        for (int i = 25; i < got.length; i++) {
            got[i] = RandomUtil.nextInt(1_000_000);
        }
        Assert.assertArrayEquals("the host's stream moved while a rollout was seeded", expected, got);
    }

    /**
     * The pooled-thread hazards: a task that THROWS still clears its stream (else it leaks into
     * the next task on that thread, which then refuses to seed), and reseeding the process
     * stream from inside a rollout is refused rather than done.
     */
    @Test
    public void test_streamClearedOnThrowAndProcessReseedRefused() throws Exception {
        ExecutorService pool = simPool(1);
        try {
            Future<?> thrower = pool.submit(() -> RandomUtil.withThreadSeed(1L, () -> {
                throw new IllegalArgumentException("task failed mid-rollout");
            }));
            try {
                thrower.get(1, TimeUnit.MINUTES);
                Assert.fail("the throwing task should have thrown");
            } catch (java.util.concurrent.ExecutionException expected) {
                Assert.assertTrue(expected.getCause() instanceof IllegalArgumentException);
            }
            // same (only) pool thread: seeding again must succeed, i.e. the finally cleared it
            int drawn = pool.submit(() -> RandomUtil.withThreadSeed(2L, () -> RandomUtil.nextInt(1000)))
                    .get(1, TimeUnit.MINUTES);
            Assert.assertEquals(new java.util.Random(2L).nextInt(1000), drawn);

            Future<?> reseed = pool.submit(() -> RandomUtil.withThreadSeed(3L, () -> {
                RandomUtil.setSeed(99L);
                return null;
            }));
            try {
                reseed.get(1, TimeUnit.MINUTES);
                Assert.fail("setSeed inside a rollout must be refused");
            } catch (java.util.concurrent.ExecutionException expected) {
                Assert.assertTrue(String.valueOf(expected.getCause()), expected.getCause() instanceof IllegalStateException);
            }
        } finally {
            pool.shutdownNow();
        }
    }
}
