package mage.player.ai;

import mage.MageObject;
import mage.abilities.Ability;
import mage.abilities.ActivatedAbility;
import mage.abilities.SpellAbility;
import mage.cards.Card;
import mage.abilities.Modes;
import mage.abilities.Mode;
import mage.abilities.StaticAbility;
import mage.abilities.common.PassAbility;
import mage.abilities.effects.Effect;
import mage.abilities.effects.SearchEffect;
import mage.abilities.keyword.*;
import mage.cards.Cards;
import mage.choices.Choice;
import mage.constants.Outcome;
import mage.constants.RangeOfInfluence;
import mage.counters.CounterType;
import mage.filter.StaticFilters;
import mage.filter.common.FilterLandCard;
import mage.game.Game;
import mage.game.combat.Combat;
import mage.game.combat.CombatGroup;
import mage.game.events.GameEvent;
import mage.game.permanent.Permanent;
import mage.game.stack.StackAbility;
import mage.game.stack.StackObject;
import mage.player.ai.ma.optimizers.TreeOptimizer;
import mage.player.ai.ma.optimizers.impl.*;
import mage.player.ai.score.GameStateEvaluator2;
import mage.player.ai.util.CombatInfo;
import mage.player.ai.util.CombatUtil;
import mage.players.Player;
import mage.target.Target;
import mage.target.TargetAmount;
import mage.target.TargetCard;
import mage.util.CardUtil;
import mage.util.RandomUtil;
import mage.util.ThreadUtils;
import mage.util.XmageThreadFactory;
import org.apache.log4j.Logger;

import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Collectors;

/**
 * AI: server side bot with game simulations (mad bot, part of implementation)
 *
 * @author nantuko, JayDi85
 */
public class ComputerPlayer6 extends ComputerPlayer {

    private static final Logger logger = Logger.getLogger(ComputerPlayer6.class);

    // TODO: add and research maxNodes logs, is it good to increase from 5000 to 50000 for better results?
    // TODO: increase maxNodes due AI skill level like max depth?
    private static final int MAX_SIMULATED_NODES_PER_CALC = 5000;
    private static final int MAX_SIMULATED_NODES_PER_ERROR = 5100; // TODO: debug only, set low value to find big calculations

    // same params as Executors.newFixedThreadPool
    // no needs errors check in afterExecute here cause that pool used for FutureTask with result check already
    private static final ExecutorService threadPoolSimulations = new ThreadPoolExecutor(
            COMPUTER_MAX_THREADS_FOR_SIMULATIONS,
            COMPUTER_MAX_THREADS_FOR_SIMULATIONS,
            0L,
            TimeUnit.MILLISECONDS,
            new LinkedBlockingQueue<>(),
            new XmageThreadFactory(ThreadUtils.THREAD_PREFIX_AI_SIMULATION_MAD)
    );
    protected int maxDepth;
    protected int maxNodes;
    protected int maxNodesError;
    protected int maxThinkTimeSecs;
    protected LinkedList<Ability> actions = new LinkedList<>();
    protected List<UUID> targets = new ArrayList<>();
    protected List<String> choices = new ArrayList<>();
    protected Combat combat;
    protected int currentScore;
    protected SimulationNode2 root;
    List<Permanent> attackersList = new ArrayList<>();
    List<Permanent> attackersToCheck = new ArrayList<>();

    protected Set<String> actionCache;
    private static final List<TreeOptimizer> optimizers = new ArrayList<>();
    protected int lastLoggedTurn = 0; // for debug logs: mark start of the turn
    protected static final String BLANKS = "...............................................";

    static {
        optimizers.add(new WrongCodeUsageOptimizer());
        optimizers.add(new LevelUpOptimizer());
        optimizers.add(new EquipOptimizer());
        optimizers.add(new DiscardCardOptimizer());
        optimizers.add(new OutcomeOptimizer());
    }

    public ComputerPlayer6(String name, RangeOfInfluence range, int skill) {
        super(name, range);
        if (skill < 4) {
            maxDepth = 4; // TODO: can be increased to support better calculations? (example = 8, skill * 2)
        } else {
            maxDepth = skill;
        }
        // WALL-CLOCK BUDGET, overridable per skill value: -Dxmage.ai.time.1=2
        //
        // Same shape and the same reason as the node budget below, and it is the
        // knob that actually binds. Measured over 10 corpus games, 2,984 recorded
        // decisions: the MEDIAN decision costs 20 ms, and both seats' maxima land
        // exactly on their caps -- 3.10s against skill 1's 3s and 24.07s against
        // skill 8's 24s. So the expensive decisions are ones that ran out of TIME,
        // not ones that exhausted maxNodes, and 11 of skill 8's 1,503 decisions
        // carried 46.6% of its total think time.
        //
        // Default is unchanged, so a JVM that does not set the property behaves
        // exactly as before. Keyed by skill number rather than join order for the
        // same reason as the node budget: a JVM-wide list indexed by seat can be
        // mis-assigned and this cannot.
        maxThinkTimeSecs = Integer.getInteger("xmage.ai.time." + skill, skill * 3);
        // Scale the node budget with skill, per the TODO above the constant.
        // Measured before this change: skill 8 and skill 1 were indistinguishable
        // over 16 games (8-8) with no latency separation, because maxDepth and
        // maxThinkTimeSecs are both non-binding -- the timeout only fires on
        // overrun, and the search aborts at maxNodes long before depth 8 matters.
        // Every skill level therefore explored the same 5000-node tree. Skill 1
        // keeps the historical budget exactly, so it remains a valid control.
        // NODE BUDGET, overridable PER SKILL VALUE: -Dxmage.ai.nodes.1=250
        // Keyed by the skill number rather than by join order, so it cannot be
        // mis-assigned the way a JVM-wide comma list indexed by seat would be.
        // Default keeps the scaled budget; a search budget is the only knob that
        // actually changes how well this AI plays -- measured 45% -> 57% for the
        // strong seat in mirrors when its budget stopped self-aborting.
        maxNodes = Integer.getInteger("xmage.ai.nodes." + skill,
                MAX_SIMULATED_NODES_PER_CALC * Math.max(1, skill));
        // THE ERROR GUARD HAS TO TRACK THE BUDGET, or scaling the budget does
        // nothing. MAX_SIMULATED_NODES_PER_ERROR is 5100 -- a debug tripwire for
        // runaway trees, per its own comment -- and it is a THROW, not a stop. A
        // skill-8 seat given 40,000 nodes hits 5,100 first and its search dies with
        // "AI ERROR: too much nodes"; the exception unwinds to addActionsTimed's
        // ExecutionException handler and the AI takes no action at all.
        // Measured over the 500-game corpus: 31 search errors, ALL of them on the
        // skill-8 seat, 0 on skill-1. That is the scaling defeating itself.
        // maxNodes + 100 keeps skill 1 at exactly 5100, the historical constant, so
        // it stays bit-identical and remains a valid control.
        maxNodesError = maxNodes + 100;
        this.actionCache = new HashSet<>();
    }

    public ComputerPlayer6(final ComputerPlayer6 player) {
        super(player);
        this.maxDepth = player.maxDepth;
        this.maxNodesError = player.maxNodesError;
        this.currentScore = player.currentScore;
        if (player.combat != null) {
            this.combat = player.combat.copy();
        }
        this.actions.addAll(player.actions);
        this.targets.addAll(player.targets);
        this.choices.addAll(player.choices);
        this.actionCache = player.actionCache;
    }

    /**
     * Change simulation timeout - used for AI stability tests only
     */
    public void setMaxThinkTimeSecs(int maxThinkTimeSecs) {
        this.maxThinkTimeSecs = maxThinkTimeSecs;
    }

    @Override
    public ComputerPlayer6 copy() {
        return new ComputerPlayer6(this);
    }

    protected void printBattlefieldScore(Game game, String info) {
        if (logger.isInfoEnabled()) {
            logger.info("");
            logger.info("=================== " + info + ", turn " + game.getTurnNum() + ", " + game.getPlayer(game.getPriorityPlayerId()).getName() + " ===================");
            logger.info("[Stack]: " + game.getStack());
            printBattlefieldScore(game, playerId);
            for (UUID opponentId : game.getOpponents(playerId)) {
                printBattlefieldScore(game, opponentId);
            }
        }
    }

    protected void printBattlefieldScore(Game game, UUID playerId) {
        // hand
        Player player = game.getPlayer(playerId);
        GameStateEvaluator2.PlayerEvaluateScore score = GameStateEvaluator2.evaluate(playerId, game);
        logger.info(new StringBuilder("[").append(game.getPlayer(playerId).getName()).append("]")
                .append(", life = ").append(player.getLife())
                .append(", score = ").append(score.getTotalScore())
                .append(" (").append(score.getPlayerInfoFull()).append(")")
                .toString());
        String cardsInfo = player.getHand().getCards(game).stream()
                .map(card -> card.getName() + ":" + GameStateEvaluator2.HAND_CARD_SCORE) // TODO: add card score here after implement
                .collect(Collectors.joining("; "));
        StringBuilder sb = new StringBuilder("-> Hand: [")
                .append(cardsInfo)
                .append("]");
        logger.info(sb.toString());

        // battlefield
        sb.setLength(0);
        String ownPermanentsInfo = game.getBattlefield().getAllPermanents().stream()
                .filter(p -> p.isOwnedBy(player.getId()))
                .map(p -> p.getName()
                        + (p.isTapped() ? ",tapped" : "")
                        + (p.isAttacking() ? ",attacking" : "")
                        + (p.getBlocking() > 0 ? ",blocking" : "")
                        + ":" + GameStateEvaluator2.evaluatePermanent(p, game, true))
                .collect(Collectors.joining("; "));
        sb.append("-> Permanents: [").append(ownPermanentsInfo).append("]");
        logger.info(sb.toString());
    }

