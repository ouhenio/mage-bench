package mage.cards.repository;

import com.j256.ormlite.dao.Dao;
import com.j256.ormlite.dao.DaoManager;
import com.j256.ormlite.dao.GenericRawResults;
import com.j256.ormlite.jdbc.JdbcConnectionSource;
import com.j256.ormlite.stmt.QueryBuilder;
import com.j256.ormlite.stmt.SelectArg;
import com.j256.ormlite.support.ConnectionSource;
import com.j256.ormlite.table.TableUtils;
import mage.game.events.Listener;
import org.apache.log4j.Logger;

import java.io.File;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedList;
import java.util.List;

/**
 * @author North, JayDi85
 */
public enum ExpansionRepository {

    instance;

    private static final Logger logger = Logger.getLogger(ExpansionRepository.class);

    // TODO: delete db version from cards and expansions due un-used (that's dbs re-created on each update)
    private static final String VERSION_ENTITY_NAME = "expansion";
    private static final long EXPANSION_DB_VERSION = 5;
    private static final long EXPANSION_CONTENT_VERSION = 18;

    private Dao<ExpansionInfo, Object> expansionDao;
    private final RepositoryEventSource eventSource = new RepositoryEventSource();
    public boolean instanceInitialized = false;
    // WHY THE CONSTRUCTOR STILL DOES NOT THROW. This is an enum singleton, so an exception
    // escaping the constructor becomes ExceptionInInitializerError and every later access
    // throws NoClassDefFoundError with the original cause gone -- strictly worse diagnostics
    // than the null DAO it would replace. So the failure is RECORDED here and raised by
    // RepositoryUtil.bootstrapLocalDb, which is the single point both entry points
    // (Mage.Server/Main and Mage.Client/MageFrame) already call, and which can report it.
    // What must never happen again is the object being handed out as usable with nothing
    // remembering that it is not.
    private volatile SQLException initFailure = null;

    /**
     * The error that stopped this repository initialising, or null if it initialised.
     */
    public SQLException getInitFailure() {
        return initFailure;
    }

    ExpansionRepository() {
        File file = new File("db");
        if (!file.exists()) {
            file.mkdirs();
        }
        try {
            ConnectionSource connectionSource = DatabaseUtils.openH2ConnectionWithRetry(DatabaseUtils.prepareH2Connection(DatabaseUtils.DB_NAME_CARDS, true));

            boolean isObsolete = RepositoryUtil.isDatabaseObsolete(connectionSource, VERSION_ENTITY_NAME, EXPANSION_DB_VERSION);
            boolean isNewBuild = RepositoryUtil.isNewBuildRun(connectionSource, VERSION_ENTITY_NAME, ExpansionRepository.class); // recreate db on new build
            if (isObsolete || isNewBuild) {
                //System.out.println("Local sets db is outdated, cleaning...");
                TableUtils.dropTable(connectionSource, ExpansionInfo.class, true);
            }

            TableUtils.createTableIfNotExists(connectionSource, ExpansionInfo.class);
            expansionDao = DaoManager.createDao(connectionSource, ExpansionInfo.class);
            instanceInitialized = true;

            eventSource.fireRepositoryDbLoaded();
        } catch (SQLException e) {
            // NOT SWALLOWED. This used to be a bare printStackTrace(): the constructor then
            // returned normally with expansionDao null and instanceInitialized false, nothing
            // on the server path reads that flag, and the first symptom was an NPE from
            // wherever the DAO was next touched -- "Cannot invoke Dao.queryBuilder() because
            // this.expansionDao is null", a message naming no database, no file, no lock and
            // no wait. See issues/p1-a-swallowed-sqlexception-turns-a-lock-timeout-into-a-null-dao-npe.
            initFailure = e;
            // Logger.getLogger, NOT the static `logger` field: this is an enum constructor, and
            // Java forbids reading a static field from one -- it has not been initialised yet.
            // That constraint is why the original code reached for printStackTrace() here, and
            // CardRepository does the same lookup at its own catch for the same reason.
            Logger.getLogger(ExpansionRepository.class).error("Could not initialise the expansion repository; every later use of it"
                    + " would have failed with a null DAO instead of this message: " + e.getMessage(), e);
        }

    }

    /**
     * Warning, don't forget to unsubscribe due memory leak problems
     *
     * @param listener
     */
    public void subscribe(Listener<RepositoryEvent> listener) {
        eventSource.addListener(listener);
    }

    public void unsubscribe(Listener<RepositoryEvent> listener) {
        eventSource.removeListener(listener);
    }

