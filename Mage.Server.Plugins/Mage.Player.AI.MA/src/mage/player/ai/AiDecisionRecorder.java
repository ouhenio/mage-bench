package mage.player.ai;

import mage.abilities.Ability;
import mage.abilities.ActivatedAbility;
import mage.cards.Card;
import mage.game.Game;
import mage.game.combat.CombatGroup;
import mage.game.permanent.Permanent;
import mage.players.Player;
import org.apache.log4j.Logger;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.List;
import java.util.UUID;

/**
 * Records engine-AI decisions as (state, chosen action, alternatives) triples.
 * <p>
 * Why this exists: the server only fires PlayerQueryEvent when it asks a *client*
 * for input, so ServerGameEventLogCollector never sees an AI seat's decisions. An
 * AI-vs-AI game therefore produces game narration and no labelled decisions at
 * all. The information exists in memory at the moment the bot commits to a move --
 * it was simply never serialised. This writes it out.
 * <p>
 * The output is intended as supervised training data: each line is one decision
 * the bot faced, what it could legally have done, and what it chose.
 * <p>
 * Off unless -Dxmage.ai.recordDir is set, so normal play is unaffected. Failures
 * here must never disturb a game, so every write is best-effort and logged.
 */
public final class AiDecisionRecorder {

    private static final Logger logger = Logger.getLogger(AiDecisionRecorder.class);
    private static final String DIR_PROPERTY = "xmage.ai.recordDir";

    private AiDecisionRecorder() {
    }


    /**
     * Bisect switch for the sub-decision hooks. Set MAGEBENCH_AI_RECORD_SKIP to a
     * comma list of kinds ("choose_target,select_attackers") to disable those hooks
     * while leaving the rest recording. Exists because a hook that merely LOOKS at
     * the game before it decides can still change it, and the only way to find out
     * which one is to turn them off one at a time.
     */
    public static boolean hookEnabled(String kind) {
        String skip = System.getenv("MAGEBENCH_AI_RECORD_SKIP");
        if (skip == null || skip.isEmpty()) {
            return true;
        }
        for (String s : skip.split(",")) {
            if (s.trim().equals(kind)) {
                return false;
            }
        }
        return true;
    }

    public static boolean isEnabled() {
        String dir = System.getProperty(DIR_PROPERTY);
        return dir != null && !dir.isEmpty();
    }

    /**
     * Append one decision. `chosen` is the ability the AI committed to; `options`
     * is what it could legally have played at that moment.
     */
    /**
     * The board state every record shares, up to but not including the decision
     * itself. Two callers append to this: an ability decision from the priority
     * loop, and the sub-decisions (mode, target, attackers, yes/no) that the
     * prompt format also asks for.
     */
    private static StringBuilder header(Game game, Player player) {
        StringBuilder sb = new StringBuilder(512);
            sb.append('{');
            // Wall-clock at the moment of commitment. Consecutive stamps for one
            // seat give think-time per decision, which is the only behavioural
            // evidence that the skill setting actually took effect: maxThinkTimeSecs
            // is skill * 3, so a deeper-searching seat must be measurably slower.
            // Win/loss data cannot distinguish "applied and made no difference"
            // from "silently never applied".
            sb.append("\"ts_ms\":").append(System.currentTimeMillis()).append(',');
            kv(sb, "game_id", game.getId().toString()).append(',');
            kv(sb, "player", player.getName()).append(',');
            sb.append("\"turn\":").append(game.getTurnNum()).append(',');
            kv(sb, "phase", String.valueOf(game.getPhase() == null ? "" : game.getPhase().getType())).append(',');
            kv(sb, "step", String.valueOf(game.getStep() == null ? "" : game.getStep().getType())).append(',');
            kv(sb, "active_player", nameOf(game, game.getActivePlayerId())).append(',');

            sb.append("\"life\":{");
            boolean first = true;
            for (UUID pid : game.getState().getPlayersInRange(player.getId(), game)) {
                Player p = game.getPlayer(pid);
                if (p == null) {
                    continue;
                }
                if (!first) {
                    sb.append(',');
                }
                first = false;
                sb.append('"').append(esc(p.getName())).append("\":").append(p.getLife());
            }
            sb.append("},");

            sb.append("\"hand\":[");
            first = true;
            for (Card c : player.getHand().getCards(game)) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                sb.append('"').append(esc(c.getName())).append('"');
            }
            sb.append("],");