    protected void act(Game game) {
        // Capture the legal options BEFORE acting: once an ability resolves the
        // set of alternatives is gone, and alternatives are what make the record
        // trainable. Only computed when recording is switched on -- getPlayable is
        // not free and this runs inside the AI's hot path.
        List<ActivatedAbility> recordedOptions = AiDecisionRecorder.isEnabled()
                ? getPlayable(game, true)
                : null;
        // CONSUME the search outcome. act() also runs when no search happened at
        // all (getNextAction short-circuits calculateActions), and a flag left set
        // from the previous decision would be attributed to this one -- a stale
        // "timeout" pinned on a deliberate pass is worse than no field. Reading it
        // once and clearing means the absence of a search reads as "none".
        String searchOutcome = lastSearchOutcome.remove(game.getId());
        if (searchOutcome == null) {
            searchOutcome = "none";
        }
        if (actions == null
                || actions.isEmpty()) {
            // WHICH of the two, not just that there was no action. "unset" means no
            // search ever populated the field; "empty" means a search ran and returned
            // nothing. Collapsed together they read as one event and cannot be told
            // apart afterwards.
            String noActionReason = (actions == null) ? "actions_unset" : "actions_empty";
            AiDecisionRecorder.record(game, this, null, recordedOptions, searchOutcome, noActionReason);
            pass(game);
        } else {
            boolean usedStack = false;
            while (actions.peek() != null) {
                Ability ability = actions.poll();
                AiDecisionRecorder.record(game, this, ability, recordedOptions, searchOutcome);
                // example: ===> SELECTED ACTION for PlayerA: Play Swamp
                logger.info(String.format("===> SELECTED ACTION for %s: %s",
                        getName(),
                        getAbilityAndSourceInfo(game, ability, true)
                ));
                if (!ability.getTargets().isEmpty()) {
                    for (Target target : ability.getTargets()) {
                        for (UUID id : target.getTargets()) {
                            target.updateTarget(id, game);
                            if (!target.isNotTarget()) {
                                game.addSimultaneousEvent(GameEvent.getEvent(GameEvent.EventType.TARGETED, id, ability, ability.getControllerId()));
                            }
                        }
                    }
                }
                this.activateAbility((ActivatedAbility) ability, game);
                if (ability.isUsesStack()) {
                    usedStack = true;
                }
            }
            if (usedStack) {
                pass(game);
            }
        }
    }

    protected int addActions(SimulationNode2 node, int depth, int alpha, int beta) {
        boolean stepFinished = false;
        int val;
        if (logger.isTraceEnabled()
                && node != null
                && node.getAbilities() != null
                && !node.getAbilities().toString().equals("[Pass]")) {
            logger.trace("Add Action [" + depth + "] " + node.getAbilities().toString() + "  a: " + alpha + " b: " + beta);
        }
        Game game = node.getGame();
        if (!COMPUTER_DISABLE_TIMEOUT_IN_GAME_SIMULATIONS && Thread.currentThread().isInterrupted()) {
            logger.debug("AI game sim interrupted by timeout");
            return GameStateEvaluator2.evaluate(playerId, game).getTotalScore();
        }
        // Condition to stop deeper simulation
        if (SimulationNode2.nodeCount > maxNodesError) {
            // how-to fix: make sure you are disabled debug mode by COMPUTER_DISABLE_TIMEOUT_IN_GAME_SIMULATIONS = false
            throw new IllegalStateException("AI ERROR: too much nodes (possible actions)");
        }
        if (depth <= 0
                || SimulationNode2.nodeCount > maxNodes
                || game.checkIfGameIsOver()) {
            val = GameStateEvaluator2.evaluate(playerId, game).getTotalScore();
            if (logger.isTraceEnabled()) {
                StringBuilder sb = new StringBuilder("Add Actions -- reached end state  <").append(val).append('>');
                SimulationNode2 logNode = node;
                do {
                    sb.append(new StringBuilder(" <- [" + logNode.getDepth() + ']' + (logNode.getAbilities() != null ? logNode.getAbilities().toString() : "[empty]")));
                    logNode = logNode.getParent();
                } while ((logNode.getParent() != null));
                logger.trace(sb);
            }
        } else if (!node.getChildren().isEmpty()) {
            if (logger.isDebugEnabled()) {
                StringBuilder sb = new StringBuilder("Add Action [").append(depth)
                        .append("] -- something added children ")
                        .append(node.getAbilities() != null ? node.getAbilities().toString() : "null")
                        .append(" added children: ").append(node.getChildren().size()).append(" (");
                for (SimulationNode2 logNode : node.getChildren()) {
                    sb.append(logNode.getAbilities() != null ? logNode.getAbilities().toString() : "null").append(", ");
                }
                sb.append(')');
                logger.debug(sb);
            }
            val = minimaxAB(node, depth - 1, alpha, beta);
        } else {
            logger.trace("Add Action -- alpha: " + alpha + " beta: " + beta + " depth:" + depth + " step:" + game.getTurnStepType() + " for player:" + game.getPlayer(game.getActivePlayerId()).getName());
            if (allPassed(game)) {
                if (!game.getStack().isEmpty()) {
                    resolve(node, depth, game);
                } else {
                    stepFinished = true;
                }
            }

            if (game.checkIfGameIsOver()) {
                val = GameStateEvaluator2.evaluate(playerId, game).getTotalScore();
            } else if (stepFinished) {
                logger.debug("Step finished");
                int testScore = GameStateEvaluator2.evaluate(playerId, game).getTotalScore();
                if (game.isActivePlayer(playerId)) {
                    if (testScore < currentScore) {
                        // if score at end of step is worse than original score don't check further
                        //logger.debug("Add Action -- abandoning check, no immediate benefit");
                        val = testScore;
                    } else {
                        val = GameStateEvaluator2.evaluate(playerId, game).getTotalScore();
                    }
                } else {
                    val = GameStateEvaluator2.evaluate(playerId, game).getTotalScore();
                }
            } else if (!node.getChildren().isEmpty()) {
                if (logger.isDebugEnabled()) {
                    StringBuilder sb = new StringBuilder("Add Action [").append(depth)
                            .append("] -- trigger ")
                            .append(node.getAbilities() != null ? node.getAbilities().toString() : "null")
                            .append(" added children: ").append(node.getChildren().size()).append(" (");
                    for (SimulationNode2 logNode : node.getChildren()) {
                        sb.append(logNode.getAbilities() != null ? logNode.getAbilities().toString() : "null").append(", ");
                    }
                    sb.append(')');
                    logger.debug(sb);
                }
                val = minimaxAB(node, depth - 1, alpha, beta);
            } else {
                val = simulatePriority(node, game, depth, alpha, beta);
            }
        }
        node.setScore(val);
        logger.trace("returning -- score: " + val + " depth:" + depth + " step:" + game.getTurnStepType() + " for player:" + game.getPlayer(node.getPlayerId()).getName());
        return val;

    }

    protected boolean getNextAction(Game game) {
        if (root != null
                && !root.children.isEmpty()) {
            SimulationNode2 test = root;
            root = root.children.get(0);
            while (!root.children.isEmpty()
                    && !root.playerId.equals(playerId)) {
                test = root;
                root = root.children.get(0);
            }
            logger.trace("Sim getNextAction -- game value:" + game.getState().getValue(true) + " test value:" + test.gameValue);
            if (root.playerId.equals(playerId)
                    && root.abilities != null
                    && game.getState().getValue(true).hashCode() == test.gameValue) {
                logger.info("simulating -- continuing previous actions chain");
                actions = new LinkedList<>(root.abilities);
                combat = root.combat;
                return true;
            } else {
                if (root.abilities == null || root.abilities.isEmpty()) {
                    logger.info("simulating -- need re-calculation (no more actions)");
                } else if (game.getState().getValue(true).hashCode() != test.gameValue) {
                    logger.info("simulating -- need re-calculation (game state changed between actions)");
                } else if (!root.playerId.equals(playerId)) {
                    // TODO: need research, why need playerId and why it taken from stack objects as controller
                    logger.info("simulating -- need re-calculation (active controller changed)");
                } else {
                    logger.info("simulating -- need re-calculation (unknown reason)");
                }
                return false;
            }
        }
        return false;
    }

