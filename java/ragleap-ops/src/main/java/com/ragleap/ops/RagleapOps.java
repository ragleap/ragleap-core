package com.ragleap.ops;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Objects;

/**
 * Reads Helm chart and Kubernetes manifest resources bundled in the Ragleap
 * Ops JAR. This class does not install charts or apply Kubernetes resources.
 */
public final class RagleapOps {
    private static final String HELM_ROOT = "/ragleap_ops/helm/";
    private static final String K8S_ROOT = "/ragleap_ops/k8s/";

    private RagleapOps() {
    }

    /**
     * Opens a bundled Helm resource.
     *
     * @param relativePath path relative to the bundled Helm directory
     * @return an open resource stream; the caller must close it
     * @throws IllegalArgumentException if the path is empty, absolute, or unsafe
     * @throws IOException if the resource does not exist
     */
    public static InputStream openHelm(String relativePath) throws IOException {
        return open(HELM_ROOT, relativePath);
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

    /**
     * Opens a bundled Kubernetes manifest or other k8s resource.
     *
     * @param relativePath path relative to the bundled k8s directory
     * @return an open resource stream; the caller must close it
     * @throws IllegalArgumentException if the path is empty, absolute, or unsafe
     * @throws IOException if the resource does not exist
     */
    public static InputStream openK8s(String relativePath) throws IOException {
        return open(K8S_ROOT, relativePath);
    }

    /**
     * Reads a bundled Kubernetes resource as UTF-8 text.
     *
     * @param relativePath path relative to the bundled k8s directory
     * @return resource contents
     * @throws IOException if the resource does not exist or cannot be read
     */
    public static String readK8sString(String relativePath) throws IOException {
        try (InputStream stream = openK8s(relativePath)) {
            return new String(stream.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static InputStream open(String root, String relativePath) throws IOException {
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

        InputStream stream = RagleapOps.class.getResourceAsStream(root + normalized);
        if (stream == null) {
            throw new IOException("Resource not found: " + relativePath);
        }
        return stream;
    }
}