    public void saveSets(final List<ExpansionInfo> newSets, final List<ExpansionInfo> updatedSets, long newContentVersion) {
        try {
            expansionDao.callBatchTasks(() -> {
                // add
                if (newSets != null && !newSets.isEmpty()) {
                    logger.info("DB: need to add " + newSets.size() + " new sets");
                    try {
                        for (ExpansionInfo exp : newSets) {
                            expansionDao.create(exp);
                        }
                    } catch (SQLException ex) {
                        logger.error("Error adding expansions to DB - ", ex);
                    }
                }

                // update
                if (updatedSets != null && !updatedSets.isEmpty()) {
                    logger.info("DB: need to update " + updatedSets.size() + " sets");
                    try {
                        for (ExpansionInfo exp : updatedSets) {
                            expansionDao.update(exp);
                        }
                    } catch (SQLException ex) {
                        logger.error("Error adding expansions to DB - ", ex);
                    }
                }

                return null;
            });

            setContentVersion(newContentVersion);
            eventSource.fireRepositoryDbUpdated();
        } catch (Exception ex) {
            //
        }
    }

    public List<String> getSetCodes() {
        List<String> setCodes = new ArrayList<>();
        try {
            List<ExpansionInfo> expansions = expansionDao.queryForAll();
            for (ExpansionInfo expansion : expansions) {
                setCodes.add(expansion.getCode());
            }
        } catch (SQLException ex) {
            logger.error("Can't get the expansion set codes from database.", ex);
            return setCodes;
        }
        return setCodes;
    }

    public ExpansionInfo[] getWithBoostersSortedByReleaseDate() {

        try {
            // only with boosters and cards
            GenericRawResults<ExpansionInfo> setsList = expansionDao.queryRaw(
                    "select * from expansion e "
                            + " where e.boosters = 1 "
                            + "   and exists(select (1) from  card c where c.setcode = e.code) "
                            + " order by e.releasedate desc",
                    expansionDao.getRawRowMapper());

            List<ExpansionInfo> resList = new ArrayList<>();
            for (ExpansionInfo info : setsList) {
                resList.add(info);
            }
            return resList.toArray(new ExpansionInfo[0]);

        } catch (SQLException ex) {
            logger.error(ex);
            return new ExpansionInfo[0];
        }
    }

    public List<ExpansionInfo> getSetsWithBasicLandsByReleaseDate() {
        List<ExpansionInfo> sets = new LinkedList<>();
        try {
            QueryBuilder<ExpansionInfo, Object> qb = expansionDao.queryBuilder();
            qb.orderBy("releaseDate", false);
            qb.where().eq("basicLands", new SelectArg(true));
            sets = expansionDao.query(qb.prepare());
        } catch (SQLException ex) {
            logger.error(ex);
        }
        return sets;
    }

    public List<ExpansionInfo> getSetsFromBlock(String blockName) {
        List<ExpansionInfo> sets = new LinkedList<>();
        try {
            QueryBuilder<ExpansionInfo, Object> qb = expansionDao.queryBuilder();
            qb.where().eq("blockName", new SelectArg(blockName));
            return expansionDao.query(qb.prepare());
        } catch (SQLException ex) {
            logger.error(ex);
        }
        return sets;
    }

    public ExpansionInfo getSetByCode(String setCode) {
        ExpansionInfo set = null;
        try {
            QueryBuilder<ExpansionInfo, Object> qb = expansionDao.queryBuilder();
            qb.limit(1L).where().eq("code", new SelectArg(setCode));
            List<ExpansionInfo> expansions = expansionDao.query(qb.prepare());
            if (!expansions.isEmpty()) {
                set = expansions.get(0);
            }
        } catch (SQLException ex) {
            logger.error(ex);
        }
        return set;
    }

    public ExpansionInfo getSetByName(String setName) {
        ExpansionInfo set = null;
        try {
            QueryBuilder<ExpansionInfo, Object> qb = expansionDao.queryBuilder();
            qb.limit(1L).where().eq("name", new SelectArg(setName));
            List<ExpansionInfo> expansions = expansionDao.query(qb.prepare());
            if (!expansions.isEmpty()) {
                set = expansions.get(0);
            }
        } catch (SQLException ex) {
            logger.error(ex);
        }
        return set;
    }

    public List<ExpansionInfo> getAll() {
        try {
            QueryBuilder<ExpansionInfo, Object> qb = expansionDao.queryBuilder();
            qb.orderBy("releaseDate", true);
            return expansionDao.query(qb.prepare());
        } catch (SQLException ex) {
            logger.error(ex);
        }
        return Collections.emptyList();
    }

    public long getContentVersionFromDB() {
        try {
            ConnectionSource connectionSource = DatabaseUtils.openH2ConnectionWithRetry(DatabaseUtils.prepareH2Connection(DatabaseUtils.DB_NAME_CARDS, false));
            return RepositoryUtil.getDatabaseVersion(connectionSource, VERSION_ENTITY_NAME + "Content");
        } catch (SQLException ex) {
            ex.printStackTrace();
        }
        return 0;
    }

    public void setContentVersion(long version) {
        try {
            ConnectionSource connectionSource = DatabaseUtils.openH2ConnectionWithRetry(DatabaseUtils.prepareH2Connection(DatabaseUtils.DB_NAME_CARDS, false));
            RepositoryUtil.updateVersion(connectionSource, VERSION_ENTITY_NAME + "Content", version);
        } catch (SQLException e) {
            logger.error("Error setting content version - " + e, e);
        }
    }

    public long getContentVersionConstant() {
        return EXPANSION_CONTENT_VERSION;
    }
}