    protected int minimaxAB(SimulationNode2 node, int depth, int alpha, int beta) {
        logger.trace("Sim minimaxAB [" + depth + "] -- a: " + alpha + " b: " + beta + " <" + (node != null ? node.getScore() : "null") + '>');
        UUID currentPlayerId = node.getGame().getPlayerList().get();
        SimulationNode2 bestChild = null;
        for (SimulationNode2 child : node.getChildren()) {
            Combat _combat = child.getCombat();
            if (alpha >= beta) {
                break;
            }
            if (SimulationNode2.nodeCount > maxNodesError) {
                throw new IllegalStateException("AI ERROR: too much nodes (possible actions)");
            }
            if (SimulationNode2.nodeCount > maxNodes) {
                break;
            }
            int val = addActions(child, depth - 1, alpha, beta);
            if (!currentPlayerId.equals(playerId)) {
                if (val < beta) {
                    beta = val;
                    bestChild = child;
                    if (node.getCombat() == null) {
                        node.setCombat(_combat);
                        bestChild.setCombat(_combat);
                    }
                }
                // no need to check other actions
                if (val == GameStateEvaluator2.LOSE_GAME_SCORE) {
                    logger.debug("lose - break");
                    break;
                }
            } else {
                if (val > alpha) {
                    alpha = val;
                    bestChild = child;
                    if (node.getCombat() == null) {
                        node.setCombat(_combat);
                        bestChild.setCombat(_combat);
                    }
                }
                // no need to check other actions
                if (val == GameStateEvaluator2.WIN_GAME_SCORE) {
                    logger.debug("win - break");
                    break;
                }
            }
        }
        node.children.clear();
        if (bestChild != null) {
            node.children.add(bestChild);
        }
        if (!currentPlayerId.equals(playerId)) {
            return beta;
        } else {
            return alpha;
        }
    }

    protected SearchEffect getSearchEffect(StackAbility ability) {
        for (Effect effect : ability.getEffects()) {
            if (effect instanceof SearchEffect) {
                return (SearchEffect) effect;
            }
        }
        return null;
    }

    protected void resolve(SimulationNode2 node, int depth, Game game) {
        StackObject stackObject = game.getStack().getFirstOrNull();
        if (stackObject == null) {
            throw new IllegalStateException("Catch empty stack on resolve (something wrong with sim code)");
        }
        if (stackObject instanceof StackAbility) {
            // AI hint for search effects (calc all possible cards for best score)
            SearchEffect effect = getSearchEffect((StackAbility) stackObject);
            if (effect != null
                    && stackObject.getControllerId().equals(playerId)) {
                Target target = effect.getTarget();
                if (!target.isChoiceCompleted(getId(), (StackAbility) stackObject, game, null)) {
                    for (UUID targetId : target.possibleTargets(stackObject.getControllerId(), stackObject.getStackAbility(), game)) {
                        Game sim = game.createSimulationForAI();
                        StackAbility newAbility = (StackAbility) stackObject.copy();
                        SearchEffect newEffect = getSearchEffect(newAbility);
                        newEffect.getTarget().addTarget(targetId, newAbility, sim);
                        sim.getStack().push(sim, newAbility);
                        SimulationNode2 newNode = new SimulationNode2(node, sim, depth, stackObject.getControllerId());
                        node.children.add(newNode);
                        newNode.getTargets().add(targetId);
                        logger.trace("Sim search -- node#: " + SimulationNode2.getCount() + " for player: " + sim.getPlayer(stackObject.getControllerId()).getName());
                    }
                    return;
                }
            }
        }
        stackObject.resolve(game);
        if (stackObject instanceof StackAbility) {
            game.getStack().remove(stackObject, game);
        }
        game.applyEffects();
        game.getPlayers().resetPassed();
        game.getPlayerList().setCurrent(game.getActivePlayerId());
    }

    /**
     * Base call for simulation of AI actions
     *
     * @return
     */
    /**
     * WHY THE LAST SEARCH ENDED. act() reaches the recorder with no action both
     * when the AI deliberately passes and when maxThinkTimeSecs cut the search
     * short, and those were being written as the same label. Measured on 505
     * priority records: 20% were "no action WITH options available", provenance
     * unknown -- a fifth of the corpus that might be teaching a model to pass
     * when the teacher merely ran out of clock. Only this method knows which
     * happened, so it records it and act() reads it.
     */
    /**
     * KEYED BY GAME, not a bare field. This is per-instance state, and it is only
     * safe as a bare field if a ComputerPlayer6 is constructed fresh for every
     * game -- otherwise one game's search verdict is read by the next. The code
     * reads as fresh construction (each game builds a new table and players join
     * it), and I tried to settle it by measurement rather than argument: a probe
     * logging the instance id per game. It emitted NOTHING, because this class's
     * logger.info does not reach server.log at all -- the pre-existing "SELECTED
     * ACTION" line in this same method is equally absent. An instrument that
     * cannot report is worse than none, and I would have read its silence as
     * "no reuse".
     * <p>
     * So the question is made moot instead of answered. A map is correct whether
     * or not instances are reused, and whether or not games are sequential.
     */
    private final Map<UUID, String> lastSearchOutcome = new ConcurrentHashMap<>();

    /**
     * DETERMINISTIC TIE-BREAK, OFF BY DEFAULT.
     *
     * The root comparison below breaks equal-score ties with a coin -- see the
     * `finalScore == alpha && tiebreakCoin()` disjunct and its original comment,
     * "Adding random for equal value to get change sometimes". That coin came from
     * RandomUtil's process-global Random, and the search drawing it runs on the
     * shared static threadPoolSimulations, so the draw ORDER interleaves with every
     * other consumer in the JVM. Seeding the game does not fix that: setSeed fixes
     * which values the stream yields, not which thread takes which one.
     *
     * Measured on 40 replicate pairs (seeds 940301-940340): replaying a seed
     * reproduces most decisions and diverges on ~1%, and the divergences land
     * exactly where this predicts. Six of twelve differing plays were land-vs-land
     * at equal value, two of those fetchland-vs-fetchland. Seven more pairs
     * differed over whether ANY action existed -- PASSIVITY_PENALTY (see below)
     * manufactures Pass ties constantly, so the coin decides whether bestNode is
     * assigned at all. Both sides recorded searchOutcome="complete"; the coin is
     * inside a completed search, which is why the search budget was never the
     * explanation.
     *
     * With -Dxmage.ai.deterministicTiebreak=true the coin instead comes from a
     * Random owned by THIS player, re-seeded at the start of every search from
     * (game seed, search ordinal). The draws inside one search are single-threaded,
     * so they cannot interleave, and no other RandomUtil consumer changes at all.
     * Variety across seeds is kept; reproducibility within a seed is gained.
     *
     * Default OFF deliberately: this changes AI play, so every corpus already
     * measured keeps its meaning and stays comparable to itself.
     */
    private static final boolean DETERMINISTIC_TIEBREAK
            = Boolean.getBoolean("xmage.ai.deterministicTiebreak");
    private Random tiebreakRandom;
    /**
     * Search ordinal PER GAME, not per player instance. The comment on
     * lastSearchOutcome above notes that AI instances may be reused and games may
     * run sequentially in one JVM, and this counter has to survive both: the
     * sequential runner plays a seed's two replicates back to back in ONE server,
     * so a per-instance counter would hand replicate 1 the ordinals 0..N and
     * replicate 2 the ordinals N+1..2N. Same seed, different draws, and the flag
     * would silently fail to make the two replicates agree -- which is exactly the
     * result it produced before this was keyed by game.
     */
    private final Map<UUID, Integer> tiebreakOrdinal = new ConcurrentHashMap<>();

    /**
     * Re-seed the tie-break coin for one search. Called once per search, from the
     * thread that owns the search, before any root comparison can run.
     */
    private void beginTiebreakSequence(Game searchGame) {
        if (!DETERMINISTIC_TIEBREAK) {
            return;
        }
        Long seed = searchGame.getOptions().gameSeed;
        if (seed == null) {
            String property = System.getProperty("xmage.game.seed");
            if (property != null && !property.trim().isEmpty()) {
                // Same two sources, same order, as GameImpl.resolveGameSeed().
                seed = Long.parseLong(property.trim());
            }
        }
        if (seed == null) {
            // LOUD. Falling back to the shared RNG here would run exactly the
            // nondeterminism this flag exists to remove, while the operator believes
            // it is gone -- and it would look like success, because the games still
            // run and only a replay reveals it.
            throw new IllegalStateException(
                    "xmage.ai.deterministicTiebreak=true but this game carries no seed: "
                            + "set GameOptions.gameSeed or -Dxmage.game.seed");
        }
        UUID gameId = searchGame.getId();
        int ordinal = tiebreakOrdinal.merge(gameId, 1, Integer::sum) - 1;
        if (ordinal == 0) {
            // THE POSITIVE CONTROL FOR THE FLAG ITSELF, and it is not optional.
            // xmage.ai.deterministicTiebreak is read by Boolean.getBoolean in THIS
            // process; set on any other process it is ignored without erroring. The
            // search budgets had exactly that defect and it went unnoticed across a
            // 4,624-game corpus, because a knob that changes nothing looks identical
            // to a knob that is working on a run whose result you cannot predict.
            // With this line, one server log says which mode actually ran.
            logger.info("AI tie-break: DETERMINISTIC, game " + gameId + " seed " + seed
                    + " (xmage.ai.deterministicTiebreak=true)");
        }
        tiebreakRandom = new Random(seed * 1_000_003L + ordinal);
    }

    private boolean tiebreakCoin() {
        if (!DETERMINISTIC_TIEBREAK) {
            return RandomUtil.nextBoolean();
        }
        if (tiebreakRandom == null) {
            throw new IllegalStateException(
                    "tie-break coin drawn before beginTiebreakSequence(): a search path "
                            + "reaches the root comparison without seeding it first");
        }
        return tiebreakRandom.nextBoolean();
    }



