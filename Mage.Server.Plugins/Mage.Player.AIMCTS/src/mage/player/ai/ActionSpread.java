package mage.player.ai;

import mage.MageObject;
import mage.abilities.Ability;
import mage.abilities.ActivatedAbility;
import mage.abilities.common.PassAbility;
import mage.game.Game;
import mage.util.RandomUtil;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/**
 * How much does the choice matter here? At a priority position, apply EACH legal action on a copy
 * of the true state and count rollouts from the result (position-environment.md s3, "action
 * spread"). A training-time measurement only.
 * <p>
 * Enumeration and application are MCTS's own: MCTSPlayer.getPlayableOptions (every playable
 * ability, expanded over its targets/modes and variable costs, plus pass), then per action a copy
 * with the ability activated, exactly as PriorityNextAction does. The rollouts from each
 * resulting state share ONE seedBase, so actions are compared under common random numbers:
 * rollout k of every action starts from the same RNG stream.
 * <p>
 * PLACEBO: the pass action is counted 2k more times (k = the number of legal actions), each under
 * its OWN seed family, in TWO groups of k. The first k build the cell thresholds; the second k are
 * SCORED exactly like the real actions (max-min over k values against the threshold), so the
 * held-out exceedance rate is the false-positive rate of the whole procedure, measured on the same
 * run. Both groups hold k values because a max-min over fewer is systematically smaller: splitting
 * one group of k in half would have understated the rate rather than measured it. The max-min of k noisy estimates grows with k, so the registered
 * criterion reads each real spread against the 95th percentile of the placebo spreads at the same
 * k (position-environment.md s3, registered 2026-09-22; the fixed 2 x SD = 0.084 is superseded).
 * The placebo families are independent, so its noise is UNCOUPLED; the real actions share seeds
 * (CRN) and may be coupled, which is the reason the real comparison can beat the placebo.
 * <p>
 * Everything random on the calling thread (enumeration, the activation's cost payment by a random
 * seat) runs under RandomUtil.withThreadSeed, so the live game's stream does not move.
 */
public final class ActionSpread {

    public static final class ActionResult {
        public final int index;
        public final String action;
        public final boolean isPass;
        // false = activateAbility refused on the copy (e.g. the cost could not be paid), so the
        // counts are from an unchanged state and must not be read as this action's value
        public final boolean activated;
        public final RolloutCounter.Result result;

        ActionResult(int index, String action, boolean isPass, boolean activated, RolloutCounter.Result result) {
            this.index = index;
            this.action = action;
            this.isPass = isPass;
            this.activated = activated;
            this.result = result;
        }

        /** (W + D/2) / results; NaN when every rollout was a no-result */
        public double winProb() {
            int res = result.count(RolloutCounter.Outcome.WIN) + result.count(RolloutCounter.Outcome.LOSS)
                    + result.count(RolloutCounter.Outcome.DRAW);
            if (res == 0) {
                return Double.NaN;
            }
            return (result.count(RolloutCounter.Outcome.WIN) + 0.5 * result.count(RolloutCounter.Outcome.DRAW)) / res;
        }
    }

    public static final class Spread {
        public final List<ActionResult> actions;
        public final List<ActionResult> placeboPasses;   // pass, k times, k independent seed families
        public final long wallMillis;

        Spread(List<ActionResult> actions, List<ActionResult> placeboPasses, long wallMillis) {
            this.actions = actions;
            this.placeboPasses = placeboPasses;
            this.wallMillis = wallMillis;
        }
    }

    private ActionSpread() {
    }

    /**
     * How many of these are not mana abilities. The readable cut of a spread drops mana abilities
     * ("{T}: Add {R}"): they are legal actions and MCTS enumerates them, but tapping for mana that
     * is then not spent changes nothing that survives the step, and they were the "best action" in
     * 10 of 24 non-flat positions of the random arm. Typed, not matched on the label.
     */
    public static int nonManaCount(List<Ability> actions) {
        int n = 0;
        for (Ability a : actions) {
            if (!(a instanceof mage.abilities.mana.ManaAbility)) {
                n++;
            }
        }
        return n;
    }

