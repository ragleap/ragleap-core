package com.ragleap.observability;

import static org.junit.jupiter.api.Assertions.*;

import java.io.IOException;
import org.junit.jupiter.api.Test;

class RagleapObservabilityTest {
    @Test
    void readsChartMetadata() throws IOException {
        String chart = RagleapObservability.readHelmString(
                "ragleap-observability/Chart.yaml");

        assertTrue(chart.contains("name: ragleap-observability"), chart);
        assertTrue(chart.matches("(?s).*\\bversion:\\s*['\"]?0\\.8\\.4['\"]?.*"), chart);
        assertTrue(chart.matches("(?s).*\\bappVersion:\\s*['\"]?0\\.8\\.4['\"]?.*"), chart);
    }

    @Test
    void readsValuesAndTemplate() throws IOException {
        assertFalse(RagleapObservability.readHelmString(
                "ragleap-observability/values.yaml").isBlank());
        assertFalse(RagleapObservability.readHelmString(
                "ragleap-observability/templates/NOTES.txt").isBlank());
    }

    @Test
    void readsRepresentativeObservabilityResources() throws IOException {
        String prometheus = RagleapObservability.readHelmString(
                "ragleap-observability/templates/prometheus-configmap.yaml");
        String grafana = RagleapObservability.readHelmString(
                "ragleap-observability/templates/grafana-deployment.yaml");

        assertFalse(prometheus.isBlank());
        assertFalse(grafana.isBlank());
        assertTrue(grafana.contains("kind: Deployment"), grafana);
    }

    @Test
    void rejectsMissingResources() {
        assertThrows(IOException.class, () ->
                RagleapObservability.openHelm(
                        "ragleap-observability/not-a-real-file.yaml"));
    }

    @Test
    void rejectsNullAndEmptyPaths() {
        assertThrows(NullPointerException.class,
                () -> RagleapObservability.openHelm(null));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapObservability.openHelm(""));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapObservability.openHelm("  "));
    }

    @Test
    void rejectsAbsoluteAndParentPaths() {
        assertThrows(IllegalArgumentException.class,
                () -> RagleapObservability.openHelm("/etc/passwd"));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapObservability.openHelm("../pom.xml"));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapObservability.openHelm(
                        "nested/../../pom.xml"));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapObservability.openHelm("\\etc\\passwd"));
    }
}