    protected Integer addActionsTimed() {
        lastSearchOutcome.put(root.game.getId(), "complete");
        beginTiebreakSequence(root.game);
        // TODO: all actions added and calculated one by one,
        //  multithreading do not supported here
        // run new game simulation in parallel thread
        FutureTask<Integer> task = new FutureTask<>(() -> addActions(root, maxDepth, Integer.MIN_VALUE, Integer.MAX_VALUE));
        threadPoolSimulations.execute(task);
        try {
            int maxSeconds = maxThinkTimeSecs;
            if (COMPUTER_DISABLE_TIMEOUT_IN_GAME_SIMULATIONS) {
                maxSeconds = 3600;
            }
            logger.debug("maxThink: " + maxSeconds + " seconds ");
            Integer res = task.get(maxSeconds, TimeUnit.SECONDS);
            if (res != null) {
                return res;
            }
        } catch (TimeoutException | InterruptedException e) {
            lastSearchOutcome.put(root.game.getId(), "timeout");
            // AI thinks too long
            // how-to fix: look at stack info - it can contain bad ability with infinite choose dialog
            logger.warn("");
            logger.warn("AI player thinks too long (report it to github):");
            logger.warn(" - player: " + getName());
            logger.warn(" - battlefield size: " + root.game.getBattlefield().getAllPermanents().size());
            logger.warn(" - stack: " + root.game.getStack());
            logger.warn(" - game: " + root.game);
            printFreezeNode(root);
            logger.warn("");
            task.cancel(true);
        } catch (ExecutionException e) {
            lastSearchOutcome.put(root.game.getId(), "error");
            // game error
            logger.error("AI player catch game error in simulation - " + getName() + " - " + root.game + ": " + e, e);
            task.cancel(true);
            // real games: must catch and log
            // unit tests: must raise again for fast fail
            if (this.isTestMode() && this.isFastFailInTestMode()) {
                throw new IllegalStateException("One of the simulated games raise the error: " + e, e);
            }
        } catch (Throwable e) {
            // ?
            lastSearchOutcome.put(root.game.getId(), "error");
            logger.error("AI simulation catch unknown error: " + e, e);
            task.cancel(true);
        }
        //TODO: timeout handling
        return 0;
    }

    private void printFreezeNode(SimulationNode2 root) {
        // print simple tree - there are possible multiple child nodes, but ignore it - same for abilities
        List<String> chain = new ArrayList<>();
        SimulationNode2 node = root;
        while (node != null) {
            if (node.abilities != null && !node.abilities.isEmpty()) {
                Ability ability = node.abilities.get(0);
                String sourceInfo = CardUtil.getSourceIdName(node.game, ability);
                chain.add(String.format("%s: %s",
                        (sourceInfo.isEmpty() ? "unknown" : sourceInfo),
                        ability
                ));
            }
            node = node.children == null || node.children.isEmpty() ? null : node.children.get(0);
        }
        logger.warn("Possible freeze chain:");
        if (root != null && chain.isEmpty()) {
            logger.warn(" - unknown use case (too many possible targets?)"); // maybe can't finish any calc, maybe related to target options
        }
        chain.forEach(s -> {
            logger.warn(" - " + s);
        });
    }

    protected int simulatePriority(SimulationNode2 node, Game game, int depth, int alpha, int beta) {
        if (!COMPUTER_DISABLE_TIMEOUT_IN_GAME_SIMULATIONS && Thread.currentThread().isInterrupted()) {
            logger.debug("AI game sim interrupted by timeout");
            return GameStateEvaluator2.evaluate(playerId, game).getTotalScore();
        }
        node.setGameValue(game.getState().getValue(true).hashCode());
        SimulatedPlayer2 currentPlayer = (SimulatedPlayer2) game.getPlayer(game.getPlayerList().get());
        SimulationNode2 bestNode = null;
        List<Ability> allActions = currentPlayer.simulatePriority(game);
        optimize(game, allActions);
        int startedScore = GameStateEvaluator2.evaluate(this.getId(), node.getGame()).getTotalScore();
        if (logger.isInfoEnabled()
                && !allActions.isEmpty()
                && depth == maxDepth) {
            logger.info(String.format("POSSIBLE ACTION CHAINS for %s (%d, started score: %d)%s",
                    getName(),
                    allActions.size(),
                    startedScore,
                    (actions.isEmpty() ? "" : ":")
            ));
            for (int i = 0; i < allActions.size(); i++) {
                // print possible actions with detailed targets
                Ability possibleAbility = allActions.get(i);
                logger.info(String.format("-> #%d (%s)", i + 1, getAbilityAndSourceInfo(game, possibleAbility, true)));
            }
        }
        int actionNumber = 0;
        int bestValSubNodes = Integer.MIN_VALUE;
        for (Ability action : allActions) {
            actionNumber++;
            if (!COMPUTER_DISABLE_TIMEOUT_IN_GAME_SIMULATIONS && Thread.currentThread().isInterrupted()) {
                logger.info("Sim Prio [" + depth + "] -- interrupted");
                break;
            }
            Game sim = game.createSimulationForAI();
            if (!(action instanceof StaticAbility) //for MorphAbility, etc
                    && sim.getPlayer(currentPlayer.getId()).activateAbility((ActivatedAbility) action.copy(), sim)) {
                sim.applyEffects();
                if (checkForRepeatedAction(sim, node, action, currentPlayer.getId())) {
                    logger.debug("Sim Prio [" + depth + "] -- repeated action: " + action);
                    continue;
                }
                if (!sim.checkIfGameIsOver()
                        && (action.isUsesStack() || action instanceof PassAbility)) {
                    // skip priority for opponents before stack resolve
                    UUID nextPlayerId = sim.getPlayerList().get();
                    do {
                        sim.getPlayer(nextPlayerId).pass(game);
                        nextPlayerId = sim.getPlayerList().getNext();
                    } while (!Objects.equals(nextPlayerId, this.getId()));
                }
                SimulationNode2 newNode = new SimulationNode2(node, sim, action, depth, currentPlayer.getId());
                sim.checkStateAndTriggered();
                int finalScore;
                if (action instanceof PassAbility && sim.getStack().isEmpty()) {
                    // no more next actions, it's a final score
                    finalScore = GameStateEvaluator2.evaluate(this.getId(), sim).getTotalScore();
                } else {
                    // resolve current action and calc all next actions to find best score (return max possible score)
                    finalScore = addActions(newNode, depth - 1, alpha, beta);
                }
                logger.debug("Sim Prio " + BLANKS.substring(0, 2 + (maxDepth - depth) * 3) + '[' + depth + "]#" + actionNumber + " <" + finalScore + "> - (" + action + ") ");

                // Hints on data:
                // * node - started game with executed command (pay and put on stack)
                // * newNode - resolved game with resolved command (resolve stack)
                // * node.children - rewrites to store only best tree (e.g. contains only final data)
                // * node.score - rewrites to store max score (e.g. contains only final data)
                if (logger.isInfoEnabled()
                        && depth >= maxDepth) {
                    // show final calculated score and best actions chain from it
                    List<SimulationNode2> fullChain = new ArrayList<>();
                    fullChain.add(newNode);
                    SimulationNode2 finalNode = newNode;
                    while (!finalNode.getChildren().isEmpty()) {
                        finalNode = finalNode.getChildren().get(0);
                        fullChain.add(finalNode);
                    }

                    // example: Sim Prio [6] #1 <diff -19, +4444> (Lightning Bolt [aa5]: Cast Lightning Bolt -> Balduvian Bears [c49])
                    // total
                    logger.info(String.format("Sim Prio [%d] #%d <total score diff %s (from %s to %s)>",
                            depth,
                            actionNumber,
                            printDiffScore(finalScore - startedScore),
                            printDiffScore(startedScore),
                            printDiffScore(finalScore)
                    ));

                    // details
                    for (int chainIndex = 0; chainIndex < fullChain.size(); chainIndex++) {
                        SimulationNode2 currentNode = fullChain.get(chainIndex);
                        SimulationNode2 prevNode;
                        if (chainIndex == 0) {
                            prevNode = node;
                        } else {
                            prevNode = fullChain.get(chainIndex - 1);
                        }

                        int currentScore = GameStateEvaluator2.evaluate(this.getId(), currentNode.getGame()).getTotalScore();
                        int prevScore = GameStateEvaluator2.evaluate(this.getId(), prevNode.getGame()).getTotalScore();

                        if (currentNode.getAbilities() != null) {
                            // ON PRIORITY

                            // runtime check
                            if (currentNode.getAbilities().size() != 1) {
                                throw new IllegalStateException("AI's simulated game must contains only one selected action, but found: " + currentNode.getAbilities());
                            }
                            if (!currentNode.getTargets().isEmpty() || !currentNode.getChoices().isEmpty()) {
                                throw new IllegalStateException("WTF, simulated abilities with targets/choices");
                            }
                            logger.info(String.format("Sim Prio [%d] -> next action: [%d]<diff %s> (%s)",
                                    depth,
                                    currentNode.getDepth(),
                                    printDiffScore(currentScore - prevScore),
                                    getAbilityAndSourceInfo(currentNode.getGame(), currentNode.getAbilities().get(0), true)
                            ));
                        } else if (!currentNode.getTargets().isEmpty()) {
                            // ON TARGETS
                            String targetsInfo = currentNode.getTargets()
                                    .stream()
                                    .map(id -> {
                                        Player player = game.getPlayer(id);
                                        if (player != null) {
                                            return player.getName();
                                        }
                                        MageObject object = game.getObject(id);
                                        if (object != null) {
                                            return object.getIdName();
                                        }
                                        return "unknown";
                                    })
                                    .collect(Collectors.joining(", "));
                            logger.info(String.format("Sim Prio [%d] -> with possible choices: [%d]<diff %s> (%s)",
                                    depth,
                                    currentNode.getDepth(),
                                    printDiffScore(currentScore - prevScore),
                                    targetsInfo)
                            );
                        } else if (!currentNode.getChoices().isEmpty()) {
                            // ON CHOICES
                            String choicesInfo = String.join(", ", currentNode.getChoices());
                            logger.info(String.format("Sim Prio [%d] -> with possible choices (must not see that code): [%d]<diff %s> (%s)",
                                    depth,
                                    currentNode.getDepth(),
                                    printDiffScore(currentScore - prevScore),
                                    choicesInfo)
                            );
                        } else {
                            logger.info(String.format("Sim Prio [%d] -> with do nothing: [%d]<diff %s>",
                                    depth,
                                    currentNode.getDepth(),
                                    printDiffScore(currentScore - prevScore))
                            );
                        }
                    }
                }

                if (currentPlayer.getId().equals(playerId)) {
                    if (finalScore > bestValSubNodes) {
                        bestValSubNodes = finalScore;
                    }
                    if (depth == maxDepth
                            && action instanceof PassAbility) {
                        finalScore = finalScore - PASSIVITY_PENALTY; // passivity penalty
                    }
                    if (finalScore > alpha
                            || (depth == maxDepth
                            && finalScore == alpha
                            && tiebreakCoin())) { // Adding random for equal value to get change sometimes
                        alpha = finalScore;
                        bestNode = newNode;
                        bestNode.setScore(finalScore);
                        if (!newNode.getChildren().isEmpty()) {
                            // TODO: wtf, must review all code to remove shared objects
                            bestNode.setCombat(newNode.getChildren().get(0).getCombat());
                        }

                        // keep only best node
                        if (depth == maxDepth) {
                            logger.info("Sim Prio [" + depth + "] -* BEST actions chain so far: <final score " + bestNode.getScore() + ">");
                            node.children.clear();
                            node.children.add(bestNode);
                            node.setScore(bestNode.getScore());
                        }
                    }

                    // no need to check other actions
                    if (finalScore == GameStateEvaluator2.WIN_GAME_SCORE) {
                        logger.debug("Sim Prio -- win - break");
                        break;
                    }
                } else {
                    if (finalScore < beta) {
                        beta = finalScore;
                        bestNode = newNode;
                        bestNode.setScore(finalScore);
                        if (!newNode.getChildren().isEmpty()) {
                            bestNode.setCombat(newNode.getChildren().get(0).getCombat());
                        }
                    }

                    // no need to check other actions
                    if (finalScore == GameStateEvaluator2.LOSE_GAME_SCORE) {
                        logger.debug("Sim Prio -- lose - break");
                        break;
                    }
                }
                if (alpha >= beta) {
                    break;
                }
                if (SimulationNode2.nodeCount > MAX_SIMULATED_NODES_PER_ERROR) {
                    throw new IllegalStateException("AI ERROR: too many nodes (possible actions)");
                }
                if (SimulationNode2.nodeCount > maxNodes) {
                    logger.debug("Sim Prio -- reached end-state");
                    break;
                }
            }
        } // end of for (allActions)

        if (depth == maxDepth) {
            // TODO: buggy? Why it ended with depth limit 6 on one Pass action?!
            logger.info("Sim Prio [" + depth + "] ## Ended due max actions chain depth limit (" + maxDepth + ") -- Nodes calculated: " + SimulationNode2.nodeCount);
        }
        if (bestNode != null) {
            node.children.clear();
            node.children.add(bestNode);
            node.setScore(bestNode.getScore());
            if (logger.isTraceEnabled()
                    && !bestNode.getAbilities().toString().equals("[Pass]")) {
                logger.trace(new StringBuilder("Sim Prio [").append(depth).append("] -- Set after (depth=").append(depth).append(")  <").append(bestNode.getScore()).append("> ").append(bestNode.getAbilities().toString()).toString());
            }
        }

        if (currentPlayer.getId().equals(playerId)) {
            return bestValSubNodes;
        } else {
            return beta;
        }
    }

