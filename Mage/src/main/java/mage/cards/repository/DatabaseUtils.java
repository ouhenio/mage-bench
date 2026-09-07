package mage.cards.repository;

import com.j256.ormlite.jdbc.JdbcConnectionSource;
import com.j256.ormlite.support.ConnectionSource;
import com.j256.ormlite.support.DatabaseConnection;
import mage.util.DebugUtil;
import org.apache.log4j.Logger;

import java.nio.file.Path;
import java.nio.file.Paths;
import java.sql.SQLException;

/**
 * Helper class for database
 *
 * @author JayDi85
 */
public class DatabaseUtils {

    private static final Logger logger = Logger.getLogger(DatabaseUtils.class);
    private static final String H2_FILE_PREFIX = "jdbc:h2:file:";

    // warning, do not change names or db format
    // h2
    public static final String DB_NAME_FEEDBACK = "feedback.h2";
    public static final String DB_NAME_USERS = "authorized_user.h2";
    public static final String DB_NAME_CARDS = "cards.h2";
    // sqlite (usage reason: h2 database works bad with 1GB+ files and can break it)
    public static final String DB_NAME_RECORDS = "table_record.db";
    public static final String DB_NAME_STATS = "user_stats.db";

    /**
     * Prepare JDBC connection string and setup additional params for H2 databases
     *
     * @param dbName        database name like "cards.h2"
     * @param improveCaches use memory optimizations for cards database (no needs for other dbs)
     */
    public static String prepareH2Connection(String dbName, boolean improveCaches) {
        // example: jdbc:h2:file:./db/cards.h2;AUTO_SERVER=TRUE;IGNORECASE=TRUE
        String res = String.format("jdbc:h2:file:./db/%s", dbName);

        // shared params
        res += ";AUTO_SERVER=TRUE"; // open database in mix mode (first open by new thread, second open by new jvm-process)
        res += ";IGNORECASE=TRUE"; // ignore char case for text searching

        // additional params
        // can be defined by connection string, by exec sql like "SET xxx = yyy", by settings from existing db-file

        if (improveCaches) {
            // CACHE_SIZE
            // max query cache size in kb (default: 65 Mb per 1 GB of java's max memory)
            // warning, xmage require 150Mb cache for big queries in AI games like all card names (db can be broken on lower cache)
            //res += ";CACHE_SIZE=150000";
            res += ";CACHE_SIZE=" + Math.round(Math.max(150000, Runtime.getRuntime().maxMemory() * 0.1 / 1024));


            // QUERY_CACHE_SIZE
            // queries amount per session to cache (default: 8)
            res += ";QUERY_CACHE_SIZE=32";
        }

        // add debug stats (see DebugUtil for usage instruction)
        if (DebugUtil.DATABASE_PROFILE_SQL_QUERIES_TO_FILE) {
            res += ";TRACE_LEVEL_FILE=2";
            res += ";QUERY_STATISTICS=TRUE";
        }

        return res;
    }

    /**
     * Open an H2 database connection, retrying on lock contention.
     *
     * When multiple JVM processes open the same H2 database concurrently
     * (e.g. during golden tests), the file lock can race. AUTO_SERVER=TRUE
     * handles steady-state multi-process access, but the initial lock
     * acquisition can fail if two JVMs try simultaneously. This method
     * retries with backoff so the second JVM waits for the first to finish.
     *
     * @param url JDBC connection URL from {@link #prepareH2Connection}
     */
    /** Default retry budget, in ms. See {@link #retryBudgetMs()} for why it is not 261s. */
    static final long DEFAULT_RETRY_BUDGET_MS = 600_000L;
    /** Backoff is capped: doubling without a ceiling spends the whole budget in two sleeps. */
    static final long MAX_BACKOFF_MS = 5_000L;

    /**
     * How long to keep trying, in ms. MAGEBENCH_H2_RETRY_BUDGET_MS wins and says so.
     *
     * NOT FITTED TO THE ONE WINDOW WE MEASURED. Corpus job 3135's cold cohort of 48 produced
     * a 261-second contention window, and a budget of 261s would be a budget that works
     * exactly until the next cohort is bigger. The window's length is a function of how many
     * servers start at once, which this process cannot see -- one game per process -- so the
     * runner sets this from its own concurrency and the default is simply generous against a
     * quantity nobody has bounded.
     *
     * The number to replace it with will come from data rather than from another single
     * observation: every successful retry now logs its elapsed wait, so the distribution
     * accumulates across runs instead of being reconstructed after an incident.
     */
    static long retryBudgetMs() {
        String raw = System.getenv("MAGEBENCH_H2_RETRY_BUDGET_MS");
        if (raw == null || raw.isBlank()) {
            return DEFAULT_RETRY_BUDGET_MS;
        }
        long value = Long.parseLong(raw.trim());  // malformed must throw, never fall back
        if (value < 1_000L) {
            throw new IllegalArgumentException(
                    "MAGEBENCH_H2_RETRY_BUDGET_MS=" + value + " is below 1000ms; a budget shorter"
                            + " than one server start cannot absorb any contention at all.");
        }
        return value;
    }

