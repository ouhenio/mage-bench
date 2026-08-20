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
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

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

    /**
     * Cards already written to the sidecar, KEYED BY GAME. This was a single
     * JVM-wide set, which is indistinguishable from per-game right up until a
     * server hosts more than one game -- then game 2 onward gets a cards.jsonl
     * missing every card game 1 already saw, and a short file does not look like
     * an error. Same defect as the game seed, the record directory and
     * xmage.ai.skills: four instances of "one value per JVM" that were only ever
     * exercised one game per JVM.
     * <p>
     * A map rather than "reset when the game id changes": resetting is correct
     * only while games are strictly sequential, and would fail silently the first
     * time two ran in one JVM. Mirrors ServerGameEventLogCollector's
     * Map&lt;UUID, GameEventLogger&gt;, which is keyed this way for the same reason.
     * <p>
     * NOT EVICTED. There is no game-end hook where this sits -- it is driven from
     * the AI's act() -- so an entry per game leaks for the life of the JVM. That
     * is a few thousand strings per game against sessions of tens of games, so it
     * is left deliberately rather than unnoticed.
     */
    private static final Map<UUID, Set<String>> SEEN_CARDS =
            new ConcurrentHashMap<>();

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
     * WHERE THIS GAME'S RECORDS GO. Prefer the GAME's own log directory over the
     * JVM property, because the property carries the same defect the game seed
     * did: it is fine at one game per JVM and wrong the moment a server hosts a
     * sequence. Under sequential batching every game in a session would append to
     * ONE ai_decisions.jsonl. The records carry game_id so they are separable
     * offline, but "separable offline" is exactly the promise the log directory
     * already makes, and a second thing that only looks per-game is not worth
     * shipping.
     * <p>
     * game.getOptions().gameLogDir is per game, set by TableController from
     * MatchOptions, and is what ServerGameEventLogCollector writes
     * server_game_events.jsonl into -- so this lands the decisions NEXT TO the
     * events they belong with, and the join stops needing a game_id lookup.
     * <p>
     * The property stays as the enable switch and as the fallback, so every
     * existing invocation keeps working unchanged.
     */
    private static Path outputDir(Game game) {
        if (game != null && game.getOptions() != null) {
            String perGame = game.getOptions().gameLogDir;
            if (perGame != null && !perGame.isEmpty()) {
                return Paths.get(perGame);
            }
        }
        return Paths.get(System.getProperty(DIR_PROPERTY));
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

            // ID AND NAME, NOT NAME ALONE. An option's id is the source card's uuid,
            // so a hand of bare names cannot be joined to the options -- the chosen
            // action names a card the rendered hand has no id for, and the label
            // points at nothing. Found exactly that way: a converted example answered
            // "p3" while its own hand ran p4..p10.
            sb.append("\"hand\":[");
            first = true;
            for (Card c : player.getHand().getCards(game)) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                sb.append("{\"id\":\"").append(esc(c.getId().toString()))
                        .append("\",\"name\":\"").append(esc(c.getName())).append('"');
                appendRules(sb, c.getRules(game));
                sb.append('}');
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
                        .append(",\"toughness\":").append(p.getToughness().getValue());
                appendRules(sb, p.getRules(game));
                sb.append('}');
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
                    sb.append("{\"id\":\"").append(esc(c.getId().toString()))
                            .append("\",\"name\":\"").append(esc(c.getName())).append("\"}");
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
        record(game, player, chosen, options, null);
    }

    /**
     * @param searchOutcome why the AI's search ended -- "complete", "timeout",
     *                      "error", or null when the caller cannot say. A record
     *                      with no chosen action and searchOutcome="timeout" is
     *                      NOT a pass; it is a decision the teacher never finished
     *                      making, and training on it teaches the clock, not the
     *                      game.
     */
    public static void record(Game game, Player player, Ability chosen,
                              List<ActivatedAbility> options, String searchOutcome) {
        if (!isEnabled()) {
            return;
        }
        try {
            noteCards(game, player);
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
            if (searchOutcome != null) {
                kv(sb, "search", searchOutcome).append(',');
            }
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

            flush(game, sb);
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
            noteCards(game, player);
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
            flush(game, sb);
        } catch (IOException | RuntimeException e) {
            logger.warn("AiDecisionRecorder: failed to record choice", e);
        }
    }

    private static void flush(Game game, StringBuilder sb) throws IOException {
        Path out = outputDir(game).resolve("ai_decisions.jsonl");
        Files.createDirectories(out.getParent());
        try (BufferedWriter w = Files.newBufferedWriter(
                out, StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.APPEND)) {
            w.write(sb.toString());
        }
    }

    /**
     * STATIC CARD DATA, written once per card to a sidecar rather than into every
     * record. The decision records carry card NAMES; the prompt shows oracle text,
     * mana cost and P/T as well. That data is identical every time a card appears,
     * so repeating it per record would multiply the corpus for no information.
     * <p>
     * It has to come from here rather than from a card database: XMage builds rules
     * text at runtime from each card's ability objects, so the text a prompt would
     * show exists only in a live game. The banked pilot logs were the other
     * candidate source and cover 16 distinct cards -- one deck's worth.
     */
    private static void noteCards(Game game, Player player) {
        try {
            Set<String> seen = SEEN_CARDS.computeIfAbsent(
                    game.getId(), id -> ConcurrentHashMap.newKeySet());
            StringBuilder sb = new StringBuilder();
            // CHECK MEMBERSHIP BEFORE BUILDING ANYTHING. getRules(game) constructs
            // the card's rules text from its ability objects on every call, and this
            // runs once per DECISION -- several hundred times a game. Building rules
            // for every card in hand and on the battlefield only to discard them
            // because the card was already recorded is the bulk of that work, and it
            // is pure waste after the first sighting of each card.
            for (Card c : player.getHand().getCards(game)) {
                if (seen.contains(c.getName())) {
                    continue;
                }
                appendCard(sb, seen, c.getName(), c.getRules(game), c.getManaCostSymbols(),
                        c.getPower().toString(), c.getToughness().toString(), c.isLand(game));
            }
            for (Permanent p : game.getBattlefield().getAllActivePermanents()) {
                if (seen.contains(p.getName())) {
                    continue;
                }
                appendCard(sb, seen, p.getName(), p.getRules(game), p.getManaCostSymbols(),
                        p.getPower().toString(), p.getToughness().toString(), p.isLand(game));
            }
            if (sb.length() == 0) {
                return;
            }
            Path out = outputDir(game).resolve("cards.jsonl");
            Files.createDirectories(out.getParent());
            try (BufferedWriter w = Files.newBufferedWriter(
                    out, StandardCharsets.UTF_8, StandardOpenOption.CREATE, StandardOpenOption.APPEND)) {
                w.write(sb.toString());
            }
        } catch (IOException | RuntimeException e) {
            logger.warn("AiDecisionRecorder: failed to note card data", e);
        }
    }

    private static void appendCard(StringBuilder sb, Set<String> seen, String name, List<String> rules,
                                   List<String> cost, String power, String toughness, boolean isLand) {
        if (name == null || name.isEmpty() || !seen.add(name)) {
            return;
        }
        sb.append("{\"name\":\"").append(esc(name)).append("\",\"mana_cost\":\"");
        if (cost != null) {
            for (String m : cost) {
                sb.append(esc(m));
            }
        }
        sb.append("\",\"is_land\":").append(isLand)
          .append(",\"power\":\"").append(esc(power))
          .append("\",\"toughness\":\"").append(esc(toughness))
          .append("\",\"rules\":[");
        if (rules != null) {
            boolean f = true;
            for (String r : rules) {
                if (!f) {
                    sb.append(',');
                }
                f = false;
                sb.append('"').append(esc(r)).append('"');
            }
        }
        sb.append("]}\n");
    }

    /**
     * Rules text for ONE OCCURRENCE, written per decision rather than cached by
     * name. Rules are not static: XMage appends live hints, so the same card reads
     * "ICON_GOODYou control a Mountain or a Plains" in one state and "ICON_BAD..."
     * in another, and a card in hand carries no hint at all where the same card on
     * the battlefield does. Measured against real pilot prompts on a shared deck,
     * a by-name cache got 10 of 13 cards byte-identical and the 3 misses were all
     * this: a hint frozen at first sighting and stamped onto every later decision,
     * once with the truth value inverted. A cache keyed by name cannot be correct
     * here, so there is no cache.
     */
    private static void appendRules(StringBuilder sb, List<String> rules) {
        sb.append(",\"rules\":[");
        if (rules != null) {
            boolean f = true;
            for (String r : rules) {
                if (!f) {
                    sb.append(',');
                }
                f = false;
                sb.append('"').append(esc(r)).append('"');
            }
        }
        sb.append(']');
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
