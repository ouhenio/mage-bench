package org.mage.test.AI.rollout;

import mage.constants.PhaseStep;
import mage.constants.Zone;
import mage.game.Game;
import mage.player.ai.ActionSpread;
import mage.player.ai.ActionSpread.ActionResult;
import mage.player.ai.ActionSpread.Spread;
import mage.player.ai.RolloutCounter;
import mage.util.RandomUtil;
import org.junit.Assert;
import org.junit.Test;
import org.mage.test.serverside.base.CardTestPlayerBase;

import java.util.ArrayList;
import java.util.List;

/**
 * ActionSpread: each legal action applied on a copy, rollouts counted from the result under common
 * random numbers. The positive control is a position whose best action is known -- Lightning
 * Bolt to the face of an opponent at 3 life wins on resolution -- so a spread that cannot find it,
 * or finds no spread, fails here rather than in the measurement.
 */
public class ActionSpreadTest extends CardTestPlayerBase {

    private static final long SEED_BASE = 5_000_011L;
    private static final long UNCUT_BUDGET_MS = 120_000L;
    private static final int THREADS = 4;

    // Bolt to B's face (B at 3) wins on resolution; everything else leaves A at 3 facing two Hill
    // Giants. The first version gave B no threats, and EVERY action -- Bolt to A's own face
    // included -- scored p=1.0 (job 10138): A won by decking whatever it did. A truly flat
    // position is no positive control for spread.
    private Game boltPosition() {
        addCard(Zone.BATTLEFIELD, playerA, "Mountain", 1);
        addCard(Zone.HAND, playerA, "Lightning Bolt", 1);
        addCard(Zone.BATTLEFIELD, playerB, "Hill Giant", 2);
        setLife(playerA, 3);
        setLife(playerB, 3);
        setStopAt(1, PhaseStep.PRECOMBAT_MAIN);
        execute();
        return currentGame;
    }

    private static String describe(Spread s) {
        List<String> out = new ArrayList<>();
        for (ActionResult a : s.actions) {
            out.add(String.format("[%d %s act=%s p=%.3f nr=%d]", a.index, a.action, a.activated, a.winProb(),
                    a.result.count(RolloutCounter.Outcome.NO_RESULT)));
        }
        List<String> placebo = new ArrayList<>();
        for (ActionResult a : s.placeboPasses) {
            placebo.add(String.format("%.3f", a.winProb()));
        }
        return out + " placebo " + placebo;
    }

    @Test
    public void test_boltToTheFaceIsTheBestAction() {
        Game position = boltPosition();
        Spread s = ActionSpread.measure(position, playerA.getId(), SEED_BASE, 32, UNCUT_BUDGET_MS, THREADS);
        Assert.assertNotNull("a position with a castable Bolt has more than one action", s);
        System.out.println("ACTION_SPREAD bolt: " + describe(s));
        ActionResult best = null;
        double min = 1.0;
        for (ActionResult a : s.actions) {
            Assert.assertTrue("action " + a.action + " did not activate on its copy", a.activated);
            if (best == null || a.winProb() > best.winProb()) {
                best = a;
            }
            min = Math.min(min, a.winProb());
        }
        Assert.assertTrue("best action should be Bolt at PlayerB, was " + best.action,
                best.action.contains("Lightning Bolt") && best.action.contains("P:PlayerB"));
        Assert.assertEquals("Bolt to the face at 3 life wins on resolution", 1.0, best.winProb(), 1e-9);
        Assert.assertTrue("spread should be well above noise: " + describe(s), best.winProb() - min > 0.084);
        for (ActionResult a : s.actions) {
            if (a.action.contains("P:PlayerA")) {
                Assert.assertEquals("Bolt to one's own face at 3 life loses", 0.0, a.winProb(), 1e-9);
            }
        }
    }