            sb.append("\"battlefield\":[");
            first = true;
            for (Permanent p : game.getBattlefield().getAllActivePermanents()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                // RAW UUID, NEVER A MINTED SHORT ID. An earlier version called the short-id
                // registry's assigning accessor here, reasoning that no renderer runs in an
                // engine-vs-engine game so minting was free. It is not: that call MUTATES
                // the live game. Measured on three paired seeds, recording on vs off, it
                // CHANGED THE GAMES -- one run had Slickshot Show-Off attacking at 3/2
                // where the other had 6/4. The original recorder is byte-identical on the
                // same three seeds, so the perturbation came from here, from a call whose
                // own comment warned against it.
                //
                // A recorder that alters the games it observes is worse than none. UUIDs
                // are stable and strictly more informative; pN aliases are a RENDERING
                // concern and belong to whatever renders the prompt, which can assign them
                // deterministically from encounter order.
                sb.append("{\"id\":\"")
                        .append(esc(p.getId().toString()))
                        .append("\",\"name\":\"").append(esc(p.getName()))
                        .append("\",\"controller\":\"").append(esc(nameOf(game, p.getControllerId())))
                        .append("\",\"tapped\":").append(p.isTapped())
                        .append(",\"power\":").append(p.getPower().getValue())
                        .append(",\"toughness\":").append(p.getToughness().getValue())
                        .append('}');
            }
            sb.append("],");

            // EVERYTHING BELOW EXISTS SO THE PROMPT CAN BE RENDERED FROM THIS RECORD.
            //
            // The seated engine never receives a rendered prompt -- only a client does --
            // so the previous route to training data was to attach a client and relay the
            // engine's advice into it. That relay wins 25% where this seat wins ~70%,
            // because an advisory re-derives per decision while a seated player executes a
            // plan. Recording the seat directly removes the relay, but only if the record
            // carries everything the renderer would have shown. It did not: library and
            // graveyard counts, exile, the opponent's hand size, combat, and the short ids
            // were all absent, and all of them are one call away on the Game already here.
            sb.append("\"zones\":{");
            first = true;
            for (UUID pid : game.getState().getPlayersInRange(player.getId(), game)) {
                Player pl = game.getPlayer(pid);
                if (pl == null) {
                    continue;
                }
                if (!first) {
                    sb.append(',');
                }
                first = false;
                sb.append('"').append(esc(pl.getName())).append("\":{")
                        .append("\"hand_size\":").append(pl.getHand().size())
                        .append(",\"library\":").append(pl.getLibrary().size())
                        .append(",\"graveyard\":[");
                boolean g1 = true;
                for (Card c : pl.getGraveyard().getCards(game)) {
                    if (!g1) {
                        sb.append(',');
                    }
                    g1 = false;
                    sb.append('"').append(esc(c.getName())).append('"');
                }
                sb.append("]}");
            }
            sb.append("},");

            // Combat as attacker -> defender pairs, which is how the prompt renders it.
            sb.append("\"combat\":[");
            if (game.getCombat() != null) {
                first = true;
                for (CombatGroup cg : game.getCombat().getGroups()) {
                    for (UUID aid : cg.getAttackers()) {
                        Permanent att = game.getPermanent(aid);
                        if (att == null) {
                            continue;
                        }
                        if (!first) {
                            sb.append(',');
                        }
                        first = false;
                        sb.append("{\"attacker\":\"").append(esc(att.getName()))
                                .append("\",\"defender\":\"")
                                .append(esc(nameOf(game, cg.getDefenderId())))
                                .append("\"}");
                    }
                }
            }
            sb.append("],");

