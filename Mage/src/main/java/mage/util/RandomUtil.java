package mage.util;

import java.awt.*;
import java.util.Collection;
import java.util.Random;
import java.util.Set;
import java.util.UUID;
import java.util.function.Supplier;

/**
 * Created by IGOUDT on 5-9-2016.
 */
public final class RandomUtil {

    private static final Random random = new Random(); // thread safe with seed support

    // A ROLLOUT'S OWN STREAM. A thread that seeded itself draws from here; every other thread
    // draws from the process stream above, exactly as before this existed, so the live game and
    // its setSeed(gameSeed) are unchanged. With only the global stream, seeding a rollout reseeded
    // the live game and every concurrent rollout (Mage.Tests RolloutRngTest; the mtg repo's
    // docs/eval/instance-rng-estimate.md). Pools reuse threads, so this is set per TASK and
    // cleared in a finally -- withThreadSeed does both.
    //
    // DELIBERATE DEVIATION from the design sketch in instance-rng-estimate.md, which used
    // ThreadLocal.withInitial(Random::new): that gives EVERY unseeded thread a fresh unseeded
    // stream, so GameImpl's setSeed(gameSeed) would seed only the thread that called it and a
    // seeded live game would stop being reproducible wherever it draws off that thread. Here an
    // unseeded thread has no entry and draws from the process stream, exactly as before.
    private static final ThreadLocal<Random> threadRandom = new ThreadLocal<>();

    private static Random current() {
        Random own = threadRandom.get();
        return own != null ? own : random;
    }

    private RandomUtil() {
    }

    public static Random getRandom() {
        return current();
    }

    public static int nextInt() {
        return current().nextInt();
    }

    public static int nextInt(int max) {
        return current().nextInt(max);
    }

    public static boolean nextBoolean() {
        return current().nextBoolean();
    }

    public static long nextLong() {
        return current().nextLong();
    }

    /** True inside a seeded rollout task (withThreadSeed), false on every other thread. */
    public static boolean hasThreadStream() {
        return threadRandom.get() != null;
    }

    public static double nextDouble() {
        return current().nextDouble();
    }

    public static Color nextColor() {
        return new Color(RandomUtil.nextInt(256), RandomUtil.nextInt(256), RandomUtil.nextInt(256));
    }

    public static void setSeed(long newSeed) {
        // Reseeding the PROCESS stream from inside a rollout is the exact bug this class had.
        if (threadRandom.get() != null) {
            throw new IllegalStateException("setSeed called on a thread with its own rollout stream: "
                    + Thread.currentThread().getName() + " -- it would reseed the live game");
        }
        random.setSeed(newSeed);
    }

    public static void seedThisThread(long seed) {
        // Already seeded means the previous task on this pooled thread never cleared: its stream
        // would leak into this one. Refuse rather than silently replace it.
        if (threadRandom.get() != null) {
            throw new IllegalStateException("thread already has a rollout stream (not cleared by its last task): "
                    + Thread.currentThread().getName());
        }
        threadRandom.set(new Random(seed));
    }

    public static void clearThisThread() {
        threadRandom.remove();
    }

    public static <T> T withThreadSeed(long seed, Supplier<T> task) {
        seedThisThread(seed);
        try {
            return task.get();
        } finally {
            clearThisThread();
        }
    }

    public static <T> T randomFromCollection(Collection<T> collection) {
        if (collection.size() < 2) {
            return collection.stream().findFirst().orElse(null);
        }
        int rand = nextInt(collection.size());
        int count = 0;
        for (T current : collection) {
            if (count == rand) {
                return current;
            }
            count++;
        }
        return null;
    }
}
