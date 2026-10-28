package com.ragleap.ops;

import static org.junit.jupiter.api.Assertions.*;

import java.io.IOException;
import org.junit.jupiter.api.Test;

class RagleapOpsTest {
    @Test
    void readsHelmChartMetadata() throws IOException {
        String chart = RagleapOps.readHelmString("ragleap-ops/Chart.yaml");

        assertTrue(chart.contains("name: ragleap-ops"), chart);
        assertTrue(chart.matches("(?s).*\\bversion:\\s*['\"]?0\\.6\\.2['\"]?.*"), chart);
        assertTrue(chart.matches("(?s).*\\bappVersion:\\s*['\"]?0\\.6\\.2['\"]?.*"), chart);
    }

    @Test
    void readsHelmValuesAndTemplate() throws IOException {
        assertFalse(RagleapOps.readHelmString("ragleap-ops/values.yaml").isBlank());
        assertFalse(RagleapOps.readHelmString(
                "ragleap-ops/templates/app-deployment.yaml").isBlank());
    }

    @Test
    void readsKubernetesManifests() throws IOException {
        String namespace = RagleapOps.readK8sString("namespace.yaml");
        String app = RagleapOps.readK8sString("app-deployment.yaml");

        assertFalse(namespace.isBlank());
        assertFalse(app.isBlank());
        assertTrue(app.contains("kind: Deployment"), app);
    }

    @Test
    void rejectsMissingResources() {
        assertThrows(IOException.class,
                () -> RagleapOps.openHelm("ragleap-ops/not-a-real-file.yaml"));
        assertThrows(IOException.class,
                () -> RagleapOps.openK8s("not-a-real-file.yaml"));
    }

    @Test
    void rejectsNullAndEmptyPaths() {
        assertThrows(NullPointerException.class, () -> RagleapOps.openHelm(null));
        assertThrows(IllegalArgumentException.class, () -> RagleapOps.openHelm(""));
        assertThrows(IllegalArgumentException.class, () -> RagleapOps.openK8s("  "));
    }

    @Test
    void rejectsAbsoluteAndParentPaths() {
        assertThrows(IllegalArgumentException.class,
                () -> RagleapOps.openHelm("/etc/passwd"));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapOps.openK8s("../pom.xml"));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapOps.openK8s("nested/../../pom.xml"));
    }
}