    @Test
    public void test_sameSeedReproducesTheSpread() {
        Game position = boltPosition();
        Spread a = ActionSpread.measure(position, playerA.getId(), SEED_BASE, 16, UNCUT_BUDGET_MS, THREADS);
        Spread b = ActionSpread.measure(position, playerA.getId(), SEED_BASE, 16, UNCUT_BUDGET_MS, THREADS);
        Assert.assertEquals(a.actions.size(), b.actions.size());
        for (int i = 0; i < a.actions.size(); i++) {
            ActionResult x = a.actions.get(i), y = b.actions.get(i);
            Assert.assertEquals(x.action, y.action);
            for (RolloutCounter.Outcome o : RolloutCounter.Outcome.values()) {
                Assert.assertEquals("action " + x.action + " count of " + o, x.result.count(o), y.result.count(o));
            }
        }
        Assert.assertEquals("two placebo groups of k: thresholds and held-out scoring",
                2 * a.actions.size(), a.placeboPasses.size());
        for (int j = 0; j < a.placeboPasses.size(); j++) {
            for (RolloutCounter.Outcome o : RolloutCounter.Outcome.values()) {
                Assert.assertEquals("placebo " + j + " count of " + o,
                        a.placeboPasses.get(j).result.count(o), b.placeboPasses.get(j).result.count(o));
            }
        }
        // independent families: the k placebo counts are not one count repeated
        java.util.Set<String> distinct = new java.util.HashSet<>();
        for (ActionResult x : a.placeboPasses) {
            distinct.add(x.result.count(RolloutCounter.Outcome.WIN) + "/" + x.result.count(RolloutCounter.Outcome.LOSS));
        }
        Assert.assertTrue("placebo passes are all identical: " + distinct, distinct.size() > 1);
    }

    @Test
    public void test_liveGameUntouched() {
        Game position = boltPosition();
        String stateBefore = position.getState().getValue(true, position);
        RandomUtil.setSeed(91);
        int[] expected = new int[40];
        for (int i = 0; i < expected.length; i++) {
            expected[i] = RandomUtil.nextInt(1_000_000);
        }
        RandomUtil.setSeed(91);
        int[] got = new int[40];
        for (int i = 0; i < 20; i++) {
            got[i] = RandomUtil.nextInt(1_000_000);
        }
        ActionSpread.measure(position, playerA.getId(), SEED_BASE, 8, UNCUT_BUDGET_MS, THREADS);
        for (int i = 20; i < got.length; i++) {
            got[i] = RandomUtil.nextInt(1_000_000);
        }
        Assert.assertArrayEquals("the live game's stream moved during measure()", expected, got);
        Assert.assertEquals("the live game's state changed during measure()", stateBefore,
                position.getState().getValue(true, position));
    }

    @Test
    public void test_noChoiceIsNull() {
        // upkeep, empty hand except lands (the test deck is Mountains), nothing to activate: pass only
        setStopAt(2, PhaseStep.UPKEEP);
        execute();
        Spread s = ActionSpread.measure(currentGame, playerB.getId(), SEED_BASE, 8, UNCUT_BUDGET_MS, THREADS);
        Assert.assertNull("only pass is legal in upkeep with nothing to cast", s);
    }

    /**
     * THE PROBE ENTRY MUST NOT HOLD A LOCK. RolloutProbe.probe was static synchronized, and the mad
     * critic's rollout seats (ComputerPlayer7) call it at their own priority: they blocked at the
     * METHOD ENTRY on the monitor the game thread held for the whole measurement, while the game
     * thread waited for those rollouts. Nine jobs hung for two hours with ~3 min of CPU between
     * them (2026-09-22). The isSimulation() refusal only works outside the lock.
     */
    @Test
    public void test_probeEntryIsNotSynchronized() throws Exception {
        java.lang.reflect.Method probe = Class.forName("mage.player.ai.RolloutProbe")
                .getMethod("probe", mage.game.Game.class, java.util.UUID.class, String.class);
        Assert.assertFalse("RolloutProbe.probe must not be synchronized: a rollout seat calls it while "
                        + "the game thread holds the monitor",
                java.lang.reflect.Modifier.isSynchronized(probe.getModifiers()));
    }
}
