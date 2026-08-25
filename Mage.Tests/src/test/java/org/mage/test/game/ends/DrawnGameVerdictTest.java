package org.mage.test.game.ends;

import mage.constants.PhaseStep;
import mage.constants.Zone;
import mage.players.Player;
import org.junit.Assert;
import org.junit.Test;
import org.mage.test.serverside.base.CardTestPlayerBase;

/**
 * The properties ServerGameEventLogCollector reads to report a game's outcome.
 * <p>
 * The collector used to emit game_end from inside end(), which runs DURING the
 * play loop before GameImpl assigns winnerId, so it had no verdict to read and
 * reconstructed one: "if exactly one player has not lost, they won". On a
 * NON-simultaneous double loss that named a player who had also lost -- twice in
 * four captured cases with LESS life than the player it called the loser.
 * <p>
 * The emit moved to after the verdict, and the collector now keys on hasWon() and
 * isADraw(). These assert the two properties that makes correct, on a game the
 * engine really does draw. GameDrawByDamage in this package covers the draw
 * itself; this covers what an observer of the draw is entitled to read.
 */
public class DrawnGameVerdictTest extends CardTestPlayerBase {

    @Test
    public void aDrawnGameLeavesNobodyMarkedAsHavingWon() {
        // Same forced simultaneous loss as GameDrawByDamage: Flame Rift deals 4
        // to each player, and both are taken to 0 in the same state-based pass.
        addCard(Zone.BATTLEFIELD, playerA, "Mountain", 6);
        addCard(Zone.HAND, playerA, "Flame Rift", 3);
        addCard(Zone.BATTLEFIELD, playerB, "Mountain", 4);
        addCard(Zone.HAND, playerB, "Flame Rift", 2);

        castSpell(1, PhaseStep.PRECOMBAT_MAIN, playerA, "Flame Rift", true);
        castSpell(1, PhaseStep.PRECOMBAT_MAIN, playerA, "Flame Rift", true);
        castSpell(1, PhaseStep.PRECOMBAT_MAIN, playerA, "Flame Rift");
        castSpell(2, PhaseStep.PRECOMBAT_MAIN, playerB, "Flame Rift", true);
        castSpell(2, PhaseStep.PRECOMBAT_MAIN, playerB, "Flame Rift");

        setStopAt(2, PhaseStep.BEGIN_COMBAT);
        execute();

        Assert.assertTrue("precondition: the game is a draw", currentGame.isADraw());

        // WHAT THE COLLECTOR NOW WRITES AS `winner`. If any player were marked as
        // having won here, the collector would report a winner for a drawn game
        // again -- by a different route, and just as silently.
        for (Player player : currentGame.getState().getPlayers().values()) {
            Assert.assertFalse(
                    "a drawn game must leave nobody marked as having won, but "
                            + player.getName() + " is", player.hasWon());
        }

        // AND THE RECONSTRUCTION IT REPLACED MUST FIND NOBODY. `survivor` is the
        // old logic under its own name: exactly one player neither lost nor left.
        // In a draw both have lost, so there is no survivor and nothing to
        // mistake for a winner.
        int survivors = 0;
        for (Player player : currentGame.getState().getPlayers().values()) {
            if (!player.hasLost() && !player.hasLeft()) {
                survivors++;
            }
        }
        Assert.assertEquals("a drawn game has no survivor to promote", 0, survivors);
    }

    @Test
    public void aDecidedGameStillMarksTheWinner() {
        // THE CONTROL. Every assertion above is about absence, and absence is
        // also what a game that never finished looks like. This is the same
        // machinery on a game with a real result: exactly one hasWon, not a draw.
        // Seven bolts at {R} each, all in one main phase: 21 damage against 20
        // life. One-mana spells so the whole kill fits in turn 1 and the test
        // does not depend on how many turns the framework will play out.
        addCard(Zone.BATTLEFIELD, playerA, "Mountain", 7);
        addCard(Zone.HAND, playerA, "Lightning Bolt", 7); // {R}, 3 damage
        setLife(playerB, 20);

        for (int i = 0; i < 7; i++) {
            castSpell(1, PhaseStep.PRECOMBAT_MAIN, playerA, "Lightning Bolt", playerB);
        }

        setStopAt(1, PhaseStep.END_TURN);
        execute();

        Assert.assertFalse("this game has a winner, so it is not a draw", currentGame.isADraw());
        int winners = 0;
        for (Player player : currentGame.getState().getPlayers().values()) {
            if (player.hasWon()) {
                winners++;
            }
        }
        Assert.assertEquals("a decided game marks exactly one winner", 1, winners);
    }
}
