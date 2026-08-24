import mage.cards.Card;
import mage.cards.decks.Deck;
import mage.cards.decks.DeckCardLists;
import mage.cards.decks.importer.DeckImporter;
import mage.cards.repository.CardInfo;
import mage.cards.repository.CardRepository;
import mage.constants.CardType;

import java.io.PrintWriter;
import java.nio.file.*;
import java.util.*;

/** Dump every card in the loadable deck pool in the sidecar shape that
 *  pipelines/synth/held_out_cards.py consumes: name / mana_cost / is_land /
 *  power / toughness / rules[] / _games.
 *
 *  Rules text comes from the live Card object, exactly as AiDecisionRecorder's
 *  sidecar does -- XMage builds rules at runtime from ability objects, so this is
 *  the only source of the text a prompt would show. _games carries the number of
 *  DECKS the card appears in, which is the frequency axis the rule's control needs. */
public class CardDump {
    static String esc(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"")
                .replace("\n", " ").replace("\r", " ").replace("\t", " ");
    }

    public static void main(String[] args) throws Exception {
        List<String> paths = Files.readAllLines(Paths.get(args[0]));
        Map<String, Card> byName = new TreeMap<>();
        Map<String, Integer> deckCount = new TreeMap<>();

        for (String p : paths) {
            if (p.isBlank()) continue;
            DeckCardLists lists = DeckImporter.importDeckFromFile(p, new StringBuilder(), false);
            if (lists == null) continue;
            Deck d;
            try { d = Deck.load(lists, false, false); } catch (Exception e) { continue; }
            Set<String> here = new HashSet<>();
            for (Card c : d.getCards()) { byName.putIfAbsent(c.getName(), c); here.add(c.getName()); }
            for (String n : here) deckCount.merge(n, 1, Integer::sum);
        }

        PrintWriter out = new PrintWriter(args[1]);
        for (Map.Entry<String, Card> e : byName.entrySet()) {
            Card c = e.getValue();
            boolean isLand = c.getCardType().contains(CardType.LAND);
            StringBuilder sb = new StringBuilder();
            sb.append("{\"name\":\"").append(esc(c.getName())).append("\",");
            sb.append("\"mana_cost\":\"").append(esc(String.join("", c.getManaCostSymbols()))).append("\",");
            sb.append("\"is_land\":").append(isLand).append(",");
            sb.append("\"power\":\"").append(esc(c.getPower().toString())).append("\",");
            sb.append("\"toughness\":\"").append(esc(c.getToughness().toString())).append("\",");
            CardInfo ci = CardRepository.instance.findCard(c.getName());
            String second = ci == null ? null : ci.getSecondSideName();
            sb.append("\"second_side\":").append(second == null || second.isEmpty()
                    ? "null" : "\"" + esc(second) + "\"").append(",");
            sb.append("\"_games\":").append(deckCount.getOrDefault(e.getKey(), 0)).append(",");
            sb.append("\"rules\":[");
            List<String> rules = c.getRules();
            for (int i = 0; i < rules.size(); i++) {
                if (i > 0) sb.append(',');
                sb.append('"').append(esc(rules.get(i))).append('"');
            }
            sb.append("]}");
            out.println(sb);
        }
        out.close();
        System.out.println("cards=" + byName.size());
    }
}
