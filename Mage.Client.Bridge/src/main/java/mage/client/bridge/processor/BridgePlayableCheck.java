package mage.client.bridge.processor;

import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.function.Predicate;

import mage.view.GameView;
import mage.players.PlayableObjectStats;
import mage.players.PlayableObjectsList;

/**
 * Whether the seated player has anything real to play right now.
 *
 * ONE IMPLEMENTATION, TWO CALLERS, deliberately. This logic lived as a private method on
 * BridgePassPriorityFlow and was reachable only from the pass_priority path, so a frame's
 * `has_playable_cards` depended on WHICH TOOL the caller happened to invoke rather than on
 * the decision being asked about. Measured 2026-09-04 over 8 games: the flag appeared on 96
 * frames, all of them pass_priority results, and on ZERO of 338 GAME_SELECT/boolean priority
 * windows. Copying the check into the query builder instead of extracting it would be two
 * implementations of one predicate, which is LEDGER 43: a comparator that scanned two arms
 * differently and manufactured part of the gap it reported.
 *
 * `failedManaCast` is passed in rather than reached for, because the two callers hold that
 * state in different objects (a flow context and a projection-inputs record) and neither is
 * visible from the other.
 */
public final class BridgePlayableCheck {

    private BridgePlayableCheck() {
    }

    /**
     * True when at least one object can be played for something other than mana.
     *
     * A land that can only tap for mana is not "something to do": counting it would mark
     * every priority window playable and defeat the point. A card whose mana cast already
     * failed this turn is likewise excluded -- the engine still lists it as castable.
     */
    public static boolean hasPlayableCards(GameView gameView, Predicate<UUID> failedManaCast) {
        PlayableObjectsList playable = gameView != null ? gameView.getCanPlayObjects() : null;
        if (playable == null || playable.isEmpty()) {
            return false;
        }
        for (Map.Entry<UUID, PlayableObjectStats> entry : playable.getObjects().entrySet()) {
            if (failedManaCast != null && failedManaCast.test(entry.getKey())) {
                continue;
            }
            PlayableObjectStats stats = entry.getValue();
            List<String> abilityNames = stats.getPlayableAbilityNames();
            List<String> manaNames = stats.getAllManaAbilityNames();
            boolean allMana = !abilityNames.isEmpty() && manaNames.size() == abilityNames.size();
            if (!allMana) {
                return true;
            }
        }
        return false;
    }
}