    protected String getAbilityAndSourceInfo(Game game, Ability ability, boolean showTargets) {
        // ability
        // TODO: add modal info
        // + (action.isModal() ? " Mode = " + action.getModes().getMode().toString() : "")
        if (ability.isModal()) {
            //throw new IllegalStateException("TODO: need implement");
        }
        MageObject sourceObject = ability.getSourceObject(game);
        String abilityInfo = (sourceObject == null ? "" : sourceObject.getIdName() + ": ") + CardUtil.substring(ability.toString(), 30, "...");
        // targets
        String targetsInfo = "";
        if (showTargets) {
            List<String> allTargetsInfo = new ArrayList<>();
            ability.getAllSelectedTargets().forEach(target -> {
                target.getTargets().forEach(selectedId -> {
                    String xInfo = "";
                    if (target instanceof TargetAmount) {
                        xInfo = "x" + target.getTargetAmount(selectedId) + " ";
                    }

                    String targetInfo = null;
                    Player player = game.getPlayer(selectedId);
                    if (player != null) {
                        targetInfo = player.getName();
                    }
                    if (targetInfo == null) {
                        MageObject object = game.getObject(selectedId);
                        if (object != null) {
                            targetInfo = object.getIdName();
                        }
                    }
                    if (targetInfo == null) {
                        StackObject stackObject = game.getState().getStack().getStackObject(selectedId);
                        if (stackObject != null) {
                            targetInfo = CardUtil.substring(stackObject.toString(), 20, "...");
                        }
                    }
                    if (targetInfo == null) {
                        targetInfo = "unknown";
                    }
                    allTargetsInfo.add(xInfo + targetInfo);
                });
            });
            targetsInfo = String.join(" + ", allTargetsInfo);
        }
        return abilityInfo + (targetsInfo.isEmpty() ? "" : " -> " + targetsInfo);
    }

    private String printDiffScore(int score) {
        if (score >= 0) {
            return "+" + score;
        } else {
            return "" + score;
        }
    }

    /**
     * Various AI optimizations for actions.
     *
     * @param game
     * @param allActions
     */
    protected void optimize(Game game, List<Ability> allActions) {
        for (TreeOptimizer optimizer : optimizers) {
            optimizer.optimize(game, allActions);
        }
        Collections.sort(allActions, new Comparator<Ability>() {
            @Override
            public int compare(Ability ability1, Ability ability2) {
                String rule1 = ability1.toString();
                String rule2 = ability2.toString();

                // pass
                boolean pass1 = rule1.startsWith("Pass");
                boolean pass2 = rule2.startsWith("Pass");
                if (pass1 != pass2) {
                    if (pass1) {
                        return 1;
                    } else {
                        return -1;
                    }
                }

                // play
                boolean play1 = rule1.startsWith("Play");
                boolean play2 = rule2.startsWith("Play");
                if (play1 != play2) {
                    if (play1) {
                        return -1;
                    } else {
                        return 1;
                    }
                }

                // cast
                boolean cast1 = rule1.startsWith("Cast");
                boolean cast2 = rule2.startsWith("Cast");
                if (cast1 != cast2) {
                    if (cast1) {
                        return -1;
                    } else {
                        return 1;
                    }
                }

                // default
                return ability1.getRule().compareTo(ability2.getRule());
            }
        });
    }

    protected boolean allPassed(Game game) {
        for (Player player : game.getPlayers().values()) {
            if (!player.isPassed()
                    && !player.hasLost()
                    && !player.hasLeft()) {
                return false;
            }
        }
        return true;
    }

    @Override
    public boolean choose(Outcome outcome, Choice choice, Game game) {
        if (choices.isEmpty()) {
            return super.choose(outcome, choice, game);
        }
        if (!choice.isChosen()) {
            if (!choice.setChoiceByAnswers(choices, true)) {
                choice.setRandomChoice();
            }
        }
        return true;
    }

    @Override
    public boolean chooseTarget(Outcome outcome, Cards cards, TargetCard target, Ability source, Game game) {
        if (targets.isEmpty()) {
            return super.chooseTarget(outcome, cards, target, source, game);
        }

        UUID abilityControllerId = target.getAffectedAbilityControllerId(getId());
        if (!target.isChoiceCompleted(abilityControllerId, source, game, cards)) {
            for (UUID targetId : targets) {
                target.addTarget(targetId, source, game);
                if (target.isChoiceCompleted(abilityControllerId, source, game, cards)) {
                    targets.clear();
                    return true;
                }
            }
            return false;
        }
        return true;
    }

    @Override
    public boolean choose(Outcome outcome, Cards cards, TargetCard target, Ability source, Game game) {
        if (targets.isEmpty()) {
            return super.choose(outcome, cards, target, source, game);
        }

        UUID abilityControllerId = target.getAffectedAbilityControllerId(getId());
        if (!target.isChoiceCompleted(abilityControllerId, source, game, cards)) {
            for (UUID targetId : targets) {
                target.add(targetId, game);
                if (target.isChoiceCompleted(abilityControllerId, source, game, cards)) {
                    targets.clear();
                    return true;
                }
            }
            return false;
        }
        return true;
    }