    /** The legal actions at this priority position for the seat, as MCTS enumerates them. */
    public static List<Ability> legalActions(Game enumCopy, UUID seatId, long seed) {
        MCTSPlayer player = (MCTSPlayer) enumCopy.getPlayer(seatId);
        return RandomUtil.withThreadSeed(seed, () -> player.getPlayableOptions(enumCopy));
    }

    /**
     * Returns null when the seat has fewer than two legal actions: there is no choice to measure.
     */
    public static Spread measure(Game live, UUID seatId, long seedBase, int n, long budgetMillis, int threads) {
        return measure(live, seatId, seedBase, n, budgetMillis, threads, RolloutCounter.Critic.RANDOM);
    }

    /**
     * With a chosen critic. Enumeration and application stay on RANDOM seats either way (they
     * need MCTSPlayer); only the rollouts from each resulting state use the critic.
     */
    public static Spread measure(Game live, UUID seatId, long seedBase, int n, long budgetMillis, int threads,
                                 RolloutCounter.Critic critic) {
        long t0 = System.nanoTime();
        Game enumCopy = RolloutCounter.withRandomSeats(live);
        List<Ability> actions = legalActions(enumCopy, seatId, RolloutCounter.mix(seedBase ^ 0x5EEDL));
        if (actions.size() < 2) {
            return null;
        }
        List<ActionResult> out = new ArrayList<>();
        ActionResult firstPass = null;
        for (int i = 0; i < actions.size(); i++) {
            Ability a = actions.get(i);
            boolean[] activated = {false};
            Game after = applied(enumCopy, seatId, a, RolloutCounter.mix(seedBase + 0xAC7L + i), activated);
            RolloutCounter.Result r = RolloutCounter.count(after, seatId, seedBase, n, budgetMillis, threads, critic);
            ActionResult ar = new ActionResult(i, label(a, enumCopy), a instanceof PassAbility, activated[0], r);
            out.add(ar);
            if (ar.isPass && firstPass == null) {
                firstPass = ar;
            }
        }
        if (firstPass == null) {
            throw new IllegalStateException("no pass among the legal actions -- MCTSPlayer.getPlayableAbilities always adds one");
        }
        boolean[] passActivated = {false};
        Game passAgain = applied(enumCopy, seatId, actions.get(firstPass.index),
                RolloutCounter.mix(seedBase + 0xAC7L + firstPass.index), passActivated);
        List<ActionResult> placebos = new ArrayList<>();
        for (int j = 0; j < 2 * actions.size(); j++) {
            RolloutCounter.Result placebo = RolloutCounter.count(passAgain, seatId,
                    RolloutCounter.mix((seedBase ^ 0x91ACEBL) + j), n, budgetMillis, threads, critic);
            placebos.add(new ActionResult(firstPass.index, firstPass.action, true, passActivated[0], placebo));
        }
        return new Spread(out, placebos, (System.nanoTime() - t0) / 1_000_000L);
    }

    // PriorityNextAction's step: copy, activate the ability for the seat. Not resumed here --
    // RolloutCounter.count resumes each rollout from this state.
    private static Game applied(Game enumCopy, UUID seatId, Ability a, long seed, boolean[] activated) {
        Game sim = enumCopy.createSimulationForAI();
        MCTSPlayer seat = (MCTSPlayer) sim.getPlayer(seatId);
        activated[0] = RandomUtil.withThreadSeed(seed, () -> seat.activateAbility((ActivatedAbility) a, sim));
        return sim;
    }

    private static String label(Ability a, Game game) {
        if (a instanceof PassAbility) {
            return "pass";
        }
        MageObject source = game.getObject(a.getSourceId());
        StringBuilder sb = new StringBuilder(source == null ? "?" : source.getName()).append(" :: ").append(a.toString());
        for (mage.target.Target t : a.getTargets()) {
            for (UUID id : t.getTargets()) {
                mage.players.Player p = game.getPlayer(id);
                MageObject o = p == null ? game.getObject(id) : null;
                sb.append(" -> ").append(p != null ? "P:" + p.getName() : o != null ? o.getName() : "?");
            }
        }
        return sb.toString();
    }
}
