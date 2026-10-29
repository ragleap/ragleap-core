package com.ragleap.ansible;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

/**
 * Read-only access to Ansible playbooks packaged in the RagLeap JAR.
 *
 * <p>This class reads classpath resources only. It does not execute Ansible,
 * launch processes, connect to hosts, or modify infrastructure.</p>
 */
public final class RagleapAnsible {
    private static final String RESOURCE_ROOT = "ragleap_ansible/playbooks/";

    private RagleapAnsible() {
    }

    /**
     * Opens a packaged playbook resource.
     *
     * @param path relative path inside the packaged {@code playbooks/} tree
     * @return an input stream for the resource; the caller must close it
     * @throws IllegalArgumentException if the path is blank, absolute,
     *                                  contains backslashes, or traverses a parent
     * @throws IOException if the resource does not exist
     */
    public static InputStream openPlaybook(String path) throws IOException {
        validatePath(path);
        InputStream stream = RagleapAnsible.class.getClassLoader()
                .getResourceAsStream(RESOURCE_ROOT + path);
        if (stream == null) {
            throw new IOException("Packaged Ansible resource not found: " + path);
        }
        return stream;
    }

    /**
     * Reads a packaged playbook resource as UTF-8 text.
     *
     * @param path relative path inside the packaged {@code playbooks/} tree
     * @return resource contents
     * @throws IllegalArgumentException if the path is invalid
     * @throws IOException if the resource does not exist or cannot be read
     */
    public static String readPlaybookString(String path) throws IOException {
        try (InputStream stream = openPlaybook(path)) {
            return new String(stream.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static void validatePath(String path) {
        if (path == null || path.isBlank()) {
            throw new IllegalArgumentException("Resource path must not be blank");
        }
        if (path.startsWith("/") || path.indexOf('\\') >= 0
                || path.matches("^[A-Za-z]:.*")) {
            throw new IllegalArgumentException("Resource path must be relative: " + path);
        }
        for (String segment : path.split("/", -1)) {
            if (segment.equals("..") || segment.equals(".") || segment.isEmpty()) {
                throw new IllegalArgumentException("Invalid resource path: " + path);
            }
        }
    }
}