    private void declareBlockers(Game game, UUID activePlayerId) {
        game.fireEvent(new GameEvent(GameEvent.EventType.DECLARE_BLOCKERS_STEP_PRE, null, null, activePlayerId));
        if (!game.replaceEvent(GameEvent.getEvent(GameEvent.EventType.DECLARING_BLOCKERS, activePlayerId, activePlayerId))) {
            List<Permanent> attackers = getAttackers(game);
            if (attackers == null) {
                return;
            }

            List<Permanent> possibleBlockers = super.getAvailableBlockers(game);
            possibleBlockers = filterOutNonblocking(game, attackers, possibleBlockers);
            if (possibleBlockers.isEmpty()) {
                return;
            }

            attackers = filterOutUnblockable(game, attackers, possibleBlockers);
            if (attackers.isEmpty()) {
                return;
            }

            CombatUtil.sortByPower(attackers, false); // most powerfull go to first

            // Legal blockers BEFORE the assignment; afterwards the combat is built and
            // the untaken alternatives are gone -- the reason the comment at
            // selectAttackers gives, and it applies here identically.
            //
            // This deliberately does NOT go through getPlayable(): declaring blockers is
            // not an activated ability, so getPlayable() does not enumerate it and a
            // producer built on it records an EMPTY option list. SCHEMA.md predicts 38%
            // of Declare Blockers steps; that figure was never measurable here because
            // no blocking row existed, but the same reasoning applied to attackers is
            // why THAT hook enumerates canAttack instead -- 0 empty over 32,132 rows,
            // against SCHEMA's 36% prediction for a getPlayable() producer. Combat
            // records with no options are invalid and teach that combat has one
            // choice. `possibleBlockers` is already
            // filtered to creatures that can block something (filterOutNonblocking) and
            // `attackers` to ones that can be blocked (filterOutUnblockable), so it is
            // the real option set.
            boolean recBlk = AiDecisionRecorder.isEnabled()
                    && AiDecisionRecorder.hookEnabled("declare_blockers");
            List<String> blkIds = new ArrayList<>();
            List<String> blkTexts = new ArrayList<>();
            if (recBlk) {
                for (Permanent b : possibleBlockers) {
                    blkIds.add(b.getId().toString());
                    blkTexts.add(b.getName());
                }
            }

            CombatInfo combatInfo = CombatUtil.blockWithGoodTrade2(game, attackers, possibleBlockers);
            Player player = game.getPlayer(playerId);

            boolean blocked = false;
            for (Map.Entry<Permanent, List<Permanent>> entry : combatInfo.getCombat().entrySet()) {
                UUID attackerId = entry.getKey().getId();
                List<Permanent> blockers = entry.getValue();
                if (blockers != null) {
                    for (Permanent blocker : blockers) {
                        // TODO: buggy or miss on multi blocker requirements?!
                        player.declareBlocker(player.getId(), blocker.getId(), attackerId, game);
                        blocked = true;
                    }
                }
            }
            if (blocked) {
                game.getPlayers().resetPassed();
            }

            if (recBlk) {
                // Read the combat that was actually BUILT, not combatInfo's intent.
                // declareBlocker can refuse an assignment (see the multi-blocker TODO
                // above), and a record of a block the engine rejected would be a label
                // the game never contained. Same rule as selectAttackers, which reads
                // game.getCombat() rather than its own candidate list.
                List<String[]> blockerEntries = new ArrayList<>();
                Combat built = game.getCombat();
                if (built != null) {
                    for (CombatGroup g : built.getGroups()) {
                        for (UUID blockerId : g.getBlockers()) {
                            Permanent b = game.getPermanent(blockerId);
                            if (b == null || !b.isControlledBy(playerId)) {
                                continue; // another defender's block, not ours
                            }
                            // Only blockers THIS call offered. declareBlockers is
                            // re-entered within a combat, and game.getCombat() carries
                            // the union of every call -- so without this, a later record
                            // inherits an earlier one's block and its label names a
                            // creature that record never offered. Measured: 1 of 16
                            // rows in the first cross-deck run, label "p14:p21,p13:p21"
                            // against an option set of ["p13"]. A label that picks an
                            // unoffered option is untrainable whichever validator runs.
                            if (!blkIds.contains(blockerId.toString())) {
                                continue;
                            }
                            for (UUID attackerId : g.getAttackers()) {
                                // SEPARATOR GUARD READS THE ID BUILDER, NOT THE TEXT
                                // ONE. describeObject returns "" for an object it
                                // cannot resolve -- a permanent that has left the
                                // battlefield by the time the record is written -- so
                                // guarding on picked.length() means an empty first
                                // name leaves the guard false on the next iteration
                                // and the two UUIDs CONCATENATE with no comma.
                                // Measured in the live corpus: 2 of 33,998 attacker
                                // labels, e.g. "...31531c774eaa15bee822-d8f8-..." and
                                // one with three ids fused. The text showed it too --
                                // "Courser of Kruphix, , , , , Voyaging Satyr" -- but
                                // an unparseable id is the part that breaks training.
                                // A UUID is never empty, so pickedIds always grows and
                                // is the only reliable guard for both.
                                // Collected and sorted below rather than appended
                                // here, for the same reason as the attackers: the
                                // enclosing iteration walks g.getAttackers(), a
                                // HashSet over per-game UUIDs, so two runs making the
                                // identical block emit the pairs in different orders.
                                // ">" is the ENGINE-side pair separator, the same one
                                // AiHintProvider uses. The model-facing grammar is
                                // `blockers=p5:p1`; records_to_sft maps uuids to aliases
                                // and joins with ":" there. Two layers, not two grammars
                                // -- the recorder is raw material for the schema, not an
                                // instance of it (SCHEMA.md).
                                blockerEntries.add(new String[]{
                                        describeObject(game, blockerId) + ">" + describeObject(game, attackerId),
                                        blockerId + ">" + attackerId});
                            }
                        }
                    }
                }
                // An empty combat HERE is the AI declining to block with blockers
                // available -- a decision, and the common one. Gating this call on
                // `blocked` would record only the block-something branch, making 100%
                // of blocking rows in the corpus a block and teaching a policy that
                // always blocks. The three returns above are the real non-decisions
                // (no attackers / nothing that can block / nothing blockable) and they
                // record nothing, which is why this one must record.
                // Sorted by the pair's DISPLAY string, so two runs that made the
                // identical block emit the identical label. See the attackers site
                // for why the id is not the key: it is the thing that differs.
                blockerEntries.sort(Comparator.<String[], String>comparing(e -> e[0]).thenComparing(e -> e[1]));
                String blkPicked = String.join(", ", blockerEntries.stream().map(e -> e[0]).toList());
                String blkPickedIds = String.join(",", blockerEntries.stream().map(e -> e[1]).toList());
                AiDecisionRecorder.recordChoice(game, this, "declare_blockers",
                        "Declare blockers", blkIds, blkTexts, blkPickedIds,
                        blkPicked.isEmpty() ? "none" : blkPicked);
            }
        }
    }

    private List<Permanent> filterOutNonblocking(Game game, List<Permanent> attackers, List<Permanent> blockers) {
        List<Permanent> blockersLeft = new ArrayList<>();
        for (Permanent blocker : blockers) {
            for (Permanent attacker : attackers) {
                if (blocker.canBlock(attacker.getId(), game)) {
                    blockersLeft.add(blocker);
                    break;
                }
            }
        }
        return blockersLeft;
    }

    private List<Permanent> filterOutUnblockable(Game game, List<Permanent> attackers, List<Permanent> blockers) {
        List<Permanent> attackersLeft = new ArrayList<>();
        for (Permanent attacker : attackers) {
            if (CombatUtil.canBeBlocked(game, attacker, blockers)) {
                attackersLeft.add(attacker);
            }
        }
        return attackersLeft;
    }

    private List<Permanent> getAttackers(Game game) {
        Set<UUID> attackersUUID = game.getCombat().getAttackers();
        if (attackersUUID.isEmpty()) {
            return null;
        }

        List<Permanent> attackers = new ArrayList<>();
        for (UUID attackerId : attackersUUID) {
            Permanent permanent = game.getPermanent(attackerId);
            attackers.add(permanent);
        }
        return attackers;
    }

