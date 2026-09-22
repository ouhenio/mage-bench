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

    private RandomUtil() {
    }

    public static Random getRandom() {
        return random;
    }

    public static int nextInt() {
        return random.nextInt();
    }

    public static int nextInt(int max) {
        return random.nextInt(max);
    }

    public static boolean nextBoolean() {
        return random.nextBoolean();
    }

    public static double nextDouble() {
        return random.nextDouble();
    }

    public static Color nextColor() {
        return new Color(RandomUtil.nextInt(256), RandomUtil.nextInt(256), RandomUtil.nextInt(256));
    }

    public static void setSeed(long newSeed) {
        random.setSeed(newSeed);
    }

    // RED STUB, replaced in the next commit: today's semantics under the new names, so the rollout
    // RNG test compiles against the unmodified engine and shows its failure. Seeding "this thread"
    // here reseeds the one process-global stream, which is exactly what the test must reject.
    public static void seedThisThread(long seed) {
        random.setSeed(seed);
    }

    public static void clearThisThread() {
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
