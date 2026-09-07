package org.mage.test.serverside;

import com.j256.ormlite.support.ConnectionSource;
import mage.cards.repository.DatabaseUtils;
import org.junit.Assert;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

import java.io.File;
import java.sql.SQLException;

/**
 * An h2 open failure must say what could not be opened, where, and for how long it tried.
 *
 * Before this, `openH2ConnectionWithRetry` rethrew h2's own exception, whose message carries
 * no database path -- the URL is CWD-relative ("jdbc:h2:file:./db/...") and a checkout holds
 * several card databases. The callers then swallowed it, so the operator's first and only
 * symptom was "Cannot invoke Dao.queryBuilder() because this.expansionDao is null" from an
 * unrelated call site: a null-pointer report for a lock timeout. Corpus job 3135 lost a cold
 * cohort of 48 games and nobody looked at h2 for hours.
 *
 * @author karn-engine
 */
public class H2ConnectionFailureMessageTest {

    @Rule
    public TemporaryFolder tempFolder = new TemporaryFolder();

    /**
     * THE POSITIVE CONTROL, and it runs first on purpose. A test that only ever observes the
     * failure branch cannot tell you the success branch is reachable at all -- and on this
     * path we had never looked. Without this, "it threw a good message" is compatible with
     * "it always throws".
     */
    @Test
    public void test_AnOpenableDatabaseOpens() throws Exception {
        File dir = tempFolder.newFolder();
        String url = "jdbc:h2:file:" + new File(dir, "cards.h2").getAbsolutePath() + ";AUTO_SERVER=TRUE";
        ConnectionSource source = DatabaseUtils.openH2ConnectionWithRetry(url);
        Assert.assertNotNull("the retry path must be able to return a connection", source);
        source.close();
    }

    @Test
    public void test_TheFailureNamesTheDatabaseThePathAndTheWait() throws Exception {
        File dir = tempFolder.newFolder();
        String dbPath = new File(dir, "cards.h2").getAbsolutePath();
        String url = "jdbc:h2:file:" + dbPath + ";AUTO_SERVER=TRUE";
        // A directory the process cannot write into: h2 cannot create its lock file, which is
        // a different cause from lock contention but the same exhaustion path, and it is the
        // one a test can produce deterministically without a second JVM.
        Assert.assertTrue("could not make the folder read-only; the test cannot fail the open",
                dir.setWritable(false));
        try {
            DatabaseUtils.openH2ConnectionWithRetry(url);
            Assert.fail("expected the open to fail against an unwritable directory");
        } catch (SQLException e) {
            String message = e.getMessage();
            // Each of these was absent from the old message, and each is something the
            // operator had to go and find by hand.
            Assert.assertTrue("must name the resolved absolute path, not the CWD-relative url: " + message,
                    message.contains(dbPath));
            Assert.assertTrue("must name the jdbc url: " + message, message.contains(url));
            Assert.assertTrue("must say how many attempts were made: " + message,
                    message.contains("5 attempts"));
            Assert.assertTrue("must say how long it waited: " + message, message.contains("ms."));
            Assert.assertTrue("must name the working directory, since the url is relative to it: " + message,
                    message.contains("cwd="));
            // The cause is CHAINED, not flattened: h2's own exception carries the lock state,
            // and a message-only rethrow loses exactly the part that says what went wrong.
            Assert.assertNotNull("the underlying h2 error must remain the cause", e.getCause());
        } finally {
            dir.setWritable(true);
        }
    }
}
