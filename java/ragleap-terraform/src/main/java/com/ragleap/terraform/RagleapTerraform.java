package com.ragleap.terraform;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;

/**
 * Reads the Terraform module and example files that are bundled inside this jar.
 *
 * <p>Paths are relative to the bundle root, for example
 * {@code modules/kind-cluster/main.tf} or {@code examples/kind/main.tf}.
 */
public final class RagleapTerraform {

    private static final String ROOT = "/ragleap_terraform/";

    private RagleapTerraform() {
    }

    /**
     * Opens a bundled file. The caller closes the stream.
     *
     * @param relativePath path inside the bundle, without a leading slash and without "..".
     * @return a stream over the file.
     * @throws IllegalArgumentException if the path is unsafe or the file does not exist.
     */
    public static InputStream open(String relativePath) {
        if (relativePath == null || relativePath.isEmpty() || relativePath.startsWith("/")
                || relativePath.contains("..")) {
            throw new IllegalArgumentException(
                    "relativePath must be a relative path without '..': " + relativePath);
        }
        InputStream in = RagleapTerraform.class.getResourceAsStream(ROOT + relativePath);
        if (in == null) {
            throw new IllegalArgumentException("no such bundled file: " + relativePath);
        }
        return in;
    }

    /**
     * Reads a bundled file as UTF-8 text.
     *
     * @param relativePath path inside the bundle, see {@link #open(String)}.
     * @return the file content.
     */
    public static String readString(String relativePath) {
        try (InputStream in = open(relativePath)) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
