# deck-audit

Two standalone tools for auditing a deck collection against the engine's own card
database. They are NOT part of the maven build — they are compiled against the built
classes on demand, because they are for answering a question, not for shipping.

## Why they exist

Deciding whether a set of `.dck` files is usable as a training/eval pool needs two
answers that only the engine can give:

- **Does it load?** Parsing a `.dck` is not validation. `DeckImporter` happily builds
  a `DeckCardLists` for a card the database has never heard of; `Deck.load` is where a
  name is actually resolved. `DeckCheck` uses the second, so "loads clean" means the
  engine could really build the deck.
- **What is the card text?** XMage builds rules text at runtime from each card's
  ability objects. There is no card database file to read it out of, so a live `Card`
  is the only source of the text a prompt would show. `CardDump` is that extraction.

## Build and run

    JAVA_HOME=/path/to/jdk-21
    mvn -o -q -pl Mage.Server -am dependency:build-classpath -Dmdep.outputFile=/tmp/cp.txt
    CP="Mage/target/classes:Mage.Sets/target/classes:Mage.Common/target/classes:$(cat /tmp/cp.txt)"

    javac -cp "$CP" -d /tmp/da tools/deck-audit/*.java

    # 1. audit a deck tree; skips Commander/Jumpstart/Momir Basic, keeps maindeck == 60
    java -cp "/tmp/da:$CP" DeckCheck Mage.Client/release/sample-decks /tmp/report.tsv

    # 2. dump oracle text for every card in a list of deck paths
    sed -n '/^== clean deck paths ==$/,$p' /tmp/report.tsv | tail -n +2 > /tmp/clean.txt
    java -cp "/tmp/da:$CP" CardDump /tmp/clean.txt /tmp/pool_cards.jsonl

`CardDump`'s output is the sidecar shape consumed by `pipelines/synth/held_out_cards.py`
in the mtg repo: `name`, `mana_cost`, `is_land`, `power`, `toughness`, `rules[]`,
`second_side`, and `_games` (here: how many decks the card appears in).

## Measured with these, 2026-08-24, on `Mage.Client/release/sample-decks`

    .dck considered (skips applied)   1,163
    maindeck exactly 60               1,058
    loads clean                       1,058
    missing cards                         0
    other errors                          0
    distinct cards                    3,814
    multi-face cards                     30

The 30 multi-face are transform and split only. There are ZERO modal DFCs, because the
collection is mostly 1998-2015 and MDFCs are a 2020 mechanic — a coverage limit of the
pool, not a fault of the loader.
