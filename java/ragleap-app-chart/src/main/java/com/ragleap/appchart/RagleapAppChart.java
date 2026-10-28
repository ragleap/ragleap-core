package com.ragleap.appchart;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Objects;

/**
 * Reads the Helm chart resources bundled in the Ragleap app-chart JAR.
 *
 * <p>Resource paths are relative to {@code ragleap_app_chart/helm/}.
 * For example, use {@code ragleap-app-chart/Chart.yaml} to read the chart
 * metadata. This class reads packaged resources; it does not install or
 * deploy a Helm chart.</p>
 */
public final class RagleapAppChart {
    private static final String RESOURCE_ROOT = "/ragleap_app_chart/helm/";

    private RagleapAppChart() {
    }

    /**
     * Opens a packaged chart resource.
     *
     * @param relativePath path relative to the bundled Helm directory
     * @return an open resource stream; the caller must close it
     * @throws IllegalArgumentException if the path is empty, absolute, or unsafe
     * @throws IOException if the resource does not exist in the JAR
     */
    public static InputStream open(String relativePath) throws IOException {
        Objects.requireNonNull(relativePath, "relativePath must not be null");

        if (relativePath.isBlank()
                || relativePath.startsWith("/")
                || relativePath.startsWith("\\")
                || relativePath.contains("\\")) {
            throw new IllegalArgumentException("Invalid chart resource path");
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
            throw new IllegalArgumentException("Invalid chart resource path");
        }

        InputStream stream = RagleapAppChart.class.getResourceAsStream(
                RESOURCE_ROOT + normalized);
        if (stream == null) {
            throw new IOException("Chart resource not found: " + relativePath);
        }
        return stream;
    }

    /**
     * Reads a packaged chart resource as UTF-8 text.
     *
     * @param relativePath path relative to the bundled Helm directory
     * @return the resource content
     * @throws IOException if the resource does not exist or cannot be read
     */
    public static String readString(String relativePath) throws IOException {
        try (InputStream stream = open(relativePath)) {
            return new String(stream.readAllBytes(), StandardCharsets.UTF_8);
        }
    }
}
