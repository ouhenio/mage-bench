package org.mage.test.AI.rollout;

import mage.constants.PhaseStep;
import mage.constants.Zone;
import mage.game.Game;
import mage.player.ai.RolloutCounter;
import mage.player.ai.RolloutCounter.Outcome;
import mage.player.ai.RolloutCounter.Result;
import mage.player.ai.RolloutCounter.Rollout;
import mage.util.RandomUtil;
import org.junit.Assert;
import org.junit.Test;
import org.mage.test.serverside.base.CardTestPlayerBase;

import java.util.ArrayList;
import java.util.List;

/**
 * RolloutCounter's contracts: seeded replay (counts AND per-rollout transcripts), different seeds
 * differ, a zero budget gives only NO_RESULT through the real cut path, the live game's RNG
 * stream and state are untouched. Rollouts must demonstrably PLAY: RolloutRngTest's history is two
 * runs where every "rollout" was an engine error ending the copy as a draw at the position's turn.
 */
public class RolloutCounterTest extends CardTestPlayerBase {

    private static final long SEED_BASE = 3_000_001L;
    private static final int N = 8;
    private static final int THREADS = 4;
    // large enough that no playout here is cut (they take 1-3 s in these tests); asserted below
    private static final long UNCUT_BUDGET_MS = 120_000L;

    private Game position() {
        addCard(Zone.BATTLEFIELD, playerA, "Mountain", 3);
        addCard(Zone.BATTLEFIELD, playerA, "Grizzly Bears", 2);
        addCard(Zone.BATTLEFIELD, playerB, "Swamp", 3);
        addCard(Zone.BATTLEFIELD, playerB, "Hill Giant", 1);
        setStopAt(3, PhaseStep.PRECOMBAT_MAIN);
        execute();
        return currentGame;
    }

    private static List<String> hashes(Result r) {
        List<String> out = new ArrayList<>();
        for (Rollout x : r.rollouts) {
            out.add(x.transcriptHash);
        }
        return out;
    }

    private static List<String> ends(Result r) {
        List<String> out = new ArrayList<>();
        for (Rollout x : r.rollouts) {
            out.add(x.outcome + "@T" + x.endTurn + "[" + x.endState + "]");
        }
        return out;
    }

    private static String counts(Result r) {
        return "W" + r.count(Outcome.WIN) + " L" + r.count(Outcome.LOSS) + " D" + r.count(Outcome.DRAW)
                + " NR" + r.count(Outcome.NO_RESULT) + " wall=" + r.wallMillis + "ms";
    }

    // POSITIVE CONTROL: every rollout reached a result on a later turn, with actions taken
    private static void assertPlayed(Result r, int positionTurn) {
        for (Rollout x : r.rollouts) {
            Assert.assertNotEquals("rollout " + x.index + " was cut under an uncut budget", Outcome.NO_RESULT, x.outcome);
            Assert.assertTrue("rollout " + x.index + " did not play past the position (turn " + x.endTurn
                    + ", " + x.actions + " actions)", x.endTurn > positionTurn && x.actions > 0);
        }
    }

    @Test
    public void test_sameSeedBaseReplaysCountsAndTranscripts() {
        Game position = position();
        Result a = RolloutCounter.count(position, playerA.getId(), SEED_BASE, N, UNCUT_BUDGET_MS, THREADS);
        Result b = RolloutCounter.count(position, playerA.getId(), SEED_BASE, N, UNCUT_BUDGET_MS, THREADS);
        System.out.println("ROLLOUT_COUNT same-seed a: " + counts(a) + " " + ends(a));
        System.out.println("ROLLOUT_COUNT same-seed b: " + counts(b) + " " + hashes(b));
        assertPlayed(a, position.getTurnNum());
        assertPlayed(b, position.getTurnNum());
        Assert.assertEquals("per-rollout transcripts", hashes(a), hashes(b));
        for (Outcome o : Outcome.values()) {
            Assert.assertEquals("count of " + o, a.count(o), b.count(o));
        }
        Assert.assertEquals(UNCUT_BUDGET_MS, a.budgetMillis);
        Assert.assertEquals(SEED_BASE, a.seedBase);
    }

    @Test
    public void test_differentSeedBasesDiffer() {
        Game position = position();
        Result a = RolloutCounter.count(position, playerA.getId(), SEED_BASE, N, UNCUT_BUDGET_MS, THREADS);
        Result b = RolloutCounter.count(position, playerA.getId(), SEED_BASE + 1, N, UNCUT_BUDGET_MS, THREADS);
        assertPlayed(a, position.getTurnNum());
        assertPlayed(b, position.getTurnNum());
        Assert.assertNotEquals("two seed bases produced the same rollouts", hashes(a), hashes(b));
        // and within one call, rollouts are not one playout repeated
        Assert.assertTrue("all rollouts of one call identical: " + hashes(a),
                new java.util.HashSet<>(hashes(a)).size() > 1);
    }

