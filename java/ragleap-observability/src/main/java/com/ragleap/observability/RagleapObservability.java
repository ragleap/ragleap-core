package com.ragleap.observability;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Objects;

/**
 * Reads Helm chart resources bundled in the Ragleap Observability JAR.
 *
 * <p>This API does not install charts, deploy workloads, or access a cluster.</p>
 */
public final class RagleapObservability {
    private static final String HELM_ROOT = "/ragleap_observability/helm/";

    private RagleapObservability() {
    }

    /**
     * Opens a bundled Helm resource.
     *
     * @param relativePath path relative to the bundled Helm directory
     * @return resource stream; the caller must close it
     * @throws IllegalArgumentException if the path is empty, absolute, or unsafe
     * @throws IOException if the resource does not exist
     */
    public static InputStream openHelm(String relativePath) throws IOException {
        Objects.requireNonNull(relativePath, "relativePath must not be null");

        if (relativePath.isBlank()
                || relativePath.startsWith("/")
                || relativePath.startsWith("\\")
                || relativePath.contains("\\")) {
            throw new IllegalArgumentException("Invalid resource path");
        }

        Path path = Path.of(relativePath);
        if (path.isAbsolute()) {
            throw new IllegalArgumentException("Absolute paths are not allowed");
        }

        for (Path segment : path) {
            if (segment.toString().equals("..")) {
                throw new IllegalArgumentException("Parent path segments are not allowed");
            }
        }

        String normalized = path.normalize().toString().replace('\\', '/');
        if (normalized.isBlank() || normalized.equals(".")) {
            throw new IllegalArgumentException("Invalid resource path");
        }

        InputStream stream =
                RagleapObservability.class.getResourceAsStream(HELM_ROOT + normalized);
        if (stream == null) {
            throw new IOException("Resource not found: " + relativePath);
        }
        return stream;
    }

    /**
     * Reads a bundled Helm resource as UTF-8 text.
     *
     * @param relativePath path relative to the bundled Helm directory
     * @return resource contents
     * @throws IOException if the resource does not exist or cannot be read
     */
    public static String readHelmString(String relativePath) throws IOException {
        try (InputStream stream = openHelm(relativePath)) {
            return new String(stream.readAllBytes(), StandardCharsets.UTF_8);
        }
    }
}