    public static ConnectionSource openH2ConnectionWithRetry(String url) throws SQLException {
        long budgetMs = retryBudgetMs();
        long baseDelayMs = 500L;
        SQLException lastError = null;
        long startedAt = System.currentTimeMillis();
        // BUDGET-BOUNDED, NOT ATTEMPT-BOUNDED. The old loop ran 5 attempts with delays of
        // 500/1000/1500/2000ms -- five seconds of deliberate waiting against a window measured
        // at 261 seconds, under-provisioned by roughly fifty times. An attempt count is the
        // wrong unit: it says how many times to ask, when the question is how long to wait.
        for (int attempt = 1; ; attempt++) {
            try {
                JdbcConnectionSource connectionSource = new JdbcConnectionSource(url);
                DatabaseConnection connection = connectionSource.getReadWriteConnection("h2_open_probe");
                connectionSource.releaseConnection(connection);
                // LOGGED ON SUCCESS, not only on failure. This is the only place the length of
                // a real contention window can be observed, and without it the budget below can
                // only ever be set from the one time somebody went looking: corpus job 3135's
                // cold cohort of 48 produced a 261-second window, measured after the fact from
                // WARN timestamps. Every run now contributes that measurement instead.
                if (attempt > 1) {
                    logger.info("H2 connection succeeded on attempt " + attempt
                            + " after waiting " + (System.currentTimeMillis() - startedAt)
                            + "ms of a " + budgetMs + "ms budget: " + url);
                }
                return connectionSource;
            } catch (SQLException e) {
                lastError = e;
                if (isUnreadableDatabaseFileError(e)) {
                    throw createUnreadableDatabaseException(url, e);
                }
                long elapsed = System.currentTimeMillis() - startedAt;
                long delay = Math.min(baseDelayMs * attempt, MAX_BACKOFF_MS);
                if (elapsed + delay >= budgetMs) {
                    break;
                }
                logger.warn(
                        "H2 connection attempt " + attempt + " failed after " + elapsed
                                + "ms of a " + budgetMs + "ms budget, retrying in " + delay
                                + "ms: " + e.getMessage());
                try {
                    Thread.sleep(delay);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    throw e;
                }
            }
        }
        // NAME THE LOCK, THE PATH AND THE WAIT. The bare `throw lastError` handed the caller an
        // h2 message with no database path in it -- the URL is CWD-relative
        // ("jdbc:h2:file:./db/..."), so which of the several card databases in a checkout this
        // was is not recoverable from the message. Combined with the callers swallowing the
        // exception, the operator's first and only symptom was "expansionDao is null" from
        // somewhere else entirely, and corpus job 3135 lost 48 games before anybody looked at
        // h2 at all.
        Path dbPath = getH2FilePath(url);
        long waitedMs = System.currentTimeMillis() - startedAt;
        throw new SQLException(
                "Could not open the H2 database after " + waitedMs + "ms of a " + budgetMs + "ms budget."
                        + " url=" + url
                        + " path=" + (dbPath == null ? "<unresolved from url>" : dbPath.toAbsolutePath())
                        + " cwd=" + Paths.get("").toAbsolutePath()
                        + ". A \"Lock file recently modified\" cause here means another JVM held this"
                        + " database while this one started; raise MAGEBENCH_H2_RETRY_BUDGET_MS,"
                        + " and the launcher ceiling with it -- neither moves alone. Last error: " + lastError.getMessage(),
                lastError);
    }

    static boolean isUnreadableDatabaseFileError(SQLException error) {
        Throwable current = error;
        while (current != null) {
            String message = current.getMessage();
            if (message != null && message.contains("Unsupported database file version or invalid file header")) {
                return true;
            }
            if ("org.h2.mvstore.MVStoreException".equals(current.getClass().getName())) {
                return true;
            }
            current = current.getCause();
        }
        return false;
    }

    static IllegalStateException createUnreadableDatabaseException(String url, SQLException cause) {
        Path dbPath = getH2FilePath(url);
        if (dbPath == null) {
            return new IllegalStateException(
                    "Unreadable H2 database for non-file URL " + url
                            + ". Delete or migrate the database manually before restarting.",
                    cause
            );
        }

        Path mvStorePath = dbPath.resolveSibling(dbPath.getFileName() + ".mv.db");
        return new IllegalStateException(
                "Unreadable H2 database file " + mvStorePath
                        + ". Delete or migrate the database manually before restarting.",
                cause
        );
    }

    static Path getH2FilePath(String url) {
        if (!url.startsWith(H2_FILE_PREFIX)) {
            return null;
        }

        int paramsPos = url.indexOf(';');
        String fileName = paramsPos >= 0
                ? url.substring(H2_FILE_PREFIX.length(), paramsPos)
                : url.substring(H2_FILE_PREFIX.length());
        return Paths.get(fileName);
    }

    /**
     * Prepare JDBC connection string and setup additional params for SQLite databases
     *
     * @param dbName database name like "cards"
     */
    public static String prepareSqliteConnection(String dbName) {
        // example: jdbc:sqlite:./db/table_record.db
        return String.format("jdbc:sqlite:./db/%s", dbName);
    }
}