        return sb;
    }

    public static void record(Game game, Player player, Ability chosen, List<ActivatedAbility> options) {
        if (!isEnabled()) {
            return;
        }
        try {
            StringBuilder sb = header(game, player);
            boolean first;
            // The alternatives are what make this trainable. A chosen action with
            // no record of what else was available teaches nothing about judgement.
            sb.append("\"options\":[");
            if (options != null) {
                first = true;
                for (ActivatedAbility a : options) {
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    // STRUCTURED, not a display string. The renderer needs the source's
                    // alias to emit `Mountain [id=p6, land]`; a string like
                    // "Mountain: Play Mountain" cannot be turned back into one. The
                    // display text is kept alongside so nothing that reads the old shape
                    // breaks.
                    sb.append("{\"id\":\"")
                            .append(esc(a.getSourceId() == null ? "" : a.getSourceId().toString()))
                            .append("\",\"text\":\"").append(esc(describe(game, a)))
                            .append("\",\"mana\":\"").append(esc(a.getManaCosts().getText()))
                            .append("\"}");
                }
            }
            sb.append("],");
            // DO NOT write "pass" for a null action. `chosen == null` means the AI
            // produced no action, and that is true both when it deliberately passed
            // and when its search was cut short by maxThinkTimeSecs or maxNodes.
            // Writing "pass" for both asserts something the recorder does not know.
            //
            // Measured on the 4,315 records this already produced: 3,350 (77.6%) are
            // "pass", and 2,192 (50.8%) are "pass" WITH options available. In
            // aggregate that reads as a cautious teacher, which is exactly how the
            // older recorder came to label 1,073 of 1,295 attack decisions "Pass".
            // A corpus already collected cannot be cleaned of this -- there is no
            // field distinguishing the two -- only regenerated.
            //
            // `null` says "no action", which is all this method can honestly know.
            // Distinguishing deliberate-pass from search-exhausted needs the CALLER:
            // ComputerPlayer6.act() reaches here via `actions.isEmpty()`, and only
            // addActionsTimed()'s `catch (TimeoutException | InterruptedException)`
            // knows which happened. That is a two-file change and the call sites live
            // in uncommitted work in another tree; landing half of it here would
            // conflict with that. See the TODO item.
            sb.append("\"chosen\":");
            if (chosen == null) {
                sb.append("null");
            } else {
                // STRUCTURED, and the id is the point. A deck runs four Mountains, so the
                // display string "Mountain: Play Mountain" appears several times in one
                // option list and cannot say WHICH was taken. The options carry p11/p12;
                // a label that cannot be matched back to one of them is not a label.
                sb.append("{\"id\":\"")
                        .append(esc(chosen.getSourceId() == null ? "" : chosen.getSourceId().toString()))
                        .append("\",\"text\":\"").append(esc(describe(game, chosen)))
                        .append("\"}");
            }
            sb.append("}\n");

            Path out = Paths.get(System.getProperty(DIR_PROPERTY), "ai_decisions.jsonl");
            Files.createDirectories(out.getParent());
            try (BufferedWriter w = Files.newBufferedWriter(
                    out, StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND)) {
                w.write(sb.toString());
            }
        } catch (IOException | RuntimeException e) {
            // Never let data collection break a game.
            logger.warn("AiDecisionRecorder: failed to record decision", e);
        }
    }

    /**
     * Record a decision that is NOT the priority loop's "which ability to play".
     * <p>
     * The prompt format asks for more than actions. Measured over one pilot game's
     * 86 decision points: 73 were priority actions, which {@link #record} already
     * covers, and the remaining 13 were modes, which mana to produce, which spell
     * or ability, attacker declarations and targets. A corpus missing those cannot
     * be replayed against the prompt interface, because the interface will ask
     * questions the corpus has no answer for.
     * <p>
     * BE CLEAR ABOUT WHAT THESE LABELS ARE WORTH. Some of these engine methods do
     * not decide anything: chooseMode takes the first valid mode (its own TODO says
     * so) and chooseUse returns a blanket yes. Recording them is honest -- it is
     * what the engine did -- but a model trained on them learns the engine's
     * placeholder, not skill. They are here for FORMAT COVERAGE. Targets and
     * attackers are genuine searched decisions and are worth training on. Anything
     * consuming this file should treat `kind` as the axis to filter on.
     *
     * @param optionIds   ids parallel to optionTexts; may be empty when the choice
     *                    is over strings (a mana symbol, yes/no) with no game object
     */
    public static void recordChoice(Game game, Player player, String kind, String message,
                                    List<String> optionIds, List<String> optionTexts,
                                    String chosenId, String chosenText) {
        if (!isEnabled()) {
            return;
        }
        try {
            StringBuilder sb = header(game, player);
            kv(sb, "kind", kind).append(',');
            kv(sb, "message", message == null ? "" : message).append(',');
            sb.append("\"options\":[");
            if (optionTexts != null) {
                for (int i = 0; i < optionTexts.size(); i++) {
                    if (i > 0) {
                        sb.append(',');
                    }
                    String id = (optionIds != null && i < optionIds.size() && optionIds.get(i) != null)
                            ? optionIds.get(i) : "";
                    sb.append("{\"id\":\"").append(esc(id))
                            .append("\",\"text\":\"").append(esc(optionTexts.get(i)))
                            .append("\",\"mana\":\"\"}");
                }
            }
            sb.append("],");
            sb.append("\"chosen\":");
            if (chosenText == null) {
                sb.append("null");
            } else {
                sb.append("{\"id\":\"").append(esc(chosenId == null ? "" : chosenId))
                        .append("\",\"text\":\"").append(esc(chosenText))
                        .append("\"}");
            }
            sb.append("}\n");
            flush(sb);
        } catch (IOException | RuntimeException e) {
            logger.warn("AiDecisionRecorder: failed to record choice", e);
        }
    }

    private static void flush(StringBuilder sb) throws IOException {
        Path out = Paths.get(System.getProperty(DIR_PROPERTY), "ai_decisions.jsonl");
        Files.createDirectories(out.getParent());
        try (BufferedWriter w = Files.newBufferedWriter(
                out, StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.APPEND)) {
            w.write(sb.toString());
        }
    }

    private static String describe(Game game, Ability a) {
        if (a == null) {
            return "pass";
        }
        String src = "";
        if (a.getSourceId() != null && game.getObject(a.getSourceId()) != null) {
            src = game.getObject(a.getSourceId()).getName();
        }
        String rule = a.getRule();
        String out = (src.isEmpty() ? "" : src + ": ") + (rule == null ? "" : rule);
        if (out.isEmpty() || out.equals(": ")) {
            // Never emit an empty label. As a training target, "" is
            // indistinguishable from a genuine pass, which silently mislabels the
            // example rather than failing. Fall back to the class name, which is
            // at least honest about being unresolved.
            out = a.toString();
            if (out == null || out.isEmpty()) {
                out = "unknown:" + a.getClass().getSimpleName();
            }
        }
        return out;
    }

    private static String nameOf(Game game, UUID id) {
        if (id == null) {
            return "";
        }
        Player p = game.getPlayer(id);
        return p == null ? "" : p.getName();
    }

    private static StringBuilder kv(StringBuilder sb, String k, String v) {
        return sb.append('"').append(k).append("\":\"").append(esc(v)).append('"');
    }

    private static String esc(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder out = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    out.append("\\\"");
                    break;
                case '\\':
                    out.append("\\\\");
                    break;
                case '\n':
                    out.append("\\n");
                    break;
                case '\r':
                    out.append("\\r");
                    break;
                case '\t':
                    out.append("\\t");
                    break;
                default:
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
            }
        }
        return out.toString();
    }
}
