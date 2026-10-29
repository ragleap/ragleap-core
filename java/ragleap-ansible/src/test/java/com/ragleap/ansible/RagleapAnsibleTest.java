package com.ragleap.ansible;

import static org.junit.jupiter.api.Assertions.*;

import java.io.IOException;
import org.junit.jupiter.api.Test;

class RagleapAnsibleTest {
    @Test
    void readsBootstrapPlaybook() throws IOException {
        String content = RagleapAnsible.readPlaybookString("bootstrap.yml");
        assertFalse(content.isBlank());
    }

    @Test
    void readsRoleTaskResource() throws IOException {
        String content = RagleapAnsible.readPlaybookString("roles/ragleap_base/tasks/main.yml");
        assertFalse(content.isBlank());
    }

    @Test
    void readsNestedRoleDefaults() throws IOException {
        String content = RagleapAnsible.readPlaybookString("roles/ragleap_base/defaults/main.yml");
        assertFalse(content.isBlank());
    }

    @Test
    void missingResourceFailsClearly() {
        IOException error = assertThrows(IOException.class,
                () -> RagleapAnsible.readPlaybookString("does-not-exist.yml"));
        assertTrue(error.getMessage().contains("not found"));
    }

    @Test
    void rejectsBlankAndNullPaths() {
        assertThrows(IllegalArgumentException.class,
                () -> RagleapAnsible.openPlaybook(null));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapAnsible.openPlaybook("  "));
    }

    @Test
    void rejectsAbsoluteAndTraversalPaths() {
        assertThrows(IllegalArgumentException.class,
                () -> RagleapAnsible.openPlaybook("/etc/passwd"));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapAnsible.openPlaybook("../secret.yml"));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapAnsible.openPlaybook("roles/../../secret.yml"));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapAnsible.openPlaybook("C:/secret.yml"));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapAnsible.openPlaybook("roles\\secret.yml"));
    }
}