    /**
     * Choose attackers based on static information. That means that AI won't
     * look to the future as it was before, but just choose attackers based on
     * current state of the game. This is worse, but at least it is easier to
     * implement and won't lead to the case when AI doesn't do anything -
     * neither attack nor block.
     *
     * @param game
     * @param activePlayerId
     */
    private void declareAttackers(Game game, UUID activePlayerId) {
        attackersToCheck.clear();
        attackersList.clear();
        game.fireEvent(new GameEvent(GameEvent.EventType.DECLARE_ATTACKERS_STEP_PRE, null, null, activePlayerId));
        if (!game.replaceEvent(GameEvent.getEvent(GameEvent.EventType.DECLARING_ATTACKERS, activePlayerId, activePlayerId))) {
            Player attackingPlayer = game.getPlayer(activePlayerId);

            // check alpha strike first (all in attack to kill a player)
            for (UUID defenderId : game.getOpponents(playerId, true)) {
                Player defender = game.getPlayer(defenderId);
                if (!defender.isInGame()) {
                    continue;
                }

                attackersList = super.getAvailableAttackers(defenderId, game);
                if (attackersList.isEmpty()) {
                    continue;
                }
                List<Permanent> possibleBlockers = defender.getAvailableBlockers(game);
                List<Permanent> killers = CombatUtil.canKillOpponent(game, attackersList, possibleBlockers, defender);
                if (!killers.isEmpty()) {
                    for (Permanent attacker : killers) {
                        attackingPlayer.declareAttacker(attacker.getId(), defenderId, game, false);
                    }
                    return;
                }
            }

            // TODO: add game simulations here to find best attackers/blockers combination

            // find safe attackers (can't be killed by blockers)
            for (UUID defenderId : game.getOpponents(playerId, true)) {
                Player defender = game.getPlayer(defenderId);
                if (!defender.isInGame()) {
                    continue;
                }
                attackersList = super.getAvailableAttackers(defenderId, game);
                if (attackersList.isEmpty()) {
                    continue;
                }
                List<Permanent> possibleBlockers = defender.getAvailableBlockers(game);

                // The AI will now attack more sanely.  Simple, but good enough for now.
                // The sim minmax does not work at the moment.
                boolean safeToAttack;
                CombatEvaluator eval = new CombatEvaluator();

                for (Permanent attacker : attackersList) {
                    safeToAttack = true;
                    int attackerValue = eval.evaluate(attacker, game);
                    for (Permanent blocker : possibleBlockers) {
                        int blockerValue = eval.evaluate(blocker, game);

                        // blocker can kill attacker
                        if (attacker.getPower().getValue() <= blocker.getToughness().getValue()
                                && attacker.getToughness().getValue() <= blocker.getPower().getValue()) {
                            safeToAttack = false;
                        }

                        // attacker and blocker have the same P/T, check their overall value
                        if (attacker.getToughness().getValue() == blocker.getPower().getValue()
                                && attacker.getPower().getValue() == blocker.getToughness().getValue()) {
                            if (attackerValue > blockerValue
                                    || blocker.getAbilities().containsKey(FirstStrikeAbility.getInstance().getId())
                                    || blocker.getAbilities().containsKey(DoubleStrikeAbility.getInstance().getId())
                                    || blocker.getAbilities().contains(new ExaltedAbility())
                                    || blocker.getAbilities().containsKey(DeathtouchAbility.getInstance().getId())
                                    || blocker.getAbilities().containsKey(IndestructibleAbility.getInstance().getId())
                                    || !attacker.getAbilities().containsKey(FirstStrikeAbility.getInstance().getId())
                                    || !attacker.getAbilities().containsKey(DoubleStrikeAbility.getInstance().getId())
                                    || !attacker.getAbilities().contains(new ExaltedAbility())) {
                                safeToAttack = false;
                            }
                        }

                        // attacker can kill by deathtouch
                        if (attacker.getAbilities().containsKey(DeathtouchAbility.getInstance().getId())
                                || attacker.getAbilities().containsKey(IndestructibleAbility.getInstance().getId())) {
                            safeToAttack = true;
                        }

                        // attacker has flying and blocker has neither flying nor reach
                        if (attacker.getAbilities().containsKey(FlyingAbility.getInstance().getId())
                                && !blocker.getAbilities().containsKey(FlyingAbility.getInstance().getId())
                                && !blocker.getAbilities().containsKey(ReachAbility.getInstance().getId())) {
                            safeToAttack = true;
                        }

                        // if any check fails, move on to the next possible attacker
                        if (!safeToAttack) {
                            break;
                        }
                    }

                    // 0 power, don't bother attacking
                    if (attacker.getPower().getValue() == 0) {
                        safeToAttack = false;
                    }

                    // add attacker to the next list of all attackers that can safely attack
                    if (safeToAttack) {
                        attackersToCheck.add(attacker);
                    }
                }

                // find possible target for attack (priority: planeswalker -> battle -> player)
                int totalPowerOfAttackers = 0;
                int usedPowerOfAttackers = 0;
                for (Permanent attacker : attackersToCheck) {
                    totalPowerOfAttackers += attacker.getPower().getValue();
                }

                // TRY ATTACK PLANESWALKER + BATTLE
                List<Permanent> possiblePermanentDefenders = new ArrayList<>();
                // planeswalker first priority
                game.getBattlefield().getActivePermanents(StaticFilters.FILTER_PERMANENT_PLANESWALKER, activePlayerId, game)
                        .stream()
                        .filter(p -> p.canBeAttacked(null, defenderId, game))
                        .forEach(possiblePermanentDefenders::add);
                // battle second priority
                game.getBattlefield().getActivePermanents(StaticFilters.FILTER_PERMANENT_BATTLE, activePlayerId, game)
                        .stream()
                        .filter(p -> p.canBeAttacked(null, defenderId, game))
                        .forEach(possiblePermanentDefenders::add);

                for (Permanent permanentDefender : possiblePermanentDefenders) {
                    if (usedPowerOfAttackers >= totalPowerOfAttackers) {
                        break;
                    }
                    int currentCounters;
                    if (permanentDefender.isPlaneswalker(game)) {
                        currentCounters = permanentDefender.getCounters(game).getCount(CounterType.LOYALTY);
                    } else if (permanentDefender.isBattle(game)) {
                        currentCounters = permanentDefender.getCounters(game).getCount(CounterType.DEFENSE);
                    } else {
                        // impossible error (SBA must remove all planeswalkers/battles with 0 counters before declare attackers)
                        throw new IllegalStateException("AI: can't find counters for defending permanent " + permanentDefender.getName(), new Throwable());
                    }

                    // attack anyway (for kill or damage)
                    // TODO: add attackers optimization here (1 powerfull + min number of additional permanents,
                    //  current code uses random/etb order)
                    for (Permanent attackingPermanent : attackersToCheck) {
                        if (attackingPermanent.isAttacking()) {
                            // already used for another target
                            continue;
                        }
                        attackingPlayer.declareAttacker(attackingPermanent.getId(), permanentDefender.getId(), game, true);
                        currentCounters -= attackingPermanent.getPower().getValue();
                        usedPowerOfAttackers += attackingPermanent.getPower().getValue();
                        if (currentCounters <= 0) {
                            break;
                        }
                    }
                }

                // TRY ATTACK PLAYER
                // any remaining attackers go for the player
                for (Permanent attackingPermanent : attackersToCheck) {
                    if (attackingPermanent.isAttacking()) {
                        continue;
                    }
                    attackingPlayer.declareAttacker(attackingPermanent.getId(), defenderId, game, true);
                }
            }
        }
    }

    @Override
    public void selectAttackers(Game game, UUID attackingPlayerId) {
        logger.debug("selectAttackers");
        // Legal attackers BEFORE the declaration; afterwards the combat is already
        // built and the untaken alternatives are gone.
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        boolean recAtk = AiDecisionRecorder.isEnabled() && AiDecisionRecorder.hookEnabled("select_attackers");
        if (recAtk) {
            for (Permanent p : game.getBattlefield().getAllActivePermanents(playerId)) {
                if (p.canAttack(null, game)) {
                    ids.add(p.getId().toString());
                    texts.add(p.getName());
                }
            }
        }
        declareAttackers(game, playerId);
        if (recAtk) {
            // Same as targets: an attack is a SET of creatures, and all of them are
            // the label. The prompt interface takes them as a list too.
            // SORTED BY NAME, and the sort key is the whole point.
            //
            // Combat.getAttackers() returns a HashSet<UUID> (Combat.java:131) over
            // permanent UUIDs that are MINTED FRESH EACH GAME, so iteration order
            // varies between two runs that made the IDENTICAL attack. Measured on 40
            // seeds x 2 replicates at one pin in one JVM: 9 of 9 diverging attack
            // labels were the same creatures in a different order, and 1 of 1
            // diverging block labels likewise. It read as engine nondeterminism and
            // it was this.
            //
            // SORTING BY ID WOULD NOT FIX IT -- the ids are exactly the thing that
            // differs between runs. The name is the only key stable across them.
            // Ties (two "Golem Token") are harmless: the rendered label is identical
            // either way, which is what the comparison reads.
            //
            // String.join also retires the separator guard below: an empty name can
            // no longer fuse two ids, because the entries are joined rather than
            // appended.
            List<String[]> attackerEntries = new ArrayList<>();
            if (game.getCombat() != null) {
                for (UUID aid : game.getCombat().getAttackers()) {
                    attackerEntries.add(new String[]{describeObject(game, aid), aid.toString()});
                }
            }
            attackerEntries.sort(Comparator.<String[], String>comparing(e -> e[0]).thenComparing(e -> e[1]));
            StringBuilder picked = new StringBuilder(
                    String.join(", ", attackerEntries.stream().map(e -> e[0]).toList()));
            StringBuilder pickedIds = new StringBuilder(
                    String.join(",", attackerEntries.stream().map(e -> e[1]).toList()));
            AiDecisionRecorder.recordChoice(game, this, "select_attackers", "Select attackers",
                    ids, texts, pickedIds.toString(),
                    picked.length() == 0 ? "none" : picked.toString());
        }
    }

    @Override
    public void selectBlockers(Ability source, Game game, UUID defendingPlayerId) {
        logger.debug("selectBlockers");
        declareBlockers(game, playerId);
    }

    // ---------------------------------------------------------------------
    // SUB-DECISION RECORDING.
    //
    // The priority loop in act() is only 73 of a game's ~86 decision points. The
    // other 13 -- modes, which mana, which spell or ability, attackers, targets --
    // are asked through these methods, and a corpus without them cannot answer
    // everything the prompt interface asks.
    //
    // EVERY OVERRIDE HERE HAS THE SAME SHAPE, AND THE SHAPE IS THE SAFETY ARGUMENT:
    // call super exactly once, record what it returned, return it unchanged. No
    // branch depends on whether recording is on, and no engine input is read after
    // super has run except to describe it. The recorder is hooked in the subclass
    // rather than in ComputerPlayer because AiDecisionRecorder lives in this module
    // and the base AI module cannot see it -- and because leaving the base class
    // untouched means an unrecorded game runs exactly the code it ran before.
    //
    // These labels are NOT uniformly worth training on; see recordChoice's javadoc.
    // chooseMode takes the first valid mode and chooseUse answers a blanket yes, so
    // those two are format coverage, not skill. Filter on `kind`.
    // ---------------------------------------------------------------------

