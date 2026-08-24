import mage.cards.Card;
import mage.cards.decks.Deck;
import mage.cards.decks.DeckCardInfo;
import mage.cards.decks.DeckCardLists;
import mage.cards.decks.importer.DeckImporter;
import mage.cards.repository.CardInfo;
import mage.cards.repository.CardRepository;

import java.io.PrintWriter;
import java.nio.file.*;
import java.util.*;
import java.util.stream.Collectors;

/** Load every 60-card sample deck through the ENGINE's own importer + Deck.load.
 *
 *  Parsing a .dck is not validation: the importer happily produces a DeckCardLists
 *  for a card the database has never heard of. Deck.load is where a name is
 *  actually resolved, so that is what decides "loads clean" here. */
public class DeckCheck {
    static final Set<String> SKIP = Set.of("Commander", "Jumpstart", "Momir Basic");

    public static void main(String[] args) throws Exception {
        Path root = Paths.get(args[0]);
        PrintWriter out = new PrintWriter(args[1]);

        List<Path> decks = Files.walk(root)
                .filter(p -> p.toString().endsWith(".dck"))
                .filter(p -> {
                    Path rel = root.relativize(p);
                    return rel.getNameCount() < 1 || !SKIP.contains(rel.getName(0).toString());
                })
                .sorted().collect(Collectors.toList());

        int considered = 0, sized60 = 0, clean = 0, missing = 0, other = 0;
        Map<String, Integer> missingCards = new TreeMap<>();
        Set<String> allNames = new TreeSet<>();
        Set<String> multiface = new TreeSet<>();
        List<String> cleanPaths = new ArrayList<>();

        for (Path p : decks) {
            considered++;
            StringBuilder errs = new StringBuilder();
            DeckCardLists lists;
            try {
                lists = DeckImporter.importDeckFromFile(p.toString(), errs, false);
            } catch (RuntimeException e) {
                other++; out.println("IMPORT_THREW\t" + p + "\t" + e); continue;
            }
            if (lists == null) { other++; out.println("IMPORT_NULL\t" + p); continue; }

            int main = lists.getCards().stream().mapToInt(c -> 1).sum();
            if (main != 60) continue;          // not our population
            sized60++;

            try {
                Deck d = Deck.load(lists, false, false);
                clean++; cleanPaths.add(p.toString());
                for (Card c : d.getCards()) {
                    allNames.add(c.getName());
                    CardInfo ci = CardRepository.instance.findCard(c.getName());
                    boolean mf = c.getName().contains("//")
                            || (ci != null && ci.getSecondSideName() != null
                                           && !ci.getSecondSideName().isEmpty());
                    if (mf) multiface.add(c.getName());
                }
            } catch (Exception e) {
                String m = String.valueOf(e.getMessage());
                if (m.contains("Card not found")) {
                    missing++;
                    for (String line : m.split("\n")) {
                        if (line.contains("Card not found")) {
                            missingCards.merge(line.trim(), 1, Integer::sum);
                        }
                    }
                    out.println("MISSING\t" + p + "\t" + m.replace("\n", " | "));
                } else {
                    other++; out.println("OTHER\t" + p + "\t" + m);
                }
            }
        }

        out.println();
        out.printf("considered_dck\t%d%n", considered);
        out.printf("maindeck_exactly_60\t%d%n", sized60);
        out.printf("loads_clean\t%d%n", clean);
        out.printf("missing_cards\t%d%n", missing);
        out.printf("other_errors\t%d%n", other);
        out.printf("distinct_cards_in_clean_set\t%d%n", allNames.size());
        out.printf("multiface_cards\t%d%n", multiface.size());
        out.println();
        out.println("== multiface ==");
        multiface.forEach(out::println);
        out.println("== missing (name -> deck count) ==");
        missingCards.forEach((k, v) -> out.println(v + "\t" + k));
        out.println("== clean deck paths ==");
        cleanPaths.forEach(out::println);
        out.close();
        System.out.printf("considered=%d sized60=%d clean=%d missing=%d other=%d distinct=%d multiface=%d%n",
                considered, sized60, clean, missing, other, allNames.size(), multiface.size());
    }
}