    @Test
    public void test_zeroBudgetIsAllNoResult() {
        Game position = position();
        Result r = RolloutCounter.count(position, playerA.getId(), SEED_BASE, N, 0L, THREADS);
        Assert.assertEquals(counts(r), N, r.count(Outcome.NO_RESULT));
        for (Rollout x : r.rollouts) {
            Assert.assertEquals("rollout " + x.index, "deadline", x.cutReason);
        }
        // and the pooled threads were left clean: an uncut call right after still plays to results
        Result after = RolloutCounter.count(position, playerA.getId(), SEED_BASE, 2, UNCUT_BUDGET_MS, THREADS);
        assertPlayed(after, position.getTurnNum());
    }

    @Test
    public void test_liveGameStreamAndStateUntouched() {
        Game position = position();
        String stateBefore = position.getState().getValue(true, position);
        int turnBefore = position.getTurnNum();

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
        Result r = RolloutCounter.count(position, playerA.getId(), SEED_BASE, N, UNCUT_BUDGET_MS, THREADS);
        assertPlayed(r, turnBefore);
        for (int i = 25; i < got.length; i++) {
            got[i] = RandomUtil.nextInt(1_000_000);
        }
        Assert.assertArrayEquals("the live game's stream moved during count()", expected, got);
        Assert.assertEquals("the live game's state changed during count()", stateBefore,
                position.getState().getValue(true, position));
        Assert.assertEquals(turnBefore, position.getTurnNum());
    }

    /**
     * WIN MUST BE REACHABLE. The default test deck is 71 Mountains, so the board above never gains
     * a creature, and every rollout so far has been a LOSS for the seat (two 2/2s against a 3/3).
     * That may be the position's true value, or a counter that cannot score a win. Lopsided boards
     * each way tell them apart: the seat with three Hill Giants against nothing must mostly win,
     * and the reverse must mostly lose.
     */
    @Test
    public void test_outcomeTracksTheBoard() {
        addCard(Zone.BATTLEFIELD, playerA, "Mountain", 3);
        addCard(Zone.BATTLEFIELD, playerA, "Hill Giant", 3);
        addCard(Zone.BATTLEFIELD, playerB, "Swamp", 3);
        setStopAt(3, PhaseStep.PRECOMBAT_MAIN);
        execute();
        Result forA = RolloutCounter.count(currentGame, playerA.getId(), SEED_BASE, N, UNCUT_BUDGET_MS, THREADS);
        Result forB = RolloutCounter.count(currentGame, playerB.getId(), SEED_BASE, N, UNCUT_BUDGET_MS, THREADS);
        System.out.println("ROLLOUT_COUNT lopsided, seat A (three giants): " + counts(forA) + " " + ends(forA));
        System.out.println("ROLLOUT_COUNT lopsided, seat B (empty board): " + counts(forB) + " " + ends(forB));
        assertPlayed(forA, currentGame.getTurnNum());
        assertPlayed(forB, currentGame.getTurnNum());
        Assert.assertTrue("the favoured seat should mostly WIN: " + counts(forA), forA.count(Outcome.WIN) > N / 2);
        Assert.assertTrue("the unfavoured seat should mostly LOSE: " + counts(forB), forB.count(Outcome.LOSS) > N / 2);
    }

    /**
     * ROLLOUT SEEDS MUST NOT SHARE THEIR FIRST DRAWS. java.util.Random seeded with CONSECUTIVE
     * values returns the same first draw for every power-of-two bound (nextBoolean included): the
     * LCG's first step moves adjacent seeds apart by only 0x5DEECE66D in 2^48. With the old
     * rolloutSeed = seedBase * 1_000_003 + k, 256 of 256 rollouts (red run 10134; 64 of 64 in a
     * direct replication) of one call drew the same first
     * nextInt(2), so they were correlated and N rollouts were worth fewer than N -- the suspected
     * cause of job 10133's between-repeat SD staying at 0.049 from N=64 to N=128 (overdispersion
     * 1.77 at N=128). No game needed: this is a property of the seeds alone.
     */
    @Test
    public void test_rolloutSeedsDecorrelateFirstDraw() {
        int n = 256;
        int ones = 0;
        java.util.Set<Integer> first64 = new java.util.HashSet<>();
        for (int k = 0; k < n; k++) {
            ones += new java.util.Random(RolloutCounter.rolloutSeed(SEED_BASE, k)).nextInt(2);
            first64.add(new java.util.Random(RolloutCounter.rolloutSeed(SEED_BASE, k)).nextInt(64));
        }
        // binomial(256, 0.5): mean 128, sd 8; +-40 is five sd
        Assert.assertTrue("first nextInt(2) over " + n + " consecutive rollouts: " + ones + " ones",
                ones > 88 && ones < 168);
        Assert.assertTrue("first nextInt(64) takes only " + first64.size() + " distinct values over " + n + " rollouts",
                first64.size() >= 48);
    }
}
