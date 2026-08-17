package mage.player.ai;

import mage.abilities.Ability;
import mage.abilities.ActivatedAbility;
import mage.cards.Card;
import mage.game.Game;
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

    public static boolean isEnabled() {
        String dir = System.getProperty(DIR_PROPERTY);
        return dir != null && !dir.isEmpty();
    }

    /**
     * Append one decision. `chosen` is the ability the AI committed to; `options`
     * is what it could legally have played at that moment.
     */
    public static void record(Game game, Player player, Ability chosen, List<ActivatedAbility> options) {
        if (!isEnabled()) {
            return;
        }
        try {
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
                sb.append("{\"name\":\"").append(esc(p.getName()))
                        .append("\",\"controller\":\"").append(esc(nameOf(game, p.getControllerId())))
                        .append("\",\"tapped\":").append(p.isTapped())
                        .append(",\"power\":").append(p.getPower().getValue())
                        .append(",\"toughness\":").append(p.getToughness().getValue())
                        .append('}');
            }
            sb.append("],");

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
                    sb.append('"').append(esc(describe(game, a))).append('"');
                }
            }
            sb.append("],");
            kv(sb, "chosen", chosen == null ? "pass" : describe(game, chosen));
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
