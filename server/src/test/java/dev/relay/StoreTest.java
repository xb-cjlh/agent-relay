package dev.relay;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.jdbc.datasource.AbstractDataSource;
import java.nio.file.*;
import java.sql.*;
import javax.sql.DataSource;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
import static dev.relay.Values.*;

class StoreTest {
    @TempDir Path directory;
    // Database behavior is covered
    // by the HTTP integration suite against real, isolated MySQL databases.
    private final DataSource noDatabase = new AbstractDataSource() {
        public Connection getConnection() throws SQLException { throw new SQLException("unavailable"); }
        public Connection getConnection(String u,String p) throws SQLException { return getConnection(); }
    };
    @Test void unavailableDatabaseFailsHealthCheck() throws Exception {
        Store store=new Store(directory.toString(),new ObjectMapper(),noDatabase,new OssImages(new org.springframework.mock.env.MockEnvironment()));
        assertFalse(store.healthy());
    }
    @Test void rejectsInvalidSessionIdsAndNonFiniteTimes() {
        for (String sid : List.of("-flag","../session","会话","a b","")) assertThrows(ApiException.class,()->sessionId(sid));
        assertThrows(ApiException.class,()->number(Double.NaN)); assertThrows(ApiException.class,()->integer(true,1,1800,"timeout"));
        assertEquals("😀",tail("ab😀",1)); assertEquals("😀",head("😀ab",1));
    }
}