    private String describeObject(Game game, UUID id) {
        if (id == null) {
            return "";
        }
        Player p = game.getPlayer(id);
        if (p != null) {
            return p.getName();
        }
        MageObject o = game.getObject(id);
        return o == null ? id.toString() : o.getName();
    }

    @Override
    public boolean chooseMulligan(Game game) {
        boolean mulligan = super.chooseMulligan(game);
        // BEFORE THE HAND GOES BACK. Mulligan.executeMulliganPhase calls this and
        // only then reshuffles, so the hand read here is the one the decision was
        // made on. recordChoice's own header writes the hand with ids and names,
        // so this adds only what the header cannot derive: how many of them are
        // lands, which is the entire input to the rule.
        //
        // THE HAND IS NOT ALWAYS 7 HERE, which is worth writing down because the
        // usual description of the London mulligan says it should be. XMage's
        // LondonMulligan.mulligan() draws the full starting hand and then bottoms
        // down to the new size IMMEDIATELY, inside the same call -- not at keep
        // time -- so the next chooseMulligan sees a SMALLER hand. Measured over
        // 290 games: 582 decisions at 7 cards, 112 at 6, 51 at 5. The chain is
        // exact (112 mulligans at 7 produced 112 decisions at 6; 51 at 6 produced
        // 51 at 5), so `hand.size() - 2` is a moving threshold and the
        // `hand.size() < 6` early keep is live, not dead code: it fired on all 51
        // five-card hands.
        //
        // HOOKED HERE AND NOT ON THE BASE CLASS, which is where it belongs and
        // cannot go: AiDecisionRecorder lives in Mage.Player.AI.MA and
        // ComputerPlayer in Mage.Player.AI, and the pom dependency runs MA -> AI
        // only. So a BARE ComputerPlayer IS NOT COVERED by this, nor are
        // SimulatedPlayerMCTS and ComputerPlayerControllableProxy, which override
        // chooseMulligan themselves. That is not a gap in practice -- every seat
        // in the corpus is a skill player, i.e. a ComputerPlayer6 -- but a run
        // that ever uses a plain ComputerPlayer will record no mulligans and look
        // exactly like a run where nobody mulliganed.
        if (AiDecisionRecorder.isEnabled() && AiDecisionRecorder.hookEnabled("choose_mulligan")) {
            int lands = hand.getCards(new FilterLandCard(), game).size();
            // EMPTY IDS, TEXT ONLY. A mulligan is a boolean, not a target: there
            // is no card whose uuid could identify the choice. The assembler
            // requires every chosen id to be a comma-separated list of UUIDs and
            // treats "" as legitimate -- a decline carries no id -- so anything
            // else fails build_dataset's malformed_label check and DROPS THE
            // WHOLE GAME. Measured before this was fixed: 287 of 290 games
            // dropped, i.e. essentially every game, because nearly every game
            // contains at least one mulligan decision.
            AiDecisionRecorder.recordChoice(game, this, "choose_mulligan",
                    "Mulligan? hand=" + hand.size() + " lands=" + lands,
                    Arrays.asList("", ""),
                    Arrays.asList("keep", "mulligan"),
                    "",
                    mulligan ? "mulligan" : "keep");
        }
        return mulligan;
    }

    @Override
    public Mode chooseMode(Modes modes, Ability source, Game game) {
        Mode chosen = super.chooseMode(modes, source, game);
        if (AiDecisionRecorder.isEnabled() && AiDecisionRecorder.hookEnabled("choose_mode")) {
            List<String> ids = new ArrayList<>();
            List<String> texts = new ArrayList<>();
            for (Mode m : modes.getAvailableModes(source, game)) {
                ids.add(m.getId() == null ? "" : m.getId().toString());
                texts.add(String.valueOf(m));
            }
            AiDecisionRecorder.recordChoice(game, this, "choose_mode",
                    "Choose mode: " + describeObject(game, source == null ? null : source.getSourceId()),
                    ids, texts,
                    chosen == null || chosen.getId() == null ? "" : chosen.getId().toString(),
                    chosen == null ? null : String.valueOf(chosen));
        }
        return chosen;
    }

    @Override
    public boolean chooseUse(Outcome outcome, String message, Ability source, Game game) {
        boolean chosen = super.chooseUse(outcome, message, source, game);
        if (AiDecisionRecorder.isEnabled() && AiDecisionRecorder.hookEnabled("choose_use")) {
            AiDecisionRecorder.recordChoice(game, this, "choose_use", message,
                    Arrays.asList("", ""), Arrays.asList("yes", "no"),
                    "", chosen ? "yes" : "no");
        }
        return chosen;
    }

    @Override
    public boolean chooseTarget(Outcome outcome, Target target, Ability source, Game game) {
        // Snapshot the legal set BEFORE super runs. Afterwards the target is already
        // filled, and "what could it have picked" is no longer answerable.
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        boolean recTarget = AiDecisionRecorder.isEnabled() && AiDecisionRecorder.hookEnabled("choose_target");
        if (recTarget) {
            try {
                for (UUID id : target.possibleTargets(getId(), source, game)) {
                    ids.add(id.toString());
                    texts.add(describeObject(game, id));
                }
            } catch (RuntimeException e) {
                logger.debug("chooseTarget: could not enumerate possible targets", e);
            }
        }
        boolean ok = super.chooseTarget(outcome, target, source, game);
        if (recTarget) {
            // A target set is a LIST, so both id and text carry every element,
            // comma-joined. Keeping only the first would silently drop the rest of
            // a multi-target spell, and the drop would be invisible downstream.
            StringBuilder picked = new StringBuilder();
            StringBuilder pickedIds = new StringBuilder();
            for (UUID id : target.getTargets()) {
                // Same guard, same reason: a target can be unresolvable too.
                if (pickedIds.length() > 0) {
                    picked.append(", ");
                    pickedIds.append(",");
                }
                picked.append(describeObject(game, id));
                pickedIds.append(id);
            }
            AiDecisionRecorder.recordChoice(game, this, "choose_target",
                    "Select target: " + describeObject(game, source == null ? null : source.getSourceId()),
                    ids, texts,
                    pickedIds.toString(), picked.length() == 0 ? null : picked.toString());
        }
        return ok;
    }

    @Override
    public SpellAbility chooseAbilityForCast(Card card, Game game, boolean noMana) {
        SpellAbility chosen = super.chooseAbilityForCast(card, game, noMana);
        if (AiDecisionRecorder.isEnabled() && AiDecisionRecorder.hookEnabled("choose_ability_for_cast")) {
            List<String> ids = new ArrayList<>();
            List<String> texts = new ArrayList<>();
            for (Ability a : card.getAbilities(game)) {
                if (!(a instanceof SpellAbility)) {
                    continue;
                }
                SpellAbility sa = (SpellAbility) a;
                ids.add(sa.getId() == null ? "" : sa.getId().toString());
                texts.add(sa.getRule());
            }
            AiDecisionRecorder.recordChoice(game, this, "choose_ability_for_cast",
                    "Choose spell or ability to play: " + card.getName(),
                    ids, texts,
                    chosen == null || chosen.getId() == null ? "" : chosen.getId().toString(),
                    chosen == null ? null : chosen.getRule());
        }
        return chosen;
    }

    /**
     * Copies game and replaces all players in copy with simulated players
     *
     * @param game
     * @return a new game object with simulated players
     */
    protected Game createSimulation(Game game) {
        Game sim = game.createSimulationForAI();
        for (Player oldPlayer : sim.getState().getPlayers().values()) {
            // replace original player by simulated player and find result (execute/resolve current action)
            Player origPlayer = game.getState().getPlayers().get(oldPlayer.getId()).copy();
            SimulatedPlayer2 simPlayer = new SimulatedPlayer2(oldPlayer, oldPlayer.getId().equals(playerId));
            simPlayer.restore(origPlayer);
            sim.getState().getPlayers().put(oldPlayer.getId(), simPlayer);
        }
        return sim;
    }

    private boolean checkForRepeatedAction(Game sim, SimulationNode2 node, Ability action, UUID playerId) {
        // pass or casting two times a spell multiple times on hand is ok
        if (action instanceof PassAbility || action instanceof SpellAbility || action.isManaAbility()) {
            return false;
        }
        int newVal = GameStateEvaluator2.evaluate(playerId, sim).getTotalScore();
        SimulationNode2 test = node.getParent();
        while (test != null) {
            if (test.getPlayerId().equals(playerId)) {
                if (test.getAbilities() != null && test.getAbilities().size() == 1) {
                    if (action.toString().equals(test.getAbilities().get(0).toString())) {
                        if (test.getParent() != null) {
                            Game prevGame = node.getGame();
                            if (prevGame != null) {
                                int oldVal = GameStateEvaluator2.evaluate(playerId, prevGame).getTotalScore();
                                if (oldVal >= newVal) {
                                    return true;
                                }
                            }
                        }
                    }
                }
            }
            test = test.getParent();
        }
        return false;
    }

    @Override
    public void cleanUpOnMatchEnd() {
        root = null;
        super.cleanUpOnMatchEnd();
    }

}
